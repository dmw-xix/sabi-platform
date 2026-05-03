# scrapers/social/twitter_trends_scraper.py
# Scrapes trends24.in for X (Twitter) trending topics in Nigeria
# Matches trending terms against tracked artists
# No API key required — public HTML
# Source: trends24.in/nigeria/ and trends24.in/nigeria/lagos/
#
# What this gives us:
# - Which artists are trending on X Nigeria right now
# - Hourly trend snapshots (24hr history available)
# - Lagos-specific trends (biggest music market in Nigeria)
# - Context: what's driving the trend (related terms)

import re
import time
import hashlib
import requests
from bs4 import BeautifulSoup
from datetime import datetime, date
from loguru import logger
from database.client import get_supabase, log_scraper_run

SCRAPER_NAME = "twitter_trends_ng"
SOURCE = "twitter_trends"
TODAY = str(date.today())

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://trends24.in/",
}

# Multiple geographic scopes for Nigerian music market
TREND_URLS = {
    "nigeria":   "https://trends24.in/nigeria/",
    "lagos":     "https://trends24.in/nigeria/lagos/",
}


def scrape_trends24(url: str, location: str) -> list[dict]:
    """
    Scrapes trends24.in for a given location.
    Returns list of trending topics with rank and context.

    Trends24 structure:
    - Shows 24 hourly snapshots on one page
    - Most recent snapshot is first
    - Each trend card has: rank, term, tweet count
    """
    trends = []

    try:
        logger.info(f"  Fetching {location}: {url}")
        response = requests.get(url, headers=HEADERS, timeout=20)
        response.raise_for_status()

        soup = BeautifulSoup(response.text, "lxml")

        # Trends24 wraps each hourly snapshot in a trend-card div
        # The first card = most recent hour
        trend_cards = soup.find_all("div", class_="trend-card")

        if not trend_cards:
            # Try alternate selectors
            trend_cards = soup.find_all(
                "div", class_=re.compile(r"trend")
            )

        if not trend_cards:
            logger.warning(f"  No trend cards found at {url}")
            # Debug: log first 500 chars of HTML
            logger.debug(f"  HTML preview: {response.text[:500]}")
            return []

        # Take only the most recent snapshot (first card)
        latest_card = trend_cards[0]

        # Extract timestamp of this snapshot
        snapshot_time = None
        time_elem = latest_card.find(
            ["time", "span"], class_=re.compile(r"time|hour|date")
        )
        if time_elem:
            snapshot_time = time_elem.get("datetime") or time_elem.get_text(strip=True)

        # Extract trend list items
        trend_list = latest_card.find("ol") or latest_card.find("ul")

        if not trend_list:
            # Try finding list items directly
            items = latest_card.find_all("li")
        else:
            items = trend_list.find_all("li")

        logger.info(f"  Found {len(items)} trending topics ({location})")

        for rank, item in enumerate(items, 1):
            # Extract trend name
            link = item.find("a")
            if not link:
                continue

            term = link.get_text(strip=True)
            if not term:
                continue

            # Extract tweet/post count if available
            count_text = ""
            count_elem = item.find(
                ["span", "p"],
                class_=re.compile(r"count|tweet|post|number")
            )
            if count_elem:
                count_text = count_elem.get_text(strip=True)

            tweet_count = _parse_count(count_text)

            trends.append({
                "rank": rank,
                "term": term,
                "tweet_count": tweet_count,
                "location": location,
                "snapshot_time": snapshot_time or datetime.now().isoformat(),
                "url": url,
            })

            if rank <= 10:
                logger.debug(
                    f"  #{rank}: {term}"
                    + (f" ({tweet_count:,} posts)" if tweet_count else "")
                )

    except requests.HTTPError as e:
        logger.error(f"  HTTP error for {url}: {e}")
    except Exception as e:
        logger.error(f"  Scrape error for {url}: {e}")
        import traceback
        logger.debug(traceback.format_exc())

    return trends


def _parse_count(text: str) -> int:
    """Parse count strings: '12.5K' → 12500, '1.2M' → 1200000"""
    if not text:
        return 0
    text = str(text).strip().replace(",", "").replace("+", "")
    try:
        if text.upper().endswith("M"):
            return int(float(text[:-1]) * 1_000_000)
        elif text.upper().endswith("K"):
            return int(float(text[:-1]) * 1_000)
        return int(float(text)) if text else 0
    except ValueError:
        return 0


