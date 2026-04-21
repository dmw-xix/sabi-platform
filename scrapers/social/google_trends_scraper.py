# scrapers/social/google_trends_scraper.py
# Two-layer approach:
# Layer 1: RSS feed → Nigeria daily trending topics (no auth, fast)
# Layer 2: Playwright → intercepts Explore page API calls for artist data
# No cookie file needed — Playwright runs as a real browser

import re
import json
import time
import hashlib
import requests
import feedparser
from datetime import date, datetime, timedelta
from loguru import logger
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout
from database.client import get_supabase, log_scraper_run
from config.settings import YOUTUBE_API_KEY

SCRAPER_NAME = "google_trends_ng"
SOURCE = "google_trends"
TODAY = str(date.today())

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0.0.0 Safari/537.36"
    ),
}

TRENDS_RSS_URL = (
    "https://trends.google.com/trending"
)

YOUTUBE_TRENDING_URL = "https://www.googleapis.com/youtube/v3/videos"


# ─────────────────────────────────────────────
# LAYER 1: Nigeria Daily Trending RSS
# ─────────────────────────────────────────────

def fetch_nigeria_trending_rss() -> list[dict]:
    """
    Pulls Google's public daily trending searches for Nigeria.
    No auth needed. Updates ~hourly.
    """
    trends = []
    try:
        logger.info("Fetching Nigeria trending RSS...")
        response = requests.get(TRENDS_RSS_URL, headers=HEADERS, timeout=20)
        response.raise_for_status()

        feed = feedparser.parse(response.content)
        if not feed.entries:
            logger.warning("Empty RSS feed")
            return []

        for entry in feed.entries:
            title = entry.get("title", "").strip()
            traffic_text = getattr(entry, "ht_approx_traffic", "") or ""
            traffic = _parse_traffic(traffic_text)

            related = []
            # Grab related news headlines
            for key in entry.keys():
                if "news_item_title" in key:
                    related.append(entry[key])

            trends.append({
                "term": title,
                "traffic": traffic,
                "related": related[:3],
            })

        logger.info(f"  {len(trends)} trending topics in Nigeria")
        for t in trends[:5]:
            logger.debug(f"  → '{t['term']}' ~{t['traffic']:,} searches")

    except Exception as e:
        logger.error(f"RSS error: {e}")

    return trends


def _parse_traffic(text: str) -> int:
    text = str(text).strip().replace("+", "").replace(",", "")
    try:
        if text.upper().endswith("M"):
            return int(float(text[:-1]) * 1_000_000)
        elif text.upper().endswith("K"):
            return int(float(text[:-1]) * 1_000)
        return int(float(text)) if text else 0
    except ValueError:
        return 0


# ─────────────────────────────────────────────
# LAYER 2: Playwright Explore Page Interceptor
# ─────────────────────────────────────────────

def fetch_artist_explore_data(
    artist_name: str,
    geo: str = "NG",
    timeframe: str = "now 7-d",
) -> dict | None:
    """
    Opens Google Trends Explore page in a real browser.
    Intercepts the internal API calls that load chart data.

    Returns:
    - interest_over_time: list of {date, value} points
    - related_queries_rising: top rising searches alongside the artist
    - related_queries_top: top overall related searches
    - avg_interest: 7-day average (0-100)
    - peak_interest: highest point
    """
    explore_url = (
        f"https://trends.google.com/trends/explore"
        f"?q={requests.utils.quote(artist_name)}"
        f"&geo={geo}"
        f"&date={requests.utils.quote(timeframe)}"
        f"&hl=en-US"
    )

    captured_data = {
        "interest_over_time": [],
        "related_queries_rising": [],
        "related_queries_top": [],
        "raw_responses": [],
    }

    def handle_response(response):
        """Intercept internal Trends API responses."""
        url = response.url
        if "trends.google.com/trends/api/" not in url:
            return
        try:
            body = response.text()
            # Google Trends API responses start with ")]}',\n"
            # Strip that prefix before parsing JSON
            if body.startswith(")]}',"):
                body = body[5:].strip()
            elif body.startswith(")]}'"):
                body = body[4:].strip()

            data = json.loads(body)
            captured_data["raw_responses"].append({
                "url": url,
                "data": data,
            })
        except Exception:
            pass

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent=HEADERS["User-Agent"],
            locale="en-US",
            timezone_id="Africa/Lagos",
            viewport={"width": 1280, "height": 900},
        )
        page = context.new_page()
        page.on("response", handle_response)

        try:
            logger.debug(f"  Opening Explore: {artist_name}")
            page.goto(explore_url, wait_until="networkidle", timeout=30000)
            time.sleep(4)  # Let all API calls complete

        except PlaywrightTimeout:
            logger.warning(f"  Timeout for {artist_name} explore page")
        except Exception as e:
            logger.error(f"  Explore error for {artist_name}: {e}")
        finally:
            browser.close()

    # Parse captured API responses
    return _parse_explore_responses(captured_data)


