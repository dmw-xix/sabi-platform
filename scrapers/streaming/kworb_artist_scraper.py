# scrapers/streaming/kworb_artist_scraper.py
# Tracks how many countries each artist is charting in globally
# Uses Kworb iTunes artist pages — updated every 15 minutes
# Key signal: Nigerian artists crossing over internationally
# URL pattern: kworb.net/itunes/artist/{slug}.html

import time
import requests
from bs4 import BeautifulSoup
from datetime import date, datetime
from loguru import logger
from database.client import get_supabase, log_scraper_run

SCRAPER_NAME = "kworb_artist_global"
SOURCE = "kworb_itunes"
TODAY = str(date.today())

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    )
}

# Kworb uses iTunes artist slugs — different from Audiomack/Spotify
# Pattern: lowercase, no spaces, e.g. burnaboy, wizkidayo, asakemusik
# We store these in a new column — for now hardcode the seed list
ARTIST_KWORB_SLUGS = {
    "burna-boy":    "burnaboy",
    "wizkid":       "wizkidayo",
    "asake":        "asakemusik",
    "davido":       "davido",
    "rema":         "rema",
    "ayra-starr":   "ayrastarr",
    "fireboy-dml":  "fireboydml",
    "bnxn":         "bnxn",
    "omah-lay":     "omahlay",
    "tems":         "tems",
}


def scrape_artist_global_presence(
    artist_slug: str,
    kworb_slug: str
) -> dict | None:
    """
    Scrapes an artist's Kworb iTunes page.
    Returns:
    - countries_charting: number of countries with active chart entries
    - top_songs: list of songs currently charting with their positions
    - global_presence_score: weighted score based on chart positions
    """
    url = f"https://kworb.net/itunes/artist/{kworb_slug}.html"

    try:
        logger.info(f"  Fetching: {url}")
        response = requests.get(url, headers=HEADERS, timeout=20)

        if response.status_code == 404:
            logger.warning(
                f"  404 for {kworb_slug} — slug may be wrong"
            )
            return None

        response.raise_for_status()
        soup = BeautifulSoup(response.text, "lxml")

        # Find all tables on the page
        # Kworb artist page has multiple tables:
        # - Songs currently charting
        # - Country breakdown
        tables = soup.find_all("table")

        if not tables:
            logger.warning(f"  No tables found for {kworb_slug}")
            return None

        songs_charting = []
        countries_set = set()
        total_chart_score = 0

        for table in tables:
            rows = table.find_all("tr")
            for row in rows[1:]:  # skip header
                cells = row.find_all("td")
                if len(cells) < 2:
                    continue

                try:
                    row_text = row.get_text(" ", strip=True)

                    # Look for country codes (2-letter uppercase)
                    # and position numbers
                    links = row.find_all("a")
                    for link in links:
                        href = link.get("href", "")
                        text = link.get_text(strip=True)

                        # Country chart links contain country codes
                        if "/itunes/country/" in href or "/charts/itunes/" in href:
                            # Extract country code
                            country = href.split("/")[-1].replace(
                                ".html", ""
                            ).upper()[:2]
                            if len(country) == 2 and country.isalpha():
                                countries_set.add(country)

                        # Song title
                        if "/itunes/song/" in href or "/track/" in href:
                            # Get the position from the first cell
                            position_text = cells[0].get_text(strip=True)
                            try:
                                position = int(position_text)
                                # Score: #1=200pts, #10=100pts, #100=10pts
                                score = max(0, 210 - position * 2)
                                total_chart_score += score
                                songs_charting.append({
                                    "title": text,
                                    "position": position,
                                })
                            except ValueError:
                                pass

                except Exception:
                    continue

        # Count unique countries from page text as fallback
        # Kworb shows country flags/names in the page
        if not countries_set:
            page_text = soup.get_text()
            # Count occurrences of country patterns
            import re
            country_patterns = re.findall(r'\b[A-Z]{2}\b', page_text)
            # Filter to known ISO country codes (rough check)
            valid_countries = {
                c for c in country_patterns
                if len(c) == 2 and c.isalpha() and c not in
                {'AS', 'IN', 'ON', 'AT', 'OR', 'TO', 'BY', 'BE', 'IS'}
            }
            countries_set = valid_countries

        result = {
            "countries_charting": len(countries_set),
            "countries_list": list(countries_set)[:50],
            "songs_count": len(songs_charting),
            "top_songs": songs_charting[:10],
            "global_score": total_chart_score,
        }

        logger.info(
            f"  {kworb_slug}: {len(countries_set)} countries, "
            f"{len(songs_charting)} songs charting, "
            f"score={total_chart_score}"
        )

        return result

    except requests.RequestException as e:
        logger.error(f"  Request failed for {kworb_slug}: {e}")
        return None


