# scrapers/streaming/audiomack_scraper.py
# Scrapes Audiomack artist metrics and top tracks
# Captures: followers, total plays, monthly listeners per artist
# Captures: play count, likes, playlist adds per track
# No API key required — all public data

import re
import time
from datetime import date, datetime
from loguru import logger
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout
from database.client import get_supabase, log_scraper_run

SCRAPER_NAME = "audiomack_artist_snapshots"
SOURCE = "audiomack"
BASE_URL = "https://audiomack.com"
TODAY = str(date.today())

# How many top tracks to capture per artist
MAX_TRACKS_PER_ARTIST = 10

# Polite delay between artist requests (seconds)
DELAY_BETWEEN_ARTISTS = 3


# ── Metric Cleaning ───────────────────────────────────────────────────────────

def parse_number(text: str) -> float | None:
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
        logger.debug(f"Could not parse number: {text!r}")
        return None


# ── Artist Page Scraper ───────────────────────────────────────────────────────

def scrape_artist_profile(page, artist_slug: str) -> dict | None:
    """
    Scrapes the main artist profile page.
    Returns dict with followers, total_plays, monthly_listeners.
    Returns None if page fails to load or metrics not found.
    """
    url = f"{BASE_URL}/{artist_slug}"
    logger.info(f"  Scraping profile: {url}")

    try:
        page.goto(url, wait_until="domcontentloaded", timeout=90000)
        time.sleep(6)

        # Check page loaded correctly — look for artist name
        page_text = page.inner_text("body")

        if "404" in page.title() or "not found" in page_text.lower()[:200]:
            logger.warning(f"  Artist page not found: {artist_slug}")
            return None

        metrics = {}

        # ── Strategy 1: Look for stat elements directly ──────────────────────
        # Audiomack renders stats in elements with specific class patterns
        stat_selectors = [
            "[class*='stat']",
            "[class*='Stats']",
            "[class*='count']",
            "[class*='Count']",
            "[class*='metric']",
        ]

        for selector in stat_selectors:
            stat_els = page.query_selector_all(selector)
            if stat_els:
                for el in stat_els:
                    try:
                        text = el.inner_text().strip()
                        # Look for number-label pairs
                        lines = [l.strip() for l in text.split('\n')
                                 if l.strip()]
                        if len(lines) >= 2:
                            value = parse_number(lines[0])
                            label = lines[1].lower()
                            if value is not None:
                                if "follower" in label:
                                    metrics["followers"] = value
                                elif "play" in label and "monthly" not in label:
                                    metrics["total_plays"] = value
                                elif "monthly" in label or "listener" in label:
                                    metrics["monthly_listeners"] = value
                    except Exception:
                        continue

        # ── Strategy 2: Parse from full page text ────────────────────────────
        # Fallback when selector approach finds nothing
        if not metrics:
            logger.debug(
                f"  Stat selectors found nothing for {artist_slug} "
                "— trying text parsing"
            )
            metrics = parse_profile_from_text(page_text)

        if metrics:
            logger.info(
                f"  Profile metrics: "
                f"followers={metrics.get('followers')}, "
                f"plays={metrics.get('total_plays')}, "
                f"monthly={metrics.get('monthly_listeners')}"
            )
        else:
            logger.warning(
                f"  No metrics found for {artist_slug}"
            )

        return metrics if metrics else None

    except PlaywrightTimeout:
        logger.error(f"  Timeout loading {url}")
        return None
    except Exception as e:
        logger.error(f"  Error scraping {artist_slug}: {e}")
        return None


def parse_profile_from_text(page_text: str) -> dict:
    """
    Extracts artist metrics from raw page text.
    Looks for number + label patterns in the rendered text.
    """
    metrics = {}
    lines = [l.strip() for l in page_text.split('\n') if l.strip()]

    # Patterns to search for in adjacent line pairs
    label_map = {
        "followers": "followers",
        "following": None,           # skip
        "plays": "total_plays",
        "total plays": "total_plays",
        "monthly listeners": "monthly_listeners",
        "listeners": "monthly_listeners",
    }

    for i, line in enumerate(lines):
        label_lower = line.lower()
        if label_lower in label_map and label_map[label_lower]:
            # Value is usually the line BEFORE the label
            if i > 0:
                value = parse_number(lines[i - 1])
                if value is not None:
                    metrics[label_map[label_lower]] = value
            # Sometimes value is AFTER the label
            if not metrics.get(label_map[label_lower]) and i + 1 < len(lines):
                value = parse_number(lines[i + 1])
                if value is not None:
                    metrics[label_map[label_lower]] = value

    return metrics