def _parse_explore_responses(captured: dict) -> dict:
    """
    Parses the intercepted API responses to extract:
    - Interest over time data points
    - Rising related queries
    - Top related queries
    """
    result = {
        "interest_over_time": [],
        "related_queries_rising": [],
        "related_queries_top": [],
        "avg_interest": 0.0,
        "peak_interest": 0.0,
        "momentum_pct": 0.0,
    }

    for resp in captured["raw_responses"]:
        url = resp["url"]
        data = resp["data"]

        # Interest over time response
        if "multiline" in url or "timeseries" in url:
            try:
                timeline = (
                    data.get("default", {})
                    .get("timelineData", [])
                )
                if not timeline:
                    # Try alternate path
                    timeline = data.get("timelineData", [])

                points = []
                for point in timeline:
                    values = point.get("value", [])
                    if values:
                        points.append(float(values[0]))

                if points:
                    result["interest_over_time"] = points
                    result["avg_interest"] = round(
                        sum(points) / len(points), 2
                    )
                    result["peak_interest"] = round(max(points), 2)

                    # Momentum: second half vs first half
                    mid = len(points) // 2
                    if mid > 0:
                        first_half = sum(points[:mid]) / mid
                        second_half = sum(points[mid:]) / max(1, len(points) - mid)
                        if first_half > 0:
                            result["momentum_pct"] = round(
                                ((second_half - first_half) / first_half) * 100,
                                2
                            )

            except Exception as e:
                logger.debug(f"  Timeline parse error: {e}")

        # Related queries response
        if "relatedsearches" in url or "relatedqueries" in url:
            try:
                default = data.get("default", {})
                ranked_list = default.get("rankedList", [])

                for section in ranked_list:
                    ranked_keywords = section.get("rankedKeyword", [])
                    section_type = "rising" if any(
                        k.get("link", "").find("RISING") != -1
                        or str(k.get("value", "")).find("+") != -1
                        for k in ranked_keywords
                    ) else "top"

                    terms = [
                        {
                            "term": k.get("query", ""),
                            "value": k.get("value", ""),
                        }
                        for k in ranked_keywords[:10]
                        if k.get("query")
                    ]

                    if section_type == "rising":
                        result["related_queries_rising"].extend(terms)
                    else:
                        result["related_queries_top"].extend(terms)

            except Exception as e:
                logger.debug(f"  Related queries parse error: {e}")

    return result


# ─────────────────────────────────────────────
# LAYER 3: YouTube Trending Nigeria
# ─────────────────────────────────────────────

def fetch_youtube_trending_nigeria() -> list[dict]:
    """Fetches YouTube music trending in Nigeria using existing API key."""
    if not YOUTUBE_API_KEY:
        return []

    videos = []
    try:
        logger.info("Fetching YouTube trending Nigeria (Music)...")
        response = requests.get(
            YOUTUBE_TRENDING_URL,
            params={
                "part": "snippet,statistics",
                "chart": "mostPopular",
                "regionCode": "NG",
                "videoCategoryId": "10",  # Music
                "maxResults": 50,
                "key": YOUTUBE_API_KEY,
            },
            timeout=20,
        )
        response.raise_for_status()
        data = response.json()

        for item in data.get("items", []):
            snippet = item.get("snippet", {})
            stats = item.get("statistics", {})
            videos.append({
                "video_id": item["id"],
                "title": snippet.get("title", ""),
                "channel": snippet.get("channelTitle", ""),
                "views": int(stats.get("viewCount", 0)),
                "likes": int(stats.get("likeCount", 0)),
            })

        logger.info(f"  {len(videos)} trending music videos")

    except Exception as e:
        logger.error(f"YouTube trending error: {e}")

    return videos


# ─────────────────────────────────────────────
# Matching and Saving
# ─────────────────────────────────────────────

def match_artists_in_rss(
    trends: list[dict], artists: list[dict]
) -> dict:
    """Match tracked artists against RSS trending terms."""
    results = {}
    for artist in artists:
        name_lower = artist["name"].lower()
        parts = name_lower.split()
        primary = parts[0] if parts and len(parts[0]) > 3 else name_lower

        appearances = 0
        total_traffic = 0
        matched_terms = []

        for trend in trends:
            term_lower = trend["term"].lower()
            if name_lower in term_lower or primary in term_lower:
                appearances += 1
                total_traffic += trend["traffic"]
                matched_terms.append(trend["term"])

        results[artist["slug"]] = {
            "artist_id": artist["id"],
            "appearances": appearances,
            "total_traffic": total_traffic,
            "score": min(100.0, (total_traffic / 1_000_000) * 100),
            "matched_terms": matched_terms,
        }

    return results


