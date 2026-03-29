# scrapers/charts/turntable_scraper.py
# Scrapes TurnTable Nigeria Official Top 100
# Published every Monday — combines radio + streaming from
# Audiomack, Boomplay, YouTube, Apple Music, Deezer, Spotify
# This is the most authoritative Nigerian music chart in existence

import time
from datetime import date, datetime
from loguru import logger
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout
from database.client import get_supabase, log_scraper_run

SCRAPER_NAME = "turntable_ng_top100"
CHART_NAME = "turntable_ng_top100"
TODAY = str(date.today())
URL = "https://www.turntablecharts.com/Charts/Top50"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    )
}


def scrape_turntable() -> list[dict]:
    """
    Scrapes TurnTable Nigeria Top 100.
    Uses Playwright because the site is React-rendered.
    """
    entries = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent=HEADERS["User-Agent"],
            viewport={"width": 1280, "height": 900},
        )
        page = context.new_page()

        try:
            logger.info(f"Loading TurnTable charts: {URL}")
            page.goto(URL, wait_until="networkidle", timeout=60000)
            time.sleep(4)

            # Screenshot for debugging
            page.screenshot(path="logs/turntable_debug.png")

            # Get page text
            page_text = page.inner_text("body")
            lines = [l.strip() for l in page_text.split('\n') if l.strip()]

            logger.debug(f"Page lines (first 30): {lines[:30]}")
            logger.info(f"Total lines: {len(lines)}")

            # Try selector-based extraction first
            entries = extract_by_selectors(page)

            if not entries:
                logger.warning("Selectors failed — trying text parsing")
                entries = extract_by_text(lines)

            logger.info(f"Extracted {len(entries)} chart entries")

        except PlaywrightTimeout:
            logger.error("TurnTable page timed out")
        except Exception as e:
            logger.error(f"Scraping error: {e}")
            import traceback
            logger.debug(traceback.format_exc())
        finally:
            browser.close()

    return entries


def extract_by_selectors(page) -> list[dict]:
    """
    Tries multiple selector patterns to find chart rows.
    TurnTable is a React app — class names may vary.
    """
    entries = []

    # Common React chart component patterns
    row_selectors = [
        "[class*='chart-item']",
        "[class*='ChartItem']",
        "[class*='chart-row']",
        "[class*='ChartRow']",
        "[class*='song-row']",
        "[class*='track-row']",
        "tbody tr",
        "[class*='list-item']",
    ]

    for selector in row_selectors:
        rows = page.query_selector_all(selector)
        if not rows:
            continue

        logger.debug(f"Found {len(rows)} rows with: {selector}")

        for row in rows[:100]:
            try:
                text = row.inner_text().strip()
                lines = [l.strip() for l in text.split('\n') if l.strip()]

                if len(lines) < 2:
                    continue

                # Look for a number (position) in the row
                position = None
                title = None
                artist = None

                for i, line in enumerate(lines):
                    if line.isdigit() and 1 <= int(line) <= 100:
                        position = int(line)
                        if i + 1 < len(lines):
                            title = lines[i + 1]
                        if i + 2 < len(lines):
                            artist = lines[i + 2]
                        break

                if position and title:
                    entries.append({
                        "position": position,
                        "raw_title": title,
                        "raw_artist": artist or "",
                        "chart_date": TODAY,
                    })

            except Exception as e:
                logger.debug(f"Row parse error: {e}")
                continue

        if entries:
            logger.info(
                f"Selector extraction succeeded with: {selector}"
            )
            break

    return entries


def extract_by_text(lines: list[str]) -> list[dict]:
    """
    Text-based fallback. Looks for number-title-artist patterns.
    """
    entries = []

    skip = {
        "top 100", "top 50", "turntable", "charts", "nigeria",
        "streaming", "airplay", "official", "search", "home",
        "news", "magazine", "login", "signup", "this week",
    }

    i = 0
    while i < len(lines) and len(entries) < 100:
        line = lines[i]

        if line.isdigit() and 1 <= int(line) <= 100:
            position = int(line)
            title = None
            artist = None

            if i + 1 < len(lines):
                candidate_title = lines[i + 1]
                if (
                    not candidate_title.isdigit()
                    and candidate_title.lower() not in skip
                    and len(candidate_title) > 1
                    and len(candidate_title) < 120
                ):
                    title = candidate_title

            if title and i + 2 < len(lines):
                candidate_artist = lines[i + 2]
                if (
                    not candidate_artist.isdigit()
                    and candidate_artist.lower() not in skip
                ):
                    artist = candidate_artist

            if title:
                entries.append({
                    "position": position,
                    "raw_title": title,
                    "raw_artist": artist or "",
                    "chart_date": TODAY,
                })
                i += 3
                continue

        i += 1

    return entries


def save_entries(entries: list[dict]) -> tuple[int, int]:
    db = get_supabase()
    inserted = 0
    failed = 0

    for entry in entries:
        try:
            db.table("chart_positions").upsert(
                {
                    "chart_name": CHART_NAME,
                    "position": entry["position"],
                    "chart_date": entry["chart_date"],
                    "raw_title": entry["raw_title"],
                    "raw_artist": entry["raw_artist"],
                },
                on_conflict="chart_name,position,chart_date"
            ).execute()
            inserted += 1
        except Exception as e:
            logger.error(
                f"DB error #{entry['position']} "
                f"'{entry['raw_title']}': {e}"
            )
            failed += 1

    return inserted, failed


def run():
    start_time = datetime.now()
    logger.info(f"=== Starting {SCRAPER_NAME} ===")

    status = "failed"
    records_attempted = 0
    records_inserted = 0
    records_failed = 0
    error_message = None

    try:
        entries = scrape_turntable()
        records_attempted = len(entries)

        if entries:
            logger.info("--- Top 10 ---")
            for e in entries[:10]:
                logger.info(
                    f"  #{e['position']}: {e['raw_title']} "
                    f"— {e['raw_artist']}"
                )

            records_inserted, records_failed = save_entries(entries)
            status = "success" if records_failed == 0 else "partial"
            logger.success(
                f"Complete: {records_inserted} saved, "
                f"{records_failed} failed"
            )
        else:
            error_message = "No entries extracted — check logs/turntable_debug.png"
            logger.error(error_message)

    except Exception as e:
        error_message = str(e)
        logger.error(f"Crashed: {e}")

    finally:
        duration = (datetime.now() - start_time).total_seconds()
        log_scraper_run(
            scraper_name=SCRAPER_NAME,
            status=status,
            records_attempted=records_attempted,
            records_inserted=records_inserted,
            records_failed=records_failed,
            error_message=error_message,
            duration_seconds=duration
        )
        logger.info(f"Done. {status}. {duration:.1f}s")


if __name__ == "__main__":
    run()