# ── Top Tracks Scraper ────────────────────────────────────────────────────────

def scrape_artist_top_tracks(page, artist_slug: str) -> list[dict]:
    """
    Scrapes top tracks from audiomack.com/artist-name/songs.
    Parses the repeating Artist → Title → Date pattern from page text,
    then visits each track page to get play counts.
    """
    url = f"{BASE_URL}/{artist_slug}/songs"
    logger.info(f"  Scraping top tracks: {url}")

    try:
        page.goto(url, wait_until="domcontentloaded", timeout=90000)
        time.sleep(6)  # was 3

        # Click Top Tracks tab
        for selector in [
            "button:has-text('Top Tracks')",
            "span:has-text('Top Tracks')",
        ]:
            try:
                el = page.query_selector(selector)
                if el and el.is_visible():
                    el.click()
                    time.sleep(2)
                    break
            except Exception:
                continue

        page_text = page.inner_text("body")
        lines = [l.strip() for l in page_text.split('\n') if l.strip()]

        # Find where the track list starts — after "Recent Tracks" tab
        start_idx = 0
        for i, line in enumerate(lines):
            if line == "Recent Tracks":
                start_idx = i + 1
                break

        track_lines = lines[start_idx:]
        logger.debug(
            f"  Track section lines (first 20): {track_lines[:20]}"
        )

        # Parse triplets: Artist Name → Track Title → Date
        tracks = parse_track_triplets(track_lines, artist_slug)

        logger.info(
            f"  Found {len(tracks)} tracks, fetching play counts..."
        )

        # Visit each track page for play counts
        for track in tracks[:MAX_TRACKS_PER_ARTIST]:
            if track.get("url"):
                detail = scrape_track_page(page, track["url"])
                if detail:
                    track.update(detail)
            time.sleep(1)

        logger.info(
            f"  Completed {len(tracks)} tracks for {artist_slug}"
        )
        return tracks[:MAX_TRACKS_PER_ARTIST]

    except PlaywrightTimeout:
        logger.error(f"  Timeout: {url}")
        return []
    except Exception as e:
        logger.error(f"  Error scraping tracks for {artist_slug}: {e}")
        return []

def parse_track_triplets(lines: list[str], artist_slug: str) -> list[dict]:
    """
    Audiomack renders each track as:
      Artist Name
      Track Title
      Album: ...      (optional — not always present)
      Release Date:   (literal anchor line — always present)
      September 15, 2022

    We anchor on the literal "Release Date:" line and look backwards
    for title (1 or 2 lines up depending on album presence).
    """
    import re as re_module

    tracks = []
    seen_titles = set()

    def is_skip(s: str) -> bool:
        skip_exact = {
            "all", "top tracks", "albums", "playlists", "re-ups",
            "likes", "followers", "following", "plays",
            "monthly listeners", "load more", "load more top tracks",
            "discover", "originals", "create an account", "sign in",
            "upload", "follow", "unfollow", "about", "help",
            "business inquiries", "styleguide", "creator app",
            "legal & dmca", "privacy policy", "terms of service",
            "report a vulnerability", "do not sell my info",
            "your privacy rights", "top tracks", "recent tracks",
        }
        lower = s.lower().strip()
        if lower in skip_exact:
            return True
        if s.startswith(('©', '@', '#', '|', '+')):
            return True
        if s.startswith(('Label:', 'Genre:', 'by\xa0', 'by ', 'Feat.')):
            return True
        if 'Member Since' in s or 'Read more' in s:
            return True
        return False

    for i, line in enumerate(lines):
        # Anchor: find the literal "Release Date:" label
        if line.strip() != "Release Date:":
            continue

        # Date is the line AFTER "Release Date:"
        if i + 1 >= len(lines):
            continue
        release_date = lines[i + 1].strip()

        # Now look BACKWARDS for title and artist
        # Pattern A (with album): Artist, Title, Album:..., Release Date:
        # Pattern B (no album):   Artist, Title, Release Date:
        title = None
        artist_credit = None

        if i >= 2:
            candidate_title = lines[i - 1].strip()
            candidate_artist = lines[i - 2].strip()

            # Pattern B: no album line
            if (
                not candidate_title.startswith("Album:")
                and not is_skip(candidate_title)
                and len(candidate_title) > 1
                and len(candidate_title) < 120
            ):
                title = candidate_title
                artist_credit = candidate_artist

        if title is None and i >= 3:
            # Pattern A: album line present
            candidate_album = lines[i - 1].strip()
            candidate_title = lines[i - 2].strip()
            candidate_artist = lines[i - 3].strip()

            if (
                candidate_album.startswith("Album:")
                and not candidate_title.startswith("Album:")
                and not is_skip(candidate_title)
                and len(candidate_title) > 1
                and len(candidate_title) < 120
            ):
                title = candidate_title
                artist_credit = candidate_artist

        if not title or title in seen_titles:
            continue

        # Skip obvious non-track lines
        if is_skip(title):
            continue

        seen_titles.add(title)

        # Build track URL
        title_slug = re.sub(
            r'[^a-z0-9]+', '-', title.lower()
        ).strip('-')
        track_url = f"{BASE_URL}/{artist_slug}/song/{title_slug}"

        tracks.append({
            "title": title,
            "artist_credit": artist_credit or "",
            "release_date": release_date,
            "url": track_url,
            "plays": None,
            "likes": None,
        })

        logger.debug(
            f"  Track: '{title}' by '{artist_credit}' ({release_date})"
        )

    return tracks

    