def match_artists_in_youtube(
    videos: list[dict], artists: list[dict]
) -> dict:
    """Match tracked artists against YouTube trending videos."""
    results = {}
    for artist in artists:
        name_lower = artist["name"].lower()
        parts = name_lower.split()
        primary = parts[0] if parts and len(parts[0]) > 3 else name_lower

        matching = [
            v for v in videos
            if name_lower in v["title"].lower()
            or primary in v["title"].lower()
            or name_lower in v["channel"].lower()
        ]

        if matching:
            results[artist["slug"]] = {
                "artist_id": artist["id"],
                "count": len(matching),
                "views": sum(v["views"] for v in matching),
                "top_title": matching[0]["title"],
            }

    return results


def save_all_metrics(
    db,
    rss_results: dict,
    youtube_results: dict,
    explore_results: dict,
    trending_searches: list[dict],
) -> int:
    saved = 0

    # RSS-based metrics
    for slug, d in rss_results.items():
        for metric, value in [
            ("google_trends_score", d["score"]),
            ("google_trends_traffic", float(d["total_traffic"])),
            ("google_trends_appearances", float(d["appearances"])),
        ]:
            try:
                db.table("artist_snapshots").upsert({
                    "artist_id": d["artist_id"],
                    "source": SOURCE,
                    "metric_name": metric,
                    "metric_value": value,
                    "snapshot_date": TODAY,
                    "captured_at": datetime.now().isoformat(),
                }, on_conflict="artist_id,source,metric_name,snapshot_date").execute()
                saved += 1
            except Exception as e:
                logger.error(f"  Save error {slug}/{metric}: {e}")

    # Explore page metrics (interest over time)
    for slug, d in explore_results.items():
        for metric, value in [
            ("google_trends_avg", d.get("avg_interest", 0.0)),
            ("google_trends_peak", d.get("peak_interest", 0.0)),
            ("google_trends_momentum", d.get("momentum_pct", 0.0)),
        ]:
            try:
                db.table("artist_snapshots").upsert({
                    "artist_id": d["artist_id"],
                    "source": SOURCE,
                    "metric_name": metric,
                    "metric_value": float(value),
                    "snapshot_date": TODAY,
                    "captured_at": datetime.now().isoformat(),
                }, on_conflict="artist_id,source,metric_name,snapshot_date").execute()
                saved += 1
            except Exception as e:
                logger.error(f"  Explore save error {slug}/{metric}: {e}")

        # Save rising queries as press context
        rising = d.get("related_queries_rising", [])
        if rising:
            try:
                terms_str = ", ".join(q["term"] for q in rising[:5])
                url_hash = hashlib.md5(
                    f"trends-explore-{slug}-{TODAY}".encode()
                ).hexdigest()
                db.table("press_articles").upsert({
                    "url": f"https://trends.google.com/trends/explore?q={slug}&geo=NG",
                    "url_hash": url_hash,
                    "publication": "google_trends_explore",
                    "headline": (
                        f"Rising searches alongside {slug.replace('-', ' ').title()} "
                        f"in Nigeria: {terms_str}"
                    ),
                    "summary": (
                        f"People searching for this artist in Nigeria are "
                        f"also searching for: {terms_str}. "
                        f"7-day avg interest: {d.get('avg_interest', 0):.0f}/100"
                    ),
                    "sentiment_score": 0.0,
                    "sentiment_label": "neutral",
                    "published_at": datetime.now().isoformat(),
                    "scraped_at": datetime.now().isoformat(),
                }, on_conflict="url_hash").execute()
            except Exception as e:
                logger.debug(f"  Rising queries save error: {e}")

    # YouTube trending metrics
    for slug, d in youtube_results.items():
        for metric, value in [
            ("youtube_trending_count", float(d["count"])),
            ("youtube_trending_views", float(d["views"])),
        ]:
            try:
                db.table("artist_snapshots").upsert({
                    "artist_id": d["artist_id"],
                    "source": "youtube_trending",
                    "metric_name": metric,
                    "metric_value": value,
                    "snapshot_date": TODAY,
                    "captured_at": datetime.now().isoformat(),
                }, on_conflict="artist_id,source,metric_name,snapshot_date").execute()
                saved += 1
            except Exception as e:
                logger.error(f"  YT save error {slug}/{metric}: {e}")

    # Save top trending topics for Nigeria as context records
    for trend in trending_searches[:20]:
        if trend["traffic"] >= 50000:
            try:
                url_hash = hashlib.md5(
                    f"trends-ng-{trend['term']}-{TODAY}".encode()
                ).hexdigest()
                db.table("press_articles").upsert({
                    "url": f"https://trends.google.com/trends/trendingsearches/daily?geo=NG#{trend['term']}",
                    "url_hash": url_hash,
                    "publication": "google_trends_ng",
                    "headline": f"Trending in Nigeria: {trend['term']}",
                    "summary": (
                        f"~{trend['traffic']:,} searches today. "
                        + (f"Related news: {', '.join(trend['related'][:2])}" if trend['related'] else "")
                    ),
                    "sentiment_score": 0.0,
                    "sentiment_label": "neutral",
                    "published_at": datetime.now().isoformat(),
                    "scraped_at": datetime.now().isoformat(),
                }, on_conflict="url_hash").execute()
            except Exception as e:
                logger.debug(f"  Trend article save: {e}")

    return saved


