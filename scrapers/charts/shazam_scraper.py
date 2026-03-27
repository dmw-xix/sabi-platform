# scrapers/charts/shazam_scraper.py
# Scrapes Shazam Nigeria Top 200 daily chart
# Strategy: Direct CSV download → Network interception → Text fallback

import io
import csv
import time
import requests
from datetime import date, datetime
from loguru import logger
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout
from database.client import get_supabase, log_scraper_run

SCRAPER_NAME = "shazam_ng_charts"
CHART_NAME = "shazam_ng_top200"
SHAZAM_URL = "https://www.shazam.com/charts/top-200/nigeria"

DIRECT_CSV_URLS = [
    "https://www.shazam.com/services/charts/csv/top-200/nigeria",
    "https://www.shazam.com/services/charts/csv/top-200/geography/NG",
    "https://charts.shazam.com/charts/csv/top-200/nigeria",
]

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Referer": "https://www.shazam.com/charts/top-200/nigeria",
    "Accept": "text/csv,text/plain,*/*",
}


# ── Attempt 1: Direct HTTP request to CSV endpoint ────────────────────────────

def try_direct_csv_download() -> list[dict]:
    """
    Tries known Shazam CSV endpoint URLs directly via HTTP.
    No browser needed. Fastest approach.
    """
    for url in DIRECT_CSV_URLS:
        try:
            logger.info(f"Trying direct CSV URL: {url}")
            response = requests.get(url, headers=HEADERS, timeout=15)

            if response.status_code != 200:
                logger.debug(f"Status {response.status_code} from {url}")
                continue

            # Strip BOM (\ufeff) that some servers prepend
            content = response.text.lstrip('\ufeff').strip()

            # Validate it looks like CSV — needs multiple lines with commas
            lines = content.split('\n')
            has_commas = ',' in content[:500]
            has_enough_rows = len(lines) > 5

            logger.debug(
                f"Response: {len(lines)} lines, "
                f"has commas: {has_commas}, "
                f"first 80 chars: {content[:80]!r}"
            )

            if has_commas and has_enough_rows:
                logger.info(
                    f"Direct CSV looks valid — "
                    f"{len(lines)} lines from {url}"
                )
                return parse_csv_content(content)
            else:
                logger.debug(
                    f"Response from {url} is not valid CSV — skipping"
                )

        except Exception as e:
            logger.debug(f"Direct URL {url} failed: {e}")
            continue

    logger.info("All direct CSV URLs failed or returned invalid data")
    return []


# ── Attempt 2: Browser — click button and intercept download ──────────────────

def try_network_interception() -> list[dict]:
    """
    Opens Shazam in a headless browser.
    Clicks DOWNLOAD CSV and captures the resulting file.
    Two sub-strategies:
      A) expect_download() — catches file download dialogs
      B) response listener — catches text/csv network responses
    """
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent=HEADERS["User-Agent"],
            viewport={"width": 1280, "height": 900},
            locale="en-US",
            accept_downloads=True,
        )
        page = context.new_page()

        # Sub-strategy B: listen for text/csv responses
        captured_csv = {"content": None, "url": None}

        def handle_response(response):
            if captured_csv["content"]:
                return
            content_type = response.headers.get("content-type", "").lower()
            if "text/csv" not in content_type:
                return
            try:
                content = response.text()
                content = content.lstrip('\ufeff').strip()
                lines = content.split('\n')
                if len(lines) > 5 and ',' in content[:500]:
                    captured_csv["content"] = content
                    captured_csv["url"] = response.url
                    logger.info(
                        f"Response listener captured CSV "
                        f"({len(lines)} lines) from: {response.url}"
                    )
            except Exception as e:
                logger.debug(f"Could not read CSV response: {e}")

        page.on("response", handle_response)

        try:
            logger.info("Loading Shazam page in browser...")
            page.goto(
                SHAZAM_URL,
                wait_until="networkidle",
                timeout=60000
            )
            time.sleep(3)

            # Find the download button
            button_selectors = [
                "button:has-text('DOWNLOAD CSV')",
                "button:has-text('Download CSV')",
                "button:has-text('Download')",
                "[class*='download']",
                "a:has-text('CSV')",
                "[aria-label*='download' i]",
                "[data-testid*='download' i]",
            ]

            button = None
            matched_selector = None
            for selector in button_selectors:
                try:
                    el = page.query_selector(selector)
                    if el and el.is_visible():
                        button = el
                        matched_selector = selector
                        break
                except Exception:
                    continue

            if not button:
                # Log all visible buttons to help future debugging
                all_buttons = page.query_selector_all(
                    "button, a[href], [role='button']"
                )
                visible_texts = []
                for b in all_buttons:
                    try:
                        if b.is_visible():
                            text = b.inner_text().strip()
                            if text and len(text) < 50:
                                visible_texts.append(text)
                    except Exception:
                        pass
                logger.warning(
                    f"Download button not found. "
                    f"Visible buttons: {visible_texts[:20]}"
                )
                return []

            logger.info(f"Found button: '{matched_selector}'")

            # Sub-strategy A: expect_download intercepts file dialogs
            try:
                logger.info("Clicking button with expect_download...")
                with page.expect_download(timeout=15000) as dl_info:
                    button.click()

                download = dl_info.value
                path = download.path()

                # utf-8-sig automatically strips BOM
                with open(path, "r", encoding="utf-8-sig") as f:
                    content = f.read().strip()

                lines = content.split('\n')
                logger.info(
                    f"expect_download captured file: "
                    f"{len(lines)} lines, "
                    f"first 100 chars: {content[:100]!r}"
                )

                if len(lines) > 5 and ',' in content[:500]:
                    return parse_csv_content(content)
                else:
                    logger.warning(
                        "Downloaded file does not look like CSV"
                    )

            except Exception as dl_err:
                logger.warning(f"expect_download failed: {dl_err}")
                # Fall through to check response listener result

            # Wait briefly in case response listener is still receiving
            time.sleep(2)

            # Sub-strategy B result
            if captured_csv["content"]:
                logger.info(
                    "Using CSV captured by response listener"
                )
                return parse_csv_content(captured_csv["content"])

            logger.warning(
                "Button clicked but no CSV captured by either method"
            )
            return []

        except Exception as e:
            logger.error(f"Browser error in network interception: {e}")
            return []
        finally:
            browser.close()