def scrape_track_page(page, track_url: str) -> dict:
    """
    Visits individual track page to extract play count and likes.

    Audiomack track page renders stats as:
      "123.4K"   ← number
      "Plays"    ← label on next line

    So we look for the label then check the line BEFORE it.
    """
    try:
        page.goto(track_url, wait_until="domcontentloaded", timeout=60000)
        time.sleep(3)  # was 2

        # Check we actually landed on a track page (not 404)
        page_text = page.inner_text("body")
        if "has no content yet" in page_text or "404" in page.title():
            logger.debug(f"  Track page not found: {track_url}")
            return {}

        lines = [l.strip() for l in page_text.split('\n') if l.strip()]

        detail = {
            "plays": None,
            "likes": None,
        }

        for i, line in enumerate(lines):
            lower = line.lower()

            # Play count: label "Plays" or "Streams", value is line before
            if lower in ["plays", "streams", "total plays"]:
                if i > 0:
                    value = parse_number(lines[i - 1])
                    if value and value > 0:
                        detail["plays"] = value
                        logger.debug(
                            f"  Found plays={value} at line {i}"
                        )

            # Likes: label "Likes", value is line before
            if lower == "likes":
                if i > 0:
                    value = parse_number(lines[i - 1])
                    if value and value > 0:
                        detail["likes"] = value

        return detail

    except Exception as e:
        logger.debug(f"  Track page error {track_url}: {e}")
        return {}
    


def extract_tracks_from_elements(elements) -> list[dict]:
    """
    Extracts track data from DOM elements.
    """
    tracks = []

    for el in elements:
        try:
            text = el.inner_text().strip()
            lines = [l.strip() for l in text.split('\n') if l.strip()]

            if len(lines) < 2:
                continue

            track = {"raw_text": text}

            # First non-empty line is usually the title
            track["title"] = lines[0]

            # Look for play counts, likes in remaining lines
            for line in lines[1:]:
                value = parse_number(line)
                if value is not None:
                    if "plays" not in track:
                        track["plays"] = value
                    elif "likes" not in track:
                        track["likes"] = value

            tracks.append(track)

        except Exception as e:
            logger.debug(f"  Failed to parse track element: {e}")
            continue

    return tracks


