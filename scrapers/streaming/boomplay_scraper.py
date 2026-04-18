# scrapers/streaming/boomplay_scraper.py
# Scrapes Boomplay artist metrics for Nigerian artists
# Boomplay dominates East/West Africa — significant data gap
# Public pages — no login required
# Metrics: followers, monthly listeners, total plays

import re
import time
from datetime import date, datetime
from loguru import logger
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout
from database.client import get_supabase, log_scraper_run

SCRAPER_NAME = "boomplay_artist_snapshots"
SOURCE = "boomplay"
TODAY = str(date.today())
BASE_URL = "https://www.boomplay.com"

HEADERS_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)

# Boomplay artist slugs — format: boomplay.com/artists/{ID}
# Find the ID by searching an artist on Boomplay and checking the URL
# e.g. boomplay.com/artists/52906 = Burna Boy
ARTIST_BOOMPLAY_IDS = {
    "burna-boy":    "1182",
    "wizkid":       "1082",
    "davido":       "52904",
    "asake":        "6426916",
    "rema":         "5765693",
    "ayra-starr":   "6204537",
    "fireboy-dml":  "5484706",
    "bnxn":         "5484707",
    "omah-lay":     "6204538",
    "tems":         "6099301",
}


def parse_number(text: str) -> float | None:
    """Parse Boomplay number strings: 1.2M, 45.3K, 1,234"""
    if not text:
        return None
    text = text.strip().replace(",", "")
    try:
        if text.upper().endswith("B"):
            return float(text[:-1]) * 1_000_000_000
        elif text.upper().endswith("M"):
            return float(text[:-1]) * 1_000_000
        elif text.upper().endswith("K"):
            return float(text[:-1]) * 1_000
        else:
            return float(text)
    except ValueError:
        return None


def scrape_artist_page(
    page, artist_slug: str, boomplay_id: str
) -> dict | None:
    """
    Scrapes a Boomplay artist page.
    Returns metrics dict or None if failed.
    """
    url = f"{BASE_URL}/artists/{boomplay_id}"
    logger.info(f"  Scraping: {url}")

    try:
        page.goto(url, wait_until="domcontentloaded", timeout=60000)
        time.sleep(4)

        # Save debug screenshot on first artist
        if artist_slug == list(ARTIST_BOOMPLAY_IDS.keys())[0]:
            page.screenshot(path="logs/boomplay_debug.png")
            logger.info("  Debug screenshot saved")

        page_text = page.inner_text("body")
        lines = [l.strip() for l in page_text.split('\n') if l.strip()]

        logger.debug(f"  First 30 lines: {lines[:30]}")

        metrics = {}

        # Strategy 1: Look for stat elements
        stat_selectors = [
            "[class*='stat']",
            "[class*='count']",
            "[class*='fans']",
            "[class*='follower']",
            "[class*='listener']",
            "[class*='play']",
        ]

        for selector in stat_selectors:
            elements = page.query_selector_all(selector)
            for el in elements:
                try:
                    text = el.inner_text().strip()
                    sub_lines = [
                        l.strip() for l in text.split('\n')
                        if l.strip()
                    ]
                    if len(sub_lines) >= 2:
                        value = parse_number(sub_lines[0])
                        label = sub_lines[1].lower()
                        if value is not None:
                            if "fan" in label or "follower" in label:
                                metrics["followers"] = value
                            elif "listener" in label:
                                metrics["monthly_listeners"] = value
                            elif "play" in label or "stream" in label:
                                metrics["total_plays"] = value
                except Exception:
                    continue

        # Strategy 2: Text parsing fallback
        if not metrics:
            label_map = {
                "fans": "followers",
                "followers": "followers",
                "monthly listeners": "monthly_listeners",
                "listeners": "monthly_listeners",
                "plays": "total_plays",
                "streams": "total_plays",
            }

            for i, line in enumerate(lines):
                lower = line.lower().strip()
                if lower in label_map:
                    metric_key = label_map[lower]
                    if i > 0:
                        value = parse_number(lines[i - 1])
                        if value:
                            metrics[metric_key] = value
                    if not metrics.get(metric_key) and i + 1 < len(lines):
                        value = parse_number(lines[i + 1])
                        if value:
                            metrics[metric_key] = value

        if metrics:
            logger.info(
                f"  Boomplay metrics: {metrics}"
            )
        else:
            logger.warning(
                f"  No metrics found for {artist_slug} "
                "— check logs/boomplay_debug.png"
            )

        return metrics if metrics else None

    except PlaywrightTimeout:
        logger.error(f"  Timeout loading {url}")
        return None
    except Exception as e:
        logger.error(f"  Error scraping {artist_slug}: {e}")
        return None


def save_metrics(
    db, artist_id: str, metrics: dict
) -> int:
    """Save Boomplay metrics to artist_snapshots."""
    saved = 0

    for metric_name, value in metrics.items():
        if value is None:
            continue
        try:
            db.table("artist_snapshots").upsert(
                {
                    "artist_id": artist_id,
                    "source": SOURCE,
                    "metric_name": metric_name,
                    "metric_value": value,
                    "snapshot_date": TODAY,
                    "captured_at": datetime.now().isoformat(),
                },
                on_conflict="artist_id,source,metric_name,snapshot_date"
            ).execute()
            saved += 1
        except Exception as e:
            logger.error(
                f"  DB error saving {metric_name}: {e}"
            )

    return saved


def run():
    start_time = datetime.now()
    logger.info(f"=== Starting {SCRAPER_NAME} ===")

    status = "failed"
    records_attempted = 0
    records_inserted = 0
    records_failed = 0
    error_message = None

    try:
        db = get_supabase()

        # Load artists from DB
        result = db.table("artists").select(
            "id, name, slug"
        ).eq("is_active", True).execute()

        artist_map = {a["slug"]: a for a in result.data}

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(
                user_agent=HEADERS_UA,
                viewport={"width": 1280, "height": 900},
                locale="en-US",
            )
            page = context.new_page()

            for i, (artist_slug, boomplay_id) in enumerate(
                ARTIST_BOOMPLAY_IDS.items(), 1
            ):
                artist = artist_map.get(artist_slug)
                if not artist:
                    logger.warning(
                        f"Artist not in DB: {artist_slug}"
                    )
                    continue

                logger.info(
                    f"[{i}/{len(ARTIST_BOOMPLAY_IDS)}] "
                    f"{artist['name']}"
                )
                records_attempted += 1

                metrics = scrape_artist_page(
                    page, artist_slug, boomplay_id
                )

                if metrics:
                    saved = save_metrics(db, artist["id"], metrics)
                    records_inserted += saved
                    logger.info(f"  Saved {saved} metrics")
                else:
                    records_failed += 1

                if i < len(ARTIST_BOOMPLAY_IDS):
                    time.sleep(3)

            browser.close()

        status = (
            "success" if records_failed == 0
            else "partial" if records_inserted > 0
            else "failed"
        )
        logger.success(
            f"Complete: {records_inserted} metrics saved, "
            f"{records_failed} failed"
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
            records_attempted=records_attempted,
            records_inserted=records_inserted,
            records_failed=records_failed,
            error_message=error_message,
            duration_seconds=duration
        )
        logger.info(f"Done. {status}. {duration:.1f}s")


if __name__ == "__main__":
    run()