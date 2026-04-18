# scrapers/streaming/audiomack_discovery.py
import re
import time
from datetime import datetime
from loguru import logger
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout
from database.client import get_supabase, log_scraper_run

SCRAPER_NAME = "audiomack_discovery"

# Try multiple URLs — Audiomack restructures pages occasionally
TRENDING_URLS = [
    "https://audiomack.com/trending-now/songs/afrobeats",
    "https://audiomack.com/trending/nigeria",
    "https://audiomack.com/genre/afrobeats?country=Nigeria",
]

HEADERS_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)

# Exhaustive skip list — anything that is not an artist name
SKIP_EXACT = {
    "trending", "nigeria", "all", "top tracks", "albums", "playlists",
    "re-ups", "likes", "sign in", "upload", "discover", "originals",
    "create an account", "follow", "get plus", "about", "help",
    "load more", "load more top tracks", "songs", "music", "artists",
    "home", "search", "notifications", "messages", "library",
    "page not found", "discover trending songs", "not found",
    "error", "404", "afrobeats", "afropop", "hip-hop", "r&b",
    "dancehall", "gospel", "highlife", "genre", "country",
    "most played", "recent", "featured", "new releases",
    "top songs", "top artists", "weekly", "daily", "monthly",
    "audiomack", "sign up", "log in", "login", "register",
    "privacy policy", "terms of service", "copyright",
    "business inquiries", "styleguide", "creator app",
    "legal & dmca", "report a vulnerability",
    "do not sell my info", "your privacy rights",
    "audiomack is an on-demand music streaming",
    "top tracks", "recent tracks",
}

SKIP_STARTS = [
    "member since", "audiomack is", "http", "@", "#",
    "album:", "release date:", "by\xa0", "by ", "label:",
    "genre:", "©", "feat.", "ft.", "prod.", "produced by",
    "all rights reserved", "cookie", "privacy", "terms",
    "discover", "page not",
]

# Must look like a real artist name:
# - Has at least one letter
# - Not too long
# - Not a pure number
# - Not navigation text
def is_valid_artist_name(text: str) -> bool:
    if not text or len(text) < 2 or len(text) > 50:
        return False
    lower = text.lower().strip()
    if lower in SKIP_EXACT:
        return False
    if any(lower.startswith(s) for s in SKIP_STARTS):
        return False
    if text.isdigit():
        return False
    # Must contain at least one alphabetic character
    if not any(c.isalpha() for c in text):
        return False
    # Skip if it looks like a number with suffix (3.4M, 10K etc)
    if re.match(r'^\d+[\.,]\d+[KMBkmb]?$', text):
        return False
    if re.match(r'^\d+[KMBkmb]$', text):
        return False
    # Skip dates
    months = [
        "january", "february", "march", "april", "may", "june",
        "july", "august", "september", "october", "november", "december",
    ]
    if any(lower.startswith(m) for m in months):
        return False
    return True


def find_working_url(page) -> str | None:
    """Try trending URLs until one loads actual content."""
    for url in TRENDING_URLS:
        try:
            logger.info(f"  Trying: {url}")
            page.goto(
                url,
                wait_until="domcontentloaded",
                timeout=45000
            )
            time.sleep(4)

            # Check if we got real content (not a 404)
            page_text = page.inner_text("body")
            if (
                "page not found" not in page_text.lower()
                and len(page_text) > 500
            ):
                logger.info(f"  Working URL: {url}")
                return url
        except Exception as e:
            logger.debug(f"  URL failed {url}: {e}")
            continue
    return None