def get_all_trends() -> list[dict]:
    """Fetch trends from all Nigerian locations and merge."""
    all_trends = []
    seen_terms = set()

    for location, url in TREND_URLS.items():
        trends = scrape_trends24(url, location)
        for t in trends:
            term_lower = t["term"].lower()
            # Mark if appearing in multiple locations
            if term_lower in seen_terms:
                t["multi_location"] = True
            else:
                seen_terms.add(term_lower)
                t["multi_location"] = False
            all_trends.append(t)
        time.sleep(2)

    return all_trends


def match_artists(
    trends: list[dict],
    artists: list[dict]
) -> dict:
    """
    Match trending terms against tracked artists.
    Also checks Twitter handles for direct mentions.

    Returns {artist_slug: match_data}
    """
    matches = {}

    for artist in artists:
        name_lower = artist["name"].lower()
        handle = (artist.get("twitter_handle") or "").lower().lstrip("@")
        slug = artist["slug"]

        # Name parts for partial matching (min 4 chars to avoid false positives)
        name_parts = [
            p for p in name_lower.split()
            if len(p) >= 4
        ]

        artist_matches = []

        for trend in trends:
            term_lower = trend["term"].lower().lstrip("#").lstrip("@")

            matched = False

            # Exact full name match
            if name_lower in term_lower or term_lower in name_lower:
                matched = True

            # Twitter handle match
            elif handle and (
                handle in term_lower or term_lower == handle
            ):
                matched = True

            # Partial name match (first meaningful word)
            elif any(part in term_lower for part in name_parts):
                matched = True

            if matched:
                artist_matches.append(trend)

        if artist_matches:
            # Best rank = lowest number (highest position)
            best_rank = min(t["rank"] for t in artist_matches)
            locations = list({t["location"] for t in artist_matches})
            multi_location = any(t["multi_location"] for t in artist_matches)
            total_tweets = sum(
                t["tweet_count"] for t in artist_matches
            )

            matches[slug] = {
                "artist_id": artist["id"],
                "artist_name": artist["name"],
                "best_rank": best_rank,
                "locations": locations,
                "multi_location": multi_location,
                "total_tweets": total_tweets,
                "matched_terms": [t["term"] for t in artist_matches],
                "trend_count": len(artist_matches),
            }

            logger.info(
                f"  🔥 {artist['name']}: trending at #{best_rank} "
                f"in {', '.join(locations)}"
                + (" [MULTI-LOCATION]" if multi_location else "")
                + (f" | ~{total_tweets:,} posts" if total_tweets else "")
            )

    return matches


def save_metrics(
    db,
    artist_matches: dict,
    all_trends: list[dict]
) -> int:
    """Save Twitter trend metrics to database."""
    saved = 0

    # Save per-artist metrics
    for slug, data in artist_matches.items():
        artist_id = data["artist_id"]

        # Trending rank score: #1 = 100pts, #50 = 50pts, not trending = 0
        rank_score = max(0.0, 100.0 - data["best_rank"] * 2)

        # Multi-location bonus
        if data["multi_location"]:
            rank_score = min(100.0, rank_score * 1.3)

        metrics = {
            "twitter_trending_rank": float(data["best_rank"]),
            "twitter_trending_score": round(rank_score, 2),
            "twitter_trending_locations": float(len(data["locations"])),
            "twitter_tweet_count": float(data["total_tweets"]),
        }

        for metric_name, value in metrics.items():
            try:
                db.table("artist_snapshots").upsert({
                    "artist_id": artist_id,
                    "source": SOURCE,
                    "metric_name": metric_name,
                    "metric_value": value,
                    "snapshot_date": TODAY,
                    "captured_at": datetime.now().isoformat(),
                }, on_conflict="artist_id,source,metric_name,snapshot_date").execute()
                saved += 1
            except Exception as e:
                logger.error(f"  DB error {slug}/{metric_name}: {e}")

        # Save as social mention for press/signal context
        try:
            db.table("social_mentions").insert({
                "artist_id": artist_id,
                "platform": "twitter",
                "mention_count": data["trend_count"],
                "positive_count": 0,
                "negative_count": 0,
                "neutral_count": data["trend_count"],
                "avg_sentiment_score": 0,
                "top_keywords": data["matched_terms"][:5],
                "sample_content": (
                    f"Trending at #{data['best_rank']} on X Nigeria "
                    f"({', '.join(data['locations'])})"
                ),
                "captured_at": datetime.now().isoformat(),
                "window_hours": 1,
            }).execute()
        except Exception as e:
            logger.debug(f"  Social mention save: {e}")

    # Save raw trending topics as press context
    nigeria_trends = [t for t in all_trends if t["location"] == "nigeria"]
    for trend in nigeria_trends[:30]:
        try:
            url_hash = hashlib.md5(
                f"twitter-trends-ng-{trend['term']}-{TODAY}-{trend['rank']}".encode()
            ).hexdigest()

            db.table("press_articles").upsert({
                "url": f"https://trends24.in/nigeria/#{trend['term']}",
                "url_hash": url_hash,
                "publication": "twitter_trends_ng",
                "headline": (
                    f"Trending on X Nigeria #{trend['rank']}: "
                    f"{trend['term']}"
                ),
                "summary": (
                    f"Ranked #{trend['rank']} on X Nigeria trends"
                    + (f" and Lagos trends" if any(
                        t["term"] == trend["term"]
                        for t in all_trends
                        if t["location"] == "lagos"
                    ) else "")
                    + (f". ~{trend['tweet_count']:,} posts" if trend["tweet_count"] else "")
                ),
                "sentiment_score": 0.0,
                "sentiment_label": "neutral",
                "published_at": datetime.now().isoformat(),
                "scraped_at": datetime.now().isoformat(),
            }, on_conflict="url_hash").execute()
        except Exception as e:
            logger.debug(f"  Trend article save: {e}")

    return saved