def extract_track_titles_from_text(page, artist_slug: str) -> list[dict]:
    """
    Fallback: extracts track titles from page text when link selectors fail.
    Returns list of title-only dicts (no URLs, no play counts).
    """
    page_text = page.inner_text("body")
    lines = [l.strip() for l in page_text.split('\n') if l.strip()]

    skip_exact = {
        "all", "top tracks", "albums", "playlists", "re-ups", "likes",
        "followers", "following", "plays", "monthly listeners",
        "load more", "load more top tracks", "discover", "originals",
        "create an account", "sign in", "upload", "follow", "unfollow",
        "share", "more", "get plus", "about", "help", "business inquiries",
        "styleguide", "creator app", "legal & dmca", "privacy policy",
        "terms of service", "report a vulnerability", "do not sell my info",
        "your privacy rights", "top tracks", "recent tracks",
        artist_slug.lower(),
        f"@{artist_slug.lower()}",
    }

    skip_starts = [
        "member since", "audiomack is", "http", "@", "album:",
        "release date:", "by\xa0", "by ", "#", "label:", "genre:",
        "© ", "total account plays", "monthly listeners",
    ]

    tracks = []
    seen_titles = set()

    for line in lines:
        lower = line.lower().strip()

        if lower in skip_exact:
            continue
        if any(lower.startswith(s) for s in skip_starts):
            continue
        if parse_number(line) is not None:
            continue
        if len(line) < 2 or len(line) > 100:
            continue
        if line in seen_titles:
            continue

        seen_titles.add(line)
        tracks.append({"title": line, "url": None})

    return tracks[:MAX_TRACKS_PER_ARTIST]


# ── Individual Track Page Scraper ─────────────────────────────────────────────

def scrape_track_detail(page, track_url: str) -> dict | None:
    """
    Scrapes individual track page for detailed metrics.
    audiomack.com/artist/song/track-name
    Gets: total plays, likes, playlist adds, release date.
    Only call this for your most important tracks — it's slow.
    """
    logger.debug(f"  Scraping track detail: {track_url}")

    try:
        page.goto(track_url, wait_until="domcontentloaded", timeout=60000)
        time.sleep(3)  # was 2

        page_text = page.inner_text("body")
        lines = [l.strip() for l in page_text.split('\n') if l.strip()]

        detail = {
            "url": track_url,
            "plays": None,
            "likes": None,
            "playlist_adds": None,
            "release_date": None,
        }

        for i, line in enumerate(lines):
            lower = line.lower()

            # Release date pattern
            if "released" in lower or "release date" in lower:
                if i + 1 < len(lines):
                    detail["release_date"] = lines[i + 1]

            # Plays
            if lower in ["plays", "total plays", "streams"]:
                if i > 0:
                    value = parse_number(lines[i - 1])
                    if value:
                        detail["plays"] = value

            # Likes
            if lower in ["likes", "hearts"]:
                if i > 0:
                    value = parse_number(lines[i - 1])
                    if value:
                        detail["likes"] = value

            # Playlist adds
            if "playlist" in lower and "add" in lower:
                if i > 0:
                    value = parse_number(lines[i - 1])
                    if value:
                        detail["playlist_adds"] = value

        return detail

    except Exception as e:
        logger.debug(f"  Track detail failed for {track_url}: {e}")
        return None


# ── Database Saves ────────────────────────────────────────────────────────────

def save_artist_metrics(
    artist_id: str,
    metrics: dict
) -> int:
    """
    Saves artist snapshot metrics to artist_snapshots table.
    Returns number of metrics saved.
    """
    db = get_supabase()
    saved = 0

    metric_map = {
        "followers": "followers",
        "total_plays": "total_plays",
        "monthly_listeners": "monthly_listeners",
    }

    for metric_key, metric_name in metric_map.items():
        value = metrics.get(metric_key)
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
                f"  DB error saving {metric_name} "
                f"for artist {artist_id}: {e}"
            )

    return saved


