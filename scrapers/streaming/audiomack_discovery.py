# scrapers/streaming/audiomack_discovery.py
# Automatically discovers emerging Nigerian artists from Audiomack trending
# Writes candidates to discovery_queue table for human review
# This is how Sabi finds the next Asake before anyone else does

import re
import time
from datetime import datetime
from loguru import logger
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout
from database.client import get_supabase, log_scraper_run

SCRAPER_NAME = "audiomack_discovery"
TRENDING_URL = "https://audiomack.com/trending/nigeria"

HEADERS_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)


def scrape_trending(page) -> list[dict]:
    """Scrapes Audiomack trending Nigeria for artist names."""
    candidates = []

    try:
        logger.info(f"Loading: {TRENDING_URL}")
        page.goto(
            TRENDING_URL,
            wait_until="domcontentloaded",
            timeout=60000
        )
        time.sleep(5)
        page.screenshot(path="logs/audiomack_trending_debug.png")

        page_text = page.inner_text("body")
        lines = [l.strip() for l in page_text.split('\n') if l.strip()]

        skip = {
            "trending", "nigeria", "all", "top tracks", "albums",
            "playlists", "re-ups", "likes", "sign in", "upload",
            "discover", "originals", "create an account", "follow",
            "get plus", "about", "help", "load more",
        }

        skip_starts = [
            "member since", "@", "#", "album:", "release date:",
            "by\xa0", "label:", "genre:", "©", "audiomack is",
        ]

        # Extract potential artist names
        # On trending page, artist names appear before track titles
        i = 0
        while i < len(lines):
            line = lines[i]
            lower = line.lower().strip()

            if (
                lower not in skip
                and not any(lower.startswith(s) for s in skip_starts)
                and len(line) > 1
                and len(line) < 60
                and not line.isdigit()
                and not re.match(r'^\d+[\.,]\d+[KMB]?$', line)
            ):
                candidates.append({
                    "name": line,
                    "source": "audiomack_trending",
                    "raw_line_index": i,
                })

            i += 1

        logger.info(f"Found {len(candidates)} candidate lines")

    except PlaywrightTimeout:
        logger.error("Timeout loading Audiomack trending")
    except Exception as e:
        logger.error(f"Scraping error: {e}")

    return candidates


def filter_new_artists(
    db, candidates: list[dict]
) -> list[dict]:
    """
    Compares candidates against existing artists table.
    Returns only candidates NOT already tracked.
    Also checks discovery_queue to avoid duplicates.
    """
    # Get all existing artist names (lowercase)
    existing = db.table("artists").select("name").execute()
    existing_names = {
        a["name"].lower() for a in existing.data
    }

    # Get already-queued names
    queued = db.table("discovery_queue").select(
        "name"
    ).execute()
    queued_names = {
        q["name"].lower() for q in queued.data
    }

    known_names = existing_names | queued_names

    new_candidates = []
    seen = set()

    for c in candidates:
        name_lower = c["name"].lower()
        if (
            name_lower not in known_names
            and name_lower not in seen
            and len(c["name"]) > 2
        ):
            new_candidates.append(c)
            seen.add(name_lower)

    logger.info(
        f"New candidates (not in DB): {len(new_candidates)}"
    )
    for c in new_candidates[:10]:
        logger.info(f"  → {c['name']}")

    return new_candidates


def save_to_discovery_queue(
    db, candidates: list[dict]
) -> int:
    """Save new artist candidates to discovery_queue."""
    saved = 0
    for c in candidates:
        try:
            db.table("discovery_queue").insert({
                "name": c["name"],
                "source": c["source"],
                "signal_strength": None,
                "raw_data": c,
                "discovered_at": datetime.now().isoformat(),
                "status": "pending",
            }).execute()
            saved += 1
        except Exception as e:
            logger.debug(f"Queue insert error for {c['name']}: {e}")
    return saved


def run():
    start_time = datetime.now()
    logger.info(f"=== Starting {SCRAPER_NAME} ===")
    status = "failed"
    records_inserted = 0
    error_message = None

    try:
        db = get_supabase()

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(
                user_agent=HEADERS_UA,
                viewport={"width": 1280, "height": 900},
            )
            page = context.new_page()

            candidates = scrape_trending(page)
            browser.close()

        if candidates:
            new_artists = filter_new_artists(db, candidates)
            records_inserted = save_to_discovery_queue(db, new_artists)
            logger.success(
                f"{records_inserted} new artist candidates "
                "added to discovery queue"
            )

        status = "success"

    except Exception as e:
        error_message = str(e)
        logger.error(f"Crashed: {e}")
    finally:
        duration = (datetime.now() - start_time).total_seconds()
        log_scraper_run(
            scraper_name=SCRAPER_NAME, status=status,
            records_attempted=0,
            records_inserted=records_inserted, records_failed=0,
            error_message=error_message, duration_seconds=duration
        )
        logger.info(f"Done. {status}. {duration:.1f}s")


if __name__ == "__main__":
    run()