def flag_breakout_signals(db, artist_matches: dict):
    """
    Flag artists trending on X as breakout signals
    if they're also showing chart momentum.
    High Twitter trending = potential viral moment.
    """
    for slug, data in artist_matches.items():
        # Only flag if top 20 on Nigeria OR top 10 on Lagos
        is_significant = (
            data["best_rank"] <= 20
            or (data["multi_location"] and data["best_rank"] <= 30)
        )

        if not is_significant:
            continue

        try:
            strength = max(0.0, min(100.0, float(
                100 - data["best_rank"] * 2
                + (15 if data["multi_location"] else 0)
            )))

            db.table("breakout_signals").insert({
                "artist_id": data["artist_id"],
                "signal_type": "twitter_trending",
                "strength": strength,
                "sources": ["twitter_trends"],
                "signal_data": {
                    "best_rank": data["best_rank"],
                    "locations": data["locations"],
                    "multi_location": data["multi_location"],
                    "matched_terms": data["matched_terms"],
                    "tweet_count": data["total_tweets"],
                    "summary": (
                        f"{data['artist_name']} is trending at "
                        f"#{data['best_rank']} on X Nigeria"
                        + (" and Lagos" if data["multi_location"] else "")
                        + f" with terms: {', '.join(data['matched_terms'][:3])}"
                    ),
                },
                "detected_at": datetime.now().isoformat(),
                "status": "active",
            }).execute()

            logger.success(
                f"  Breakout signal: {data['artist_name']} "
                f"trending at #{data['best_rank']} "
                f"(strength: {strength:.0f})"
            )
        except Exception as e:
            logger.debug(f"  Breakout signal save: {e}")


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
            "id, name, slug, tier, twitter_handle"
        ).eq("is_active", True).execute().data

        logger.info(f"Checking X trends for {len(artists)} artists")

        # Fetch all trends
        all_trends = get_all_trends()

        if not all_trends:
            logger.warning("No trends fetched — check site structure")
            status = "partial"
        else:
            # Show top 10 Nigeria trends
            nigeria_top = sorted(
                [t for t in all_trends if t["location"] == "nigeria"],
                key=lambda x: x["rank"]
            )[:10]

            logger.info("--- Top 10 Trending on X Nigeria ---")
            for t in nigeria_top:
                logger.info(
                    f"  #{t['rank']:>2}: {t['term']}"
                    + (f" ({t['tweet_count']:,})" if t["tweet_count"] else "")
                )

            # Match against artists
            logger.info("")
            logger.info("--- Matching artists against trends ---")
            artist_matches = match_artists(all_trends, artists)

            if not artist_matches:
                logger.info(
                    "No tracked artists in current trends "
                    "(check back — trends change hourly)"
                )
            else:
                logger.info(
                    f"Found {len(artist_matches)} tracked artists trending"
                )

            # Save metrics and signals
            records_inserted = save_metrics(db, artist_matches, all_trends)
            flag_breakout_signals(db, artist_matches)

            status = "success"
            logger.success(
                f"Complete: {records_inserted} metrics saved | "
                f"{len(all_trends)} trends fetched | "
                f"{len(artist_matches)} artists matched"
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