# ── Attempt 3: Text parsing fallback (partial — ~50 entries only) ─────────────

def try_text_fallback() -> list[dict]:
    """
    Last resort. Parses whatever is rendered in the DOM.
    Shazam uses a virtual list so only ~50 entries are in DOM at once.
    Saves partial data rather than nothing.
    """
    logger.warning(
        "Using text fallback — Shazam virtual list limits this "
        "to ~50 entries. Saving partial data."
    )

    nav_words = {
        "get the app", "concerts", "charts", "viral", "cities",
        "download csv", "top 200", "nigeria", "radio spins",
        "connect", "the top songs in nigeria this week",
        "music video", "listen on", "fast forward '26",
        "top songs", "this week",
    }

    entries = []
    today = str(date.today())

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent=HEADERS["User-Agent"],
            viewport={"width": 1280, "height": 900},
        )
        page = context.new_page()

        try:
            page.goto(
                SHAZAM_URL,
                wait_until="networkidle",
                timeout=60000
            )
            time.sleep(4)

            page_text = page.inner_text("body")
            lines = [
                line.strip()
                for line in page_text.split("\n")
                if line.strip()
            ]
            logger.info(f"Text fallback: {len(lines)} DOM lines")

            i = 0
            while i < len(lines):
                line = lines[i]
                if line.isdigit() and 1 <= int(line) <= 200:
                    position = int(line)
                    if i + 2 < len(lines):
                        title = lines[i + 1]
                        artist = lines[i + 2]
                        if (
                            not title.isdigit()
                            and title.lower() not in nav_words
                            and 0 < len(title) < 120
                        ):
                            entries.append({
                                "position": position,
                                "raw_title": title,
                                "raw_artist": artist,
                                "chart_date": today,
                            })
                            i += 3
                            continue
                i += 1

        except Exception as e:
            logger.error(f"Text fallback browser error: {e}")
        finally:
            browser.close()

    if entries:
        positions = [e["position"] for e in entries]
        logger.info(
            f"Text fallback captured positions "
            f"{min(positions)}–{max(positions)} "
            f"({len(entries)} entries)"
        )

    return entries