def save_track_snapshots(
    artist_id: str,
    artist_slug: str,
    tracks: list[dict]
) -> int:
    """
    Saves track records and snapshots.
    Saves title even if no play count — play count saved only when present.
    """
    db = get_supabase()
    saved = 0

    for track in tracks:
        title = track.get("title")
        if not title:
            continue

        # Skip obvious non-track titles
        skip = {
            "total account plays", "monthly listeners", "followers",
            "discover", "originals", "create an account", "sign in", "total account plays",
              "monthly listeners", "followers",
        "discover", "originals", "create an account", "sign in",
        "tems", "wizkid", "burna boy", "asake", "davido", "rema",
        "ayra starr", "fireboy dml", "bnxn", "omah lay",
        }

        if title.lower() in skip:
            continue

        try:
            track_slug = (
                re.sub(r'[^a-z0-9]+', '-', title.lower()).strip('-')
            )
            track_slug = f"{track_slug}-{artist_slug}"[:100]

            result = db.table("tracks").upsert(
                {
                    "artist_id": artist_id,
                    "title": title,
                    "slug": track_slug,
                    "release_date": track.get("release_date"),
                },
                on_conflict="slug"
            ).execute()

            if not result.data:
                continue

            track_id = result.data[0]["id"]

            # Only save snapshots when we have actual values
            if track.get("plays") is not None:
                db.table("track_snapshots").upsert(
                    {
                        "track_id": track_id,
                        "source": SOURCE,
                        "metric_name": "plays",
                        "metric_value": track["plays"],
                        "snapshot_date": TODAY,
                        "captured_at": datetime.now().isoformat(),
                    },
                    on_conflict="track_id,source,metric_name,snapshot_date"
                ).execute()

            if track.get("likes") is not None:
                db.table("track_snapshots").upsert(
                    {
                        "track_id": track_id,
                        "source": SOURCE,
                        "metric_name": "likes",
                        "metric_value": track["likes"],
                        "snapshot_date": TODAY,
                        "captured_at": datetime.now().isoformat(),
                    },
                    on_conflict="track_id,source,metric_name,snapshot_date"
                ).execute()

            saved += 1
            logger.debug(
                f"  Saved: '{title}' | "
                f"plays={track.get('plays')} | "
                f"likes={track.get('likes')}"
            )

        except Exception as e:
            logger.error(f"  DB error saving '{title}': {e}")
            continue

    return saved


# ── Main Orchestrator ─────────────────────────────────────────────────────────

def run():
    """
    Main entry point.
    Fetches all active artists from DB, scrapes Audiomack for each.
    """
    start_time = datetime.now()
    logger.info(f"=== Starting {SCRAPER_NAME} ===")

    status = "failed"
    records_attempted = 0
    records_inserted = 0
    records_failed = 0
    error_message = None

    try:
        # Load active artists that have an audiomack slug
        db = get_supabase()
        result = db.table("artists").select(
            "id, name, slug, audiomack_slug, tier"
        ).eq(
            "is_active", True
        ).not_.is_(
            "audiomack_slug", "null"
        ).order("tier").execute()

        artists = result.data

        if not artists:
            logger.warning(
                "No artists with audiomack_slug found in database. "
                "Add artists first via the seed script."
            )
            return

        logger.info(
            f"Found {len(artists)} artists to scrape"
        )

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120.0.0.0 Safari/537.36"
                ),
                viewport={"width": 1280, "height": 900},
                locale="en-US",
            )
            page = context.new_page()

            for i, artist in enumerate(artists, 1):
                artist_id = artist["id"]
                artist_name = artist["name"]
                audiomack_slug = artist["audiomack_slug"]

                logger.info(
                    f"[{i}/{len(artists)}] {artist_name} "
                    f"(@{audiomack_slug})"
                )
                records_attempted += 1

                # Scrape profile metrics
                profile = scrape_artist_profile(page, audiomack_slug)

                if profile:
                    saved = save_artist_metrics(artist_id, profile)
                    records_inserted += saved
                    logger.info(
                        f"  Saved {saved} profile metrics"
                    )
                else:
                    records_failed += 1
                    logger.warning(
                        f"  No profile data for {artist_name}"
                    )

                # Scrape top tracks
                tracks = scrape_artist_top_tracks(page, audiomack_slug)

                if tracks:
                    saved = save_track_snapshots(
                        artist_id, audiomack_slug, tracks
                    )
                    records_inserted += saved
                    logger.info(
                        f"  Saved {saved} track snapshots"
                    )

                    # Preview first 3 tracks
                    for t in tracks[:3]:
                        logger.info(
                            f"    - {t.get('title')} | "
                            f"plays={t.get('plays')} | "
                            f"likes={t.get('likes')}"
                        )

                # Polite delay between artists
                if i < len(artists):
                    logger.debug(
                        f"  Waiting {DELAY_BETWEEN_ARTISTS}s "
                        "before next artist..."
                    )
                    time.sleep(DELAY_BETWEEN_ARTISTS)

            browser.close()

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