def scrape_artist_spotify_history(
    artist_slug: str,
    spotify_id: str
) -> dict | None:
    """
    Scrapes Kworb Spotify artist songs page.
    Shows all-time Spotify performance per artist.
    URL: kworb.net/spotify/artist/{SPOTIFY_ID}_songs.html
    """
    url = f"https://kworb.net/spotify/artist/{spotify_id}_songs.html"

    try:
        logger.info(f"  Fetching Spotify history: {url}")
        response = requests.get(url, headers=HEADERS, timeout=20)

        if response.status_code != 200:
            return None

        soup = BeautifulSoup(response.text, "lxml")
        table = soup.find("table")

        if not table:
            return None

        songs = []
        rows = table.find_all("tr")

        for row in rows[1:21]:  # Top 20 songs
            cells = row.find_all("td")
            if len(cells) < 3:
                continue

            try:
                # Kworb Spotify songs table:
                # Song | Streams | Streams+ | Pk | PkStreams
                title_cell = cells[0]
                title = title_cell.get_text(strip=True)

                # Get total streams
                streams_text = cells[1].get_text(strip=True).replace(",", "")
                streams = float(streams_text) if streams_text.isdigit() else None

                songs.append({
                    "title": title,
                    "total_streams": streams,
                })

            except Exception:
                continue

        logger.info(
            f"  Spotify history: {len(songs)} songs for {artist_slug}"
        )
        return {"top_songs_by_streams": songs}

    except Exception as e:
        logger.debug(f"  Spotify history error for {artist_slug}: {e}")
        return None


def save_global_metrics(
    artist_id: str,
    artist_slug: str,
    presence: dict
) -> int:
    """Save global presence metrics to artist_snapshots."""
    db = get_supabase()
    saved = 0

    metrics = {
        "kworb_countries_charting": presence.get("countries_charting"),
        "kworb_songs_charting": presence.get("songs_count"),
        "kworb_global_score": presence.get("global_score"),
    }

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
            logger.error(f"  DB error saving {metric_name}: {e}")

    # Save countries list as text metric
    if presence.get("countries_list"):
        try:
            db.table("artist_snapshots").upsert(
                {
                    "artist_id": artist_id,
                    "source": SOURCE,
                    "metric_name": "countries_list",
                    "metric_value": presence["countries_charting"],
                    "metric_text": ",".join(presence["countries_list"]),
                    "snapshot_date": TODAY,
                    "captured_at": datetime.now().isoformat(),
                },
                on_conflict="artist_id,source,metric_name,snapshot_date"
            ).execute()
        except Exception as e:
            logger.error(f"  DB error saving countries_list: {e}")

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
            "id, name, slug, spotify_id"
        ).eq("is_active", True).execute()

        artists = {a["slug"]: a for a in result.data}

        for artist_slug, kworb_slug in ARTIST_KWORB_SLUGS.items():
            artist = artists.get(artist_slug)
            if not artist:
                logger.warning(f"Artist not found in DB: {artist_slug}")
                continue

            records_attempted += 1
            logger.info(
                f"Processing: {artist['name']} ({kworb_slug})"
            )

            # Scrape global presence
            presence = scrape_artist_global_presence(
                artist_slug, kworb_slug
            )

            if presence:
                saved = save_global_metrics(
                    artist["id"], artist_slug, presence
                )
                records_inserted += saved

                # Show top charting songs
                if presence.get("top_songs"):
                    logger.info("  Top charting songs globally:")
                    for song in presence["top_songs"][:3]:
                        logger.info(
                            f"    #{song['position']}: {song['title']}"
                        )
            else:
                records_failed += 1

            time.sleep(2)  # Polite delay

        status = (
            "success" if records_failed == 0
            else "partial" if records_inserted > 0
            else "failed"
        )
        logger.success(
            f"Complete: {records_inserted} metrics saved, "
            f"{records_failed} artists failed"
        )

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