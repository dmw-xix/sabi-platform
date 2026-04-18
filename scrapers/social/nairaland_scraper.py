# scrapers/social/nairaland_scraper.py
# Scrapes Nairaland music section for artist mentions
# Street-level Nigerian sentiment — earliest cultural signal

import requests
from bs4 import BeautifulSoup
from datetime import datetime, date, timedelta
from loguru import logger
from database.client import get_supabase, log_scraper_run

SCRAPER_NAME = "nairaland_music"
SOURCE = "nairaland"
TODAY = str(date.today())

# Multiple entry points — Nairaland has several music-related boards
NAIRALAND_URLS = [
    "https://www.nairaland.com/entertainment/music",
    "https://www.nairaland.com/music-radio/new"
    "https://www.nairaland.com/music-radio",
]

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
    "Accept-Encoding": "gzip, deflate",
    "Connection": "keep-alive",
}


def scrape_nairaland_threads(url: str) -> list[dict]:
    """
    Scrapes Nairaland for thread titles and reply counts.
    Nairaland renders plain HTML — no JS needed.
    """
    threads = []

    try:
        response = requests.get(url, headers=HEADERS, timeout=20)
        response.raise_for_status()

        soup = BeautifulSoup(response.text, "lxml")

        # Nairaland thread structure:
        # Each board page has a <table> with rows containing thread links
        # Thread links are <a> tags inside <b> tags or <td class="bold">

        # Method 1: Find all bold links (thread titles)
        bold_links = soup.find_all("b")
        for bold in bold_links:
            link = bold.find("a")
            if not link:
                continue

            href = link.get("href", "")
            title = link.get_text(strip=True)

            # Nairaland thread URLs: /BOARDID/THREAD-SLUG/THREADID
            if (
                href.startswith("/")
                and href.count("/") >= 2
                and title
                and len(title) > 5
                and len(title) < 200
            ):
                # Try to find reply count near this element
                reply_count = 0
                parent_row = bold.find_parent("tr")
                if parent_row:
                    cells = parent_row.find_all("td")
                    for cell in cells:
                        text = cell.get_text(strip=True).replace(",", "")
                        if text.isdigit():
                            reply_count = max(reply_count, int(text))

                threads.append({
                    "title": title,
                    "url": f"https://www.nairaland.com{href}",
                    "reply_count": reply_count,
                })

        # Method 2: Direct table scanning if method 1 gives nothing
        if not threads:
            tables = soup.find_all("table")
            for table in tables:
                rows = table.find_all("tr")
                for row in rows:
                    cells = row.find_all("td")
                    if len(cells) < 2:
                        continue

                    # Look for a link in the first or second cell
                    for cell in cells[:3]:
                        link = cell.find("a")
                        if not link:
                            continue

                        href = link.get("href", "")
                        title = link.get_text(strip=True)

                        if (
                            href.startswith("/")
                            and len(title) > 5
                            and len(title) < 200
                            and "nairaland.com" not in title.lower()
                        ):
                            reply_count = 0
                            for c in cells:
                                txt = c.get_text(strip=True).replace(",", "")
                                if txt.isdigit() and int(txt) < 100000:
                                    reply_count = max(reply_count, int(txt))

                            threads.append({
                                "title": title,
                                "url": f"https://www.nairaland.com{href}",
                                "reply_count": reply_count,
                            })
                            break

        logger.info(
            f"  Scraped {len(threads)} threads from {url}"
        )

    except requests.RequestException as e:
        logger.error(f"  Request error for {url}: {e}")
    except Exception as e:
        logger.error(f"  Parse error for {url}: {e}")
        import traceback
        logger.debug(traceback.format_exc())

    return threads


def match_artists(
    threads: list[dict], artists: list[dict]
) -> list[dict]:
    """Find artist mentions in thread titles."""
    matches = []

    for thread in threads:
        title_lower = thread["title"].lower()

        for artist in artists:
            name_lower = artist["name"].lower()

            # Also check common name variations
            name_parts = name_lower.split()
            primary_name = name_parts[0] if name_parts else name_lower

            if name_lower in title_lower or (
                len(primary_name) > 3
                and primary_name in title_lower
            ):
                matches.append({
                    "artist_id": artist["id"],
                    "artist_name": artist["name"],
                    "thread_title": thread["title"],
                    "thread_url": thread["url"],
                    "reply_count": thread["reply_count"],
                })

    return matches


def save_mentions(db, matches: list[dict]) -> int:
    """Aggregate and save Nairaland mentions to social_mentions."""
    saved = 0

    # Group by artist
    artist_data: dict = {}
    for m in matches:
        aid = m["artist_id"]
        if aid not in artist_data:
            artist_data[aid] = {
                "count": 0,
                "total_replies": 0,
                "threads": [],
            }
        artist_data[aid]["count"] += 1
        artist_data[aid]["total_replies"] += m["reply_count"]
        artist_data[aid]["threads"].append(m["thread_title"])

    for artist_id, data in artist_data.items():
        try:
            db.table("social_mentions").insert({
                "artist_id": artist_id,
                "platform": SOURCE,
                "mention_count": data["count"],
                "positive_count": 0,
                "negative_count": 0,
                "neutral_count": data["count"],
                "avg_sentiment_score": 0,
                "top_keywords": data["threads"][:3],
                "sample_content": (
                    data["threads"][0]
                    if data["threads"] else None
                ),
                "captured_at": datetime.now().isoformat(),
                "window_hours": 24,
            }).execute()
            saved += 1
            logger.info(
                f"  Nairaland: {data['count']} threads | "
                f"{data['total_replies']} replies"
            )
        except Exception as e:
            logger.error(f"  DB save error: {e}")

    return saved


def run():
    start_time = datetime.now()
    logger.info(f"=== Starting {SCRAPER_NAME} ===")
    status = "failed"
    records_inserted = 0
    error_message = None
    all_threads = []

    try:
        db = get_supabase()

        artists = db.table("artists").select(
            "id, name, slug"
        ).eq("is_active", True).execute().data

        # Scrape all Nairaland music URLs
        for url in NAIRALAND_URLS:
            threads = scrape_nairaland_threads(url)
            all_threads.extend(threads)

        # Deduplicate by URL
        seen_urls = set()
        unique_threads = []
        for t in all_threads:
            if t["url"] not in seen_urls:
                unique_threads.append(t)
                seen_urls.add(t["url"])

        logger.info(
            f"Total unique threads: {len(unique_threads)}"
        )

        if unique_threads:
            matches = match_artists(unique_threads, artists)
            logger.info(
                f"Artist mentions found: {len(matches)}"
            )

            for m in matches[:10]:
                logger.info(
                    f"  {m['artist_name']}: "
                    f"'{m['thread_title'][:60]}' "
                    f"({m['reply_count']} replies)"
                )

            if matches:
                records_inserted = save_mentions(db, matches)

        status = "success"
        logger.success(
            f"Complete: {records_inserted} mention sets saved"
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
            records_attempted=len(all_threads),
            records_inserted=records_inserted,
            records_failed=0,
            error_message=error_message,
            duration_seconds=duration,
        )
        logger.info(f"Done. {status}. {duration:.1f}s")


if __name__ == "__main__":
    run()