# ─────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────

def run():
    start_time = datetime.now()
    logger.info(f"=== Starting {SCRAPER_NAME} ===")
    status = "failed"
    records_inserted = 0
    error_message = None
    artists = []

    try:
        db = get_supabase()
        artists = db.table("artists").select(
            "id, name, slug, tier"
        ).eq("is_active", True).order("tier").execute().data

        if not artists:
            logger.warning("No artists found")
            return

        logger.info(f"Processing {len(artists)} artists")

        # Layer 1: Nigeria RSS trending
        trending_searches = fetch_nigeria_trending_rss()
        rss_results = match_artists_in_rss(trending_searches, artists)

        # Layer 2: YouTube trending
        time.sleep(2)
        youtube_trending = fetch_youtube_trending_nigeria()
        youtube_results = match_artists_in_youtube(youtube_trending, artists)

        # Layer 3: Playwright Explore for each artist
        # Limit to Tier 1-2 artists daily (Tier 3-4 every 3 days)
        # to keep runtime reasonable
        explore_results = {}
        today_ordinal = date.today().toordinal()

        for artist in artists:
            tier = artist.get("tier", 2)
            slug = artist["slug"]

            # Tier 3-4: only run on every 3rd day
            if tier >= 3 and today_ordinal % 3 != 0:
                logger.debug(
                    f"  Skipping explore for Tier {tier}: {artist['name']}"
                )
                continue

            logger.info(
                f"  Fetching Explore data: {artist['name']} (Tier {tier})"
            )
            explore_data = fetch_artist_explore_data(
                artist["name"], geo="NG", timeframe="now 7-d"
            )

            if explore_data:
                explore_data["artist_id"] = artist["id"]
                explore_results[slug] = explore_data

                logger.info(
                    f"  {artist['name']}: "
                    f"avg={explore_data['avg_interest']:.1f} | "
                    f"peak={explore_data['peak_interest']:.1f} | "
                    f"momentum={explore_data['momentum_pct']:+.1f}% | "
                    f"rising={[q['term'] for q in explore_data['related_queries_rising'][:3]]}"
                )

            time.sleep(5)  # Respectful delay between artists

        # Show artists found in trending searches
        trending_artists = {
            s: d for s, d in rss_results.items()
            if d["appearances"] > 0
        }
        if trending_artists:
            logger.info("--- Artists in Nigeria Trending Searches ---")
            for slug, d in sorted(
                trending_artists.items(),
                key=lambda x: x[1]["total_traffic"],
                reverse=True,
            ):
                logger.info(
                    f"  {slug}: ~{d['total_traffic']:,} searches | "
                    f"{d['matched_terms']}"
                )
        else:
            logger.info(
                "No tracked artists in today's trending topics "
                "(normal — most artists aren't trending every day)"
            )

        if youtube_results:
            logger.info("--- Artists in YouTube Trending Nigeria ---")
            for slug, d in youtube_results.items():
                logger.info(
                    f"  {slug}: {d['count']} videos | "
                    f"{d['views']:,} views | '{d['top_title'][:50]}'"
                )

        # Save everything
        records_inserted = save_all_metrics(
            db,
            rss_results,
            youtube_results,
            explore_results,
            trending_searches,
        )

        status = "success"
        logger.success(
            f"Saved {records_inserted} metrics | "
            f"{len(trending_searches)} Nigeria trending topics | "
            f"{len(explore_results)} artist explore profiles | "
            f"{len(youtube_results)} artists on YouTube trending"
        )

    except Exception as e:
        error_message = str(e)
        logger.error(f"Crashed: {e}")
        import traceback
        logger.debug(traceback.format_exc())
    finally:
        duration = (datetime.now() - start_time).total_seconds()
        log_scraper_run(
            scraper_name=SCRAPER_NAME,
            status=status,
            records_attempted=len(artists),
            records_inserted=records_inserted,
            records_failed=0,
            error_message=error_message,
            duration_seconds=duration,
        )
        logger.info(f"Done. {status}. {duration:.1f}s")


if __name__ == "__main__":
    run()