# scrapers/streaming/kworb_scraper.py
# Pulls Nigeria charts from Kworb.net across multiple platforms
# Sources: Spotify (daily + weekly), Apple Music, iTunes
# No API key — public data, simple HTML table parsing

import time
import requests
from bs4 import BeautifulSoup
from datetime import date, datetime
from loguru import logger
from database.client import get_supabase, log_scraper_run

SCRAPER_NAME = "kworb_ng_charts"
TODAY = str(date.today())

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    )
}

# All Nigeria chart sources on Kworb
# chart_name maps to our chart_positions.chart_name column
CHART_SOURCES = [
    {
        "chart_name": "spotify_ng_daily",
        "url": "https://kworb.net/spotify/country/ng_daily.html",
        "platform": "spotify",
        "has_streams": True,   # Spotify gives real stream counts
    },
    {
        "chart_name": "spotify_ng_weekly",
        "url": "https://kworb.net/spotify/country/ng_weekly.html",
        "platform": "spotify",
        "has_streams": True,
    },
    {
        "chart_name": "apple_music_ng",
        "url": "https://kworb.net/charts/apple_s/ng.html",
        "platform": "apple_music",
        "has_streams": False,  # Apple Music gives positions only
    },
    {
        "chart_name": "itunes_ng",
        "url": "https://kworb.net/charts/itunes/ng.html",
        "platform": "itunes",
        "has_streams": False,
    },
]


def parse_number(text: str) -> float | None:
    """Handles: 606,610 | -69,206 | +13,294 | 3,887,521"""
    if not text:
        return None
    text = text.strip().replace(",", "").replace("+", "")
    try:
        return float(text)
    except ValueError:
        return None


def scrape_spotify_chart(source: dict) -> list[dict]:
    """
    Scrapes Spotify charts from Kworb.
    These have rich data: streams, stream changes, 7-day, totals.
    Column order: Pos | P+ | Artist and Title | Days | Pk | (x?) | Streams | Streams+ | 7Day | 7Day+ | Total
    """
    entries = []
    url = source["url"]
    chart_name = source["chart_name"]

    try:
        response = requests.get(url, headers=HEADERS, timeout=30)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, "lxml")

        table = soup.find("table")
        if not table:
            logger.error(f"No table found at {url}")
            return []

        rows = table.find_all("tr")
        logger.info(f"  {chart_name}: {len(rows) - 1} rows")

        for row in rows[1:]:
            cells = row.find_all("td")
            if len(cells) < 5:
                continue

            try:
                position = int(cells[0].get_text(strip=True))
            except ValueError:
                continue

            try:
                # Extract artist and title from the link cell
                title_cell = cells[2]
                links = title_cell.find_all("a")

                artist = ""
                title = ""
                spotify_track_id = None
                spotify_artist_id = None

                for link in links:
                    href = link.get("href", "")
                    text = link.get_text(strip=True)
                    if "/artist/" in href:
                        if not artist:
                            artist = text
                            spotify_artist_id = href.split("/artist/")[-1].replace(".html", "")
                    elif "/track/" in href:
                        title = text
                        spotify_track_id = href.split("/track/")[-1].replace(".html", "")

                # If no separate title link, get full text and split
                if not title:
                    full_text = title_cell.get_text(" ", strip=True)
                    parts = full_text.split(" - ", 1)
                    if len(parts) == 2:
                        artist = parts[0].strip()
                        title = parts[1].strip()
                    else:
                        title = full_text

                days_text = cells[3].get_text(strip=True) if len(cells) > 3 else ""
                peak_text = cells[4].get_text(strip=True) if len(cells) > 4 else ""
                streams_text = cells[6].get_text(strip=True) if len(cells) > 6 else ""
                streams_chg_text = cells[7].get_text(strip=True) if len(cells) > 7 else ""
                streams_7d_text = cells[8].get_text(strip=True) if len(cells) > 8 else ""
                streams_total_text = cells[10].get_text(strip=True) if len(cells) > 10 else ""

                entries.append({
                    "position": position,
                    "raw_artist": artist,
                    "raw_title": title,
                    "chart_name": chart_name,
                    "chart_date": TODAY,
                    "days_on_chart": int(parse_number(days_text) or 0) or None,
                    "peak_position": int(parse_number(peak_text) or 0) or None,
                    "daily_streams": parse_number(streams_text),
                    "streams_change": parse_number(streams_chg_text),
                    "streams_7day": parse_number(streams_7d_text),
                    "streams_total": parse_number(streams_total_text),
                    "spotify_track_id": spotify_track_id,
                    "spotify_artist_id": spotify_artist_id,
                })

            except Exception as e:
                logger.debug(f"  Row parse error: {e}")
                continue

    except requests.RequestException as e:
        logger.error(f"  Request failed for {url}: {e}")

    return entries