def extract_artist_names_from_links(page) -> list[str]:
    """
    Extract artist names from actual artist profile links on the page.
    Audiomack artist pages follow the pattern: /artistslug
    This is more reliable than parsing text.
    """
    names = []
    seen_slugs = set()

    try:
        # Look for artist profile links
        links = page.query_selector_all("a[href]")
        for link in links:
            href = link.get_attribute("href") or ""

            # Audiomack artist slugs: /artistname (one level deep, no subpath)
            # Exclude: /song/, /album/, /playlist/, /trending, etc.
            if (
                href.startswith("/")
                and href.count("/") == 1
                and len(href) > 2
                and not any(
                    skip in href for skip in [
                        "song", "album", "playlist", "trending",
                        "genre", "search", "notifications",
                        "login", "signup", "upload", "discover",
                        "originals", "about", "help", "legal",
                    ]
                )
            ):
                slug = href.strip("/")
                if slug and slug not in seen_slugs:
                    seen_slugs.add(slug)
                    # Get display name from link text
                    link_text = link.inner_text().strip()
                    if link_text and is_valid_artist_name(link_text):
                        names.append(link_text)

    except Exception as e:
        logger.debug(f"Link extraction error: {e}")

    return names


def scrape_trending(page) -> list[dict]:
    """Scrapes Audiomack trending for artist names."""
    candidates = []

    url = find_working_url(page)
    if not url:
        logger.error("No working Audiomack trending URL found")
        return []

    page.screenshot(path="logs/audiomack_trending_debug.png")
    logger.info("Debug screenshot saved")

    # Method 1: Extract from artist profile links (most reliable)
    link_names = extract_artist_names_from_links(page)
    logger.info(f"  Found {len(link_names)} names from links")
    for name in link_names:
        candidates.append({
            "name": name,
            "source": "audiomack_trending_links",
        })

    # Method 2: Text parsing as fallback
    if len(candidates) < 5:
        page_text = page.inner_text("body")
        lines = [l.strip() for l in page_text.split('\n') if l.strip()]

        for line in lines:
            if is_valid_artist_name(line):
                # Avoid duplicates
                if not any(
                    c["name"].lower() == line.lower()
                    for c in candidates
                ):
                    candidates.append({
                        "name": line,
                        "source": "audiomack_trending_text",
                    })

        logger.info(
            f"  Found {len(candidates)} total after text fallback"
        )

    return candidates


def filter_new_artists(db, candidates: list[dict]) -> list[dict]:
    """Return only candidates not already in DB or queue."""
    existing = db.table("artists").select("name").execute()
    existing_names = {a["name"].lower() for a in existing.data}

    queued = db.table("discovery_queue").select("name").execute()
    queued_names = {q["name"].lower() for q in queued.data}

    known = existing_names | queued_names
    new = []
    seen = set()

    for c in candidates:
        name_lower = c["name"].lower()
        if name_lower not in known and name_lower not in seen:
            new.append(c)
            seen.add(name_lower)

    logger.info(f"New candidates not in DB: {len(new)}")
    for c in new[:10]:
        logger.info(f"  → {c['name']}")

    return new


def save_to_queue(db, candidates: list[dict]) -> int:
    saved = 0
    for c in candidates:
        try:
            db.table("discovery_queue").insert({
                "name": c["name"],
                "source": c.get("source", "audiomack_trending"),
                "signal_strength": None,
                "raw_data": c,
                "discovered_at": datetime.now().isoformat(),
                "status": "pending",
            }).execute()
            saved += 1
        except Exception as e:
            logger.debug(f"Queue insert skip for {c['name']}: {e}")
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
            records_inserted = save_to_queue(db, new_artists)
            logger.success(
                f"{records_inserted} new candidates added to queue"
            )
        else:
            logger.warning(
                "No candidates found — check "
                "logs/audiomack_trending_debug.png"
            )

        status = "success"

    except Exception as e:
        error_message = str(e)
        logger.error(f"Crashed: {e}")
    finally:
        duration = (datetime.now() - start_time).total_seconds()
        log_scraper_run(
            scraper_name=SCRAPER_NAME,
            status=status,
            records_attempted=0,
            records_inserted=records_inserted,
            records_failed=0,
            error_message=error_message,
            duration_seconds=duration,
        )
        logger.info(f"Done. {status}. {duration:.1f}s")


if __name__ == "__main__":
    run()