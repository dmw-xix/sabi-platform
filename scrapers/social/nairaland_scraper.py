# scrapers/social/nairaland_scraper.py
import re
import time
import requests
from bs4 import BeautifulSoup
from datetime import datetime
from loguru import logger
from database.client import get_supabase, log_scraper_run

SCRAPER_NAME = "nairaland_music"
SOURCE = "nairaland"
TODAY = str(__import__('datetime').date.today())

NAIRALAND_URLS = [
    "https://www.nairaland.com/entertainment/music",
    "https://www.nairaland.com/entertainment/music/1",
]

# Nairaland requires a session with cookies to avoid 403
def make_session() -> requests.Session:
    session = requests.Session()
    session.headers.update({
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/122.0.0.0 Safari/537.36"
        ),
        "Accept": (
            "text/html,application/xhtml+xml,application/xml;"
            "q=0.9,image/avif,image/webp,*/*;q=0.8"
        ),
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate, br",
        "Connection": "keep-alive",
        "Upgrade-Insecure-Requests": "1",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "none",
        "Cache-Control": "max-age=0",
    })
    # First hit the homepage to get cookies
    try:
        session.get("https://www.nairaland.com", timeout=15)
        time.sleep(2)
    except Exception:
        pass
    return session


def scrape_nairaland_threads(session: requests.Session, url: str) -> list[dict]:
    threads = []
    try:
        response = session.get(url, timeout=20)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, "lxml")

        # Nairaland thread links are inside <b> tags within table rows
        # Structure: <td class="bold"><b><a href="/THREADID/thread-title">Title</a></b>
        for b_tag in soup.find_all("b"):
            link = b_tag.find("a")
            if not link:
                continue
            href = link.get("href", "")
            title = link.get_text(strip=True)

            # Valid thread URLs have format /digits/slug
            if not re.match(r'^/\d+/', href):
                continue
            if not title or len(title) < 5 or len(title) > 200:
                continue

            # Get reply count from parent row
            reply_count = 0
            parent = b_tag.find_parent("tr")
            if parent:
                for td in parent.find_all("td"):
                    txt = td.get_text(strip=True).replace(",", "")
                    if txt.isdigit() and 0 < int(txt) < 500000:
                        reply_count = max(reply_count, int(txt))

            threads.append({
                "title": title,
                "url": f"https://www.nairaland.com{href}",
                "reply_count": reply_count,
            })

        logger.info(f"  Scraped {len(threads)} threads from {url}")

    except requests.HTTPError as e:
        logger.error(f"  HTTP error for {url}: {e}")
    except Exception as e:
        logger.error(f"  Parse error for {url}: {e}")

    return threads


def match_artists(threads: list[dict], artists: list[dict]) -> list[dict]:
    matches = []
    for thread in threads:
        title_lower = thread["title"].lower()
        for artist in artists:
            name_lower = artist["name"].lower()
            # Match full name or first word (min 4 chars to avoid false positives)
            first_word = name_lower.split()[0] if name_lower.split() else ""
            if name_lower in title_lower or (
                len(first_word) >= 4 and first_word in title_lower
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
    saved = 0
    grouped: dict = {}
    for m in matches:
        aid = m["artist_id"]
        if aid not in grouped:
            grouped[aid] = {"count": 0, "total_replies": 0, "threads": []}
        grouped[aid]["count"] += 1
        grouped[aid]["total_replies"] += m["reply_count"]
        grouped[aid]["threads"].append(m["thread_title"])

    for artist_id, data in grouped.items():
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
                "sample_content": data["threads"][0] if data["threads"] else None,
                "captured_at": datetime.now().isoformat(),
                "window_hours": 24,
            }).execute()
            saved += 1
            logger.info(
                f"  {artist_id}: {data['count']} threads, "
                f"{data['total_replies']} total replies"
            )
        except Exception as e:
            logger.error(f"  DB error: {e}")
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

        session = make_session()

        for url in NAIRALAND_URLS:
            threads = scrape_nairaland_threads(session, url)
            all_threads.extend(threads)
            time.sleep(3)

        # Deduplicate by URL
        seen = set()
        unique = []
        for t in all_threads:
            if t["url"] not in seen:
                unique.append(t)
                seen.add(t["url"])

        logger.info(f"Total unique threads: {len(unique)}")

        if unique:
            matches = match_artists(unique, artists)
            logger.info(f"Artist mentions: {len(matches)}")
            for m in matches[:10]:
                logger.info(
                    f"  {m['artist_name']}: '{m['thread_title'][:60]}' "
                    f"({m['reply_count']} replies)"
                )
            if matches:
                records_inserted = save_mentions(db, matches)

        status = "success"
        logger.success(f"Complete: {records_inserted} mention sets saved")

    except Exception as e:
        error_message = str(e)
        logger.error(f"Crashed: {e}")
    finally:
        duration = (datetime.now() - start_time).total_seconds()
        log_scraper_run(
            scraper_name=SCRAPER_NAME, status=status,
            records_attempted=len(all_threads),
            records_inserted=records_inserted, records_failed=0,
            error_message=error_message, duration_seconds=duration,
        )
        logger.info(f"Done. {status}. {duration:.1f}s")


if __name__ == "__main__":
    run()