def scrape_apple_chart(source: dict) -> list[dict]:
    """
    Scrapes Apple Music / iTunes charts from Kworb.
    These are simpler — position, artist, title, movement.
    Column order varies but typically: Pos | Movement | Artist - Title
    """
    entries = []
    url = source["url"]
    chart_name = source["chart_name"]

    try:
        response = requests.get(url, headers=HEADERS, timeout=30)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, "lxml")

        table = soup.find("table")
        if not table:
            logger.error(f"No table found at {url}")
            return []

        rows = table.find_all("tr")
        logger.info(f"  {chart_name}: {len(rows) - 1} rows")

        for row in rows[1:]:
            cells = row.find_all("td")
            if len(cells) < 2:
                continue

            try:
                position = int(cells[0].get_text(strip=True))
            except ValueError:
                continue

            try:
                # Find the cell with artist/title — look for links
                artist = ""
                title = ""

                for cell in cells[1:]:
                    links = cell.find_all("a")
                    if links:
                        for link in links:
                            href = link.get("href", "")
                            text = link.get_text(strip=True)
                            if "artist" in href.lower():
                                if not artist:
                                    artist = text
                            else:
                                if not title:
                                    title = text
                        if title:
                            break

                # Fallback: get full cell text
                if not title:
                    for cell in cells[1:4]:
                        full_text = cell.get_text(" ", strip=True)
                        if len(full_text) > 3 and not full_text.isdigit():
                            parts = full_text.split(" - ", 1)
                            if len(parts) == 2:
                                artist = parts[0].strip()
                                title = parts[1].strip()
                            else:
                                title = full_text
                            break

                if not title:
                    continue

                entries.append({
                    "position": position,
                    "raw_artist": artist,
                    "raw_title": title,
                    "chart_name": chart_name,
                    "chart_date": TODAY,
                    "days_on_chart": None,
                    "peak_position": None,
                    "daily_streams": None,
                    "streams_change": None,
                    "streams_7day": None,
                    "streams_total": None,
                    "spotify_track_id": None,
                    "spotify_artist_id": None,
                })

            except Exception as e:
                logger.debug(f"  Row parse error: {e}")
                continue

    except requests.RequestException as e:
        logger.error(f"  Request failed for {url}: {e}")

    return entries


def save_entries(entries: list[dict]) -> tuple[int, int]:
    """Upserts all chart entries into chart_positions."""
    db = get_supabase()
    inserted = 0
    failed = 0

    for entry in entries:
        try:
            db.table("chart_positions").upsert(
                {
                    "chart_name": entry["chart_name"],
                    "position": entry["position"],
                    "chart_date": entry["chart_date"],
                    "raw_title": entry["raw_title"],
                    "raw_artist": entry["raw_artist"],
                    "peak_position": entry.get("peak_position"),
                    "weeks_on_chart": entry.get("days_on_chart"),
                    "daily_streams": entry.get("daily_streams"),
                    "streams_change": entry.get("streams_change"),
                    "streams_7day": entry.get("streams_7day"),
                    "streams_total": entry.get("streams_total"),
                    "spotify_track_id": entry.get("spotify_track_id"),
                    "spotify_artist_id": entry.get("spotify_artist_id"),
                },
                on_conflict="chart_name,position,chart_date"
            ).execute()
            inserted += 1

        except Exception as e:
            logger.error(
                f"  DB error #{entry['position']} "
                f"'{entry['raw_title']}' ({entry['chart_name']}): {e}"
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
    all_entries = []

    try:
        for source in CHART_SOURCES:
            logger.info(f"Scraping: {source['chart_name']}")

            if source["has_streams"]:
                entries = scrape_spotify_chart(source)
            else:
                entries = scrape_apple_chart(source)

            records_attempted += len(entries)
            all_entries.extend(entries)

            # Preview top 3 for each source
            for e in entries[:3]:
                stream_info = (
                    f" | streams={e['daily_streams']:,.0f}"
                    if e.get("daily_streams") else ""
                )
                logger.info(
                    f"  #{e['position']}: {e['raw_title']} "
                    f"— {e['raw_artist']}{stream_info}"
                )

            # Polite delay between sources
            time.sleep(2)

        # Save everything
        if all_entries:
            records_inserted, records_failed = save_entries(all_entries)
            status = "success" if records_failed == 0 else "partial"
            logger.success(
                f"Complete: {records_inserted} entries saved across "
                f"{len(CHART_SOURCES)} charts, {records_failed} failed"
            )
        else:
            error_message = "No entries from any source"
            logger.error(error_message)

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
        logger.info(
            f"Logged. Status: {status}. Duration: {duration:.1f}s"
        )


if __name__ == "__main__":
    run()