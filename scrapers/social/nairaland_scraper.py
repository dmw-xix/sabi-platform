# scrapers/social/nairaland_scraper.py
import requests
from bs4 import BeautifulSoup
from datetime import datetime, date
from loguru import logger
from database.client import get_supabase, log_scraper_run

SCRAPER_NAME = "nairaland_music"
SOURCE = "nairaland"
TODAY = str(date.today())
BASE_URL = "https://www.nairaland.com"
MUSIC_URL = f"{BASE_URL}/entertainment/music"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    )
}


def scrape_music_section() -> list[dict]:
    """Scrapes Nairaland music section for threads mentioning artists."""
    threads = []
    
    try:
        response = requests.get(
            MUSIC_URL, headers=HEADERS, timeout=20
        )
        soup = BeautifulSoup(response.text, "lxml")
        
        # Find thread rows
        thread_rows = soup.find_all("tr", class_=["odd", "even"])
        
        for row in thread_rows:
            try:
                title_cell = row.find("td", class_="bold")
                if not title_cell:
                    continue
                
                link = title_cell.find("a")
                if not link:
                    continue
                
                title = link.get_text(strip=True)
                thread_url = BASE_URL + link.get("href", "")
                
                # Get reply count
                cells = row.find_all("td")
                reply_count = 0
                for cell in cells:
                    text = cell.get_text(strip=True)
                    if text.isdigit() and int(text) > 0:
                        reply_count = int(text)
                        break
                
                threads.append({
                    "title": title,
                    "url": thread_url,
                    "reply_count": reply_count,
                })
                
            except Exception as e:
                logger.debug(f"Thread parse error: {e}")
                continue
        
        logger.info(f"Found {len(threads)} threads on Nairaland music")
        
    except Exception as e:
        logger.error(f"Nairaland scraping error: {e}")
    
    return threads


def match_artists_in_threads(
    threads: list[dict], artists: list[dict]
) -> list[dict]:
    """Find which artists are mentioned in thread titles."""
    matches = []
    
    for thread in threads:
        title_lower = thread["title"].lower()
        
        for artist in artists:
            artist_name_lower = artist["name"].lower()
            
            # Check for artist name in title
            if artist_name_lower in title_lower:
                matches.append({
                    "artist_id": artist["id"],
                    "artist_name": artist["name"],
                    "thread_title": thread["title"],
                    "thread_url": thread["url"],
                    "reply_count": thread["reply_count"],
                    "source": SOURCE,
                })
    
    return matches


def save_nairaland_mentions(db, matches: list[dict]) -> int:
    """Save Nairaland mentions as social signals."""
    saved = 0
    
    # Aggregate by artist
    artist_mentions = {}
    for m in matches:
        aid = m["artist_id"]
        if aid not in artist_mentions:
            artist_mentions[aid] = {
                "count": 0,
                "total_replies": 0,
                "threads": []
            }
        artist_mentions[aid]["count"] += 1
        artist_mentions[aid]["total_replies"] += m["reply_count"]
        artist_mentions[aid]["threads"].append(m["thread_title"])
    
    for artist_id, data in artist_mentions.items():
        try:
            db.table("social_mentions").insert({
                "artist_id": artist_id,
                "platform": SOURCE,
                "mention_count": data["count"],
                "positive_count": 0,  # Would need NLP to determine
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
                f"  Nairaland: {data['count']} threads for artist, "
                f"{data['total_replies']} total replies"
            )
        except Exception as e:
            logger.error(f"  DB error saving nairaland data: {e}")
    
    return saved


def run():
    start_time = datetime.now()
    logger.info(f"=== Starting {SCRAPER_NAME} ===")
    status = "failed"
    records_inserted = 0
    error_message = None
    
    try:
        db = get_supabase()
        
        artists = db.table("artists").select(
            "id, name, slug"
        ).eq("is_active", True).execute().data
        
        threads = scrape_music_section()
        
        if threads:
            matches = match_artists_in_threads(threads, artists)
            logger.info(
                f"Found {len(matches)} artist mentions in "
                f"{len(threads)} threads"
            )
            
            for m in matches[:10]:
                logger.info(
                    f"  {m['artist_name']}: '{m['thread_title']}' "
                    f"({m['reply_count']} replies)"
                )
            
            records_inserted = save_nairaland_mentions(db, matches)
        
        status = "success"
        logger.success(f"Complete: {records_inserted} artist mention sets saved")
        
    except Exception as e:
        error_message = str(e)
        logger.error(f"Crashed: {e}")
    finally:
        duration = (datetime.now() - start_time).total_seconds()
        log_scraper_run(
            scraper_name=SCRAPER_NAME, status=status,
            records_attempted=len(threads) if 'threads' in locals() else 0,
            records_inserted=records_inserted, records_failed=0,
            error_message=error_message, duration_seconds=duration
        )
        logger.info(f"Done. {status}. {duration:.1f}s")


if __name__ == "__main__":
    run()