# ── CSV Parser ────────────────────────────────────────────────────────────────
def parse_csv_content(content: str) -> list[dict]:
    """
    Parses Shazam CSV into chart entry dicts.
    Shazam CSV structure:
      Line 1: "Friday, 27 March 2026 [performance over the past 7 days]"  <- skip
      Line 2: Rank,Artist,Title  <- actual headers
      Line 3+: data rows
    """
    entries = []
    today = str(date.today())

    try:
        lines = content.strip().split('\n')
        logger.info(f"CSV total lines: {len(lines)}")
        logger.info(f"Line 1 (metadata): {lines[0][:80]!r}")
        logger.info(f"Line 2 (headers):  {lines[1][:80]!r}")
        logger.info(f"Line 3 (first row): {lines[2][:80]!r}")

        # Skip line 1 (metadata date row), parse from line 2 onwards
        csv_without_metadata = '\n'.join(lines[1:])
        reader = csv.DictReader(io.StringIO(csv_without_metadata))

        fieldnames = reader.fieldnames or []
        logger.info(f"CSV columns detected: {fieldnames}")

        for row in reader:
            try:
                position = (
                    row.get("Rank") or row.get("Position") or
                    row.get("position") or row.get("rank") or
                    row.get("#")
                )
                title = (
                    row.get("Title") or row.get("title") or
                    row.get("Song") or row.get("Track") or
                    row.get("Track Name") or row.get("Name")
                )
                artist = (
                    row.get("Artist") or row.get("artist") or
                    row.get("Artist Name") or row.get("Artists")
                )

                if not position or not title:
                    continue

                entries.append({
                    "position": int(str(position).strip()),
                    "raw_title": str(title).strip(),
                    "raw_artist": str(artist).strip()
                    if artist else "Unknown",
                    "chart_date": today,
                })

            except Exception as e:
                logger.debug(f"Skipping malformed row {row}: {e}")
                continue

        logger.info(f"Parsed {len(entries)} entries from CSV")

    except Exception as e:
        logger.error(f"CSV parsing error: {e}")
        logger.debug(f"Content preview: {content[:300]!r}")

    return entries


# ── Database Save ─────────────────────────────────────────────────────────────

def save_chart_entries(entries: list[dict]) -> tuple[int, int]:
    """
    Upserts chart entries to database.
    Returns (inserted_count, failed_count).
    """
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
                    "raw_title": entry.get("raw_title"),
                    "raw_artist": entry.get("raw_artist"),
                },
                on_conflict="chart_name,position,chart_date"
            ).execute()
            inserted += 1
            if inserted % 50 == 0:
                logger.info(f"  Saved {inserted} entries...")

        except Exception as e:
            logger.error(
                f"DB error on #{entry['position']} "
                f"'{entry.get('raw_title')}': {e}"
            )
            failed += 1

    return inserted, failed


# ── Master Orchestrator ───────────────────────────────────────────────────────

def scrape_shazam_nigeria() -> list[dict]:
    """
    Tries three approaches in order, stopping at the first success.
    1. Direct HTTP CSV download (fast, no browser)
    2. Browser + button click with download/response interception
    3. DOM text parsing (partial data, last resort)
    """

    logger.info("Attempt 1: Direct HTTP CSV download...")
    entries = try_direct_csv_download()
    if len(entries) > 10:
        logger.info(
            f"Attempt 1 succeeded with {len(entries)} entries"
        )
        return entries

    logger.info("Attempt 2: Browser network interception...")
    entries = try_network_interception()
    if len(entries) > 10:
        logger.info(
            f"Attempt 2 succeeded with {len(entries)} entries"
        )
        return entries

    logger.warning("Attempt 3: Text fallback (partial data)...")
    entries = try_text_fallback()
    return entries


# ── Entry Point ───────────────────────────────────────────────────────────────

def run():
    start_time = datetime.now()
    logger.info(f"=== Starting {SCRAPER_NAME} ===")

    status = "failed"
    records_attempted = 0
    records_inserted = 0
    records_failed = 0
    error_message = None

    try:
        entries = scrape_shazam_nigeria()
        records_attempted = len(entries)

        if not entries:
            error_message = "All extraction attempts returned no data"
            logger.error(error_message)

        else:
            logger.info("--- Preview: first 5 entries ---")
            for e in entries[:5]:
                logger.info(
                    f"  #{e['position']}: "
                    f"{e['raw_title']} — {e['raw_artist']}"
                )
            logger.info(
                f"--- Total extracted: {len(entries)} entries ---"
            )

            records_inserted, records_failed = save_chart_entries(entries)

            if records_failed == 0:
                status = "success"
            elif records_inserted > 0:
                status = "partial"

            logger.success(
                f"Complete: {records_inserted} saved, "
                f"{records_failed} failed"
            )

    except Exception as e:
        error_message = str(e)
        logger.error(f"Scraper crashed: {e}")

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
        logger.info(
            f"Run logged. Status: {status}. "
            f"Duration: {duration:.1f}s"
        )


if __name__ == "__main__":
    run()