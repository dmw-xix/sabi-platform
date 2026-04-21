# scrapers/social/google_trends_scraper.py
import os
import time
import pandas as pd

# Load .env BEFORE any os.getenv calls
from dotenv import load_dotenv
load_dotenv()

from datetime import date, datetime, timedelta
from loguru import logger
from database.client import get_supabase, log_scraper_run

pd.set_option('future.no_silent_downcasting', True)

SCRAPER_NAME = "google_trends_ng"
SOURCE = "google_trends"
TODAY = str(date.today())
BATCH_SIZE = 3    # Reduced from 5 — fewer keywords = fewer 400s
BATCH_DELAY = 30  # Longer delay between batches


def get_artists(db) -> list[dict]:
    result = db.table("artists").select(
        "id, name, slug, tier"
    ).eq("is_active", True).order("tier").execute()
    return result.data


def build_pytrends():
    """
    Build pytrends with cookie auth to avoid Google 400 blocks.
    Falls back to cookieless if no cookie configured.
    """
    from pytrends.request import TrendReq

    cookie = os.getenv("GOOGLE_TRENDS_COOKIE", "")

    requests_args = {}
    if cookie:
        requests_args = {
            "headers": {
                "Cookie": cookie,
                "User-Agent": (
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/122.0.0.0 Safari/537.36"
                ),
            }
        }
        logger.info("Using cookie authentication for Google Trends")
    else:
        logger.warning(
            "No GOOGLE_TRENDS_COOKIE set — may hit 400 errors. "
            "Add cookie from trends.google.com to .env"
        )

    pytrends = TrendReq(
        hl="en-US",
        tz=60,
        timeout=(20, 40),
        retries=3,
        backoff_factor=2.0,
        requests_args=requests_args,
    )
    return pytrends


def fetch_single_artist(
    pytrends, artist: dict
) -> dict | None:
    """
    Fetch trends for ONE artist at a time.
    More reliable than batches when Google is restrictive.
    """
    name = artist["name"]

    try:
        pytrends.build_payload(
            [name],
            geo="NG",
            timeframe="today 7-d",
        )
        current_df = pytrends.interest_over_time()
        time.sleep(5)

        if current_df.empty or name not in current_df.columns:
            logger.warning(f"  No data for: {name}")
            return None

        current_avg = float(current_df[name].mean())
        current_peak = float(current_df[name].max())

        # Previous week for momentum
        pytrends.build_payload(
            [name],
            geo="NG",
            timeframe="today 14-d",
        )
        prev_df = pytrends.interest_over_time()
        time.sleep(5)

        momentum = 0.0
        if not prev_df.empty and name in prev_df.columns:
            # First half of 14-day = previous week
            midpoint = len(prev_df) // 2
            prev_avg = float(prev_df[name].iloc[:midpoint].mean())
            if prev_avg > 0:
                momentum = ((current_avg - prev_avg) / prev_avg) * 100

        logger.info(
            f"  {name}: avg={current_avg:.1f} | "
            f"peak={current_peak:.1f} | "
            f"momentum={momentum:+.1f}%"
        )

        return {
            "artist_id": artist["id"],
            "tier": artist["tier"],
            "avg_interest": round(current_avg, 2),
            "peak_interest": round(current_peak, 2),
            "momentum_pct": round(momentum, 2),
        }

    except Exception as e:
        error_str = str(e)
        if "400" in error_str:
            logger.warning(
                f"  Google blocked request for {name} — "
                "add GOOGLE_TRENDS_COOKIE to .env"
            )
        else:
            logger.error(f"  Trends error for {name}: {e}")
        return None


def save_trends(db, slug: str, data: dict) -> int:
    saved = 0
    for metric_name, value in [
        ("google_trends_avg", data["avg_interest"]),
        ("google_trends_peak", data["peak_interest"]),
        ("google_trends_momentum", data["momentum_pct"]),
    ]:
        try:
            db.table("artist_snapshots").upsert({
                "artist_id": data["artist_id"],
                "source": SOURCE,
                "metric_name": metric_name,
                "metric_value": float(value),
                "snapshot_date": TODAY,
                "captured_at": datetime.now().isoformat(),
            }, on_conflict="artist_id,source,metric_name,snapshot_date").execute()
            saved += 1
        except Exception as e:
            logger.error(f"  DB error {slug}/{metric_name}: {e}")
    return saved


def run():
    start_time = datetime.now()
    logger.info(f"=== Starting {SCRAPER_NAME} ===")
    status = "failed"
    records_inserted = 0
    records_failed = 0
    error_message = None
    artists = []

    try:
        db = get_supabase()
        artists = get_artists(db)

        if not artists:
            logger.warning("No artists found")
            return

        logger.info(f"Fetching trends for {len(artists)} artists")
        pytrends = build_pytrends()
        all_results = {}

        for i, artist in enumerate(artists, 1):
            logger.info(
                f"[{i}/{len(artists)}] {artist['name']}"
            )

            data = fetch_single_artist(pytrends, artist)

            if data:
                saved = save_trends(db, artist["slug"], data)
                records_inserted += saved
                all_results[artist["slug"]] = data
            else:
                records_failed += 1

            # Delay between each artist
            if i < len(artists):
                time.sleep(BATCH_DELAY)

        # Momentum leaderboard
        if all_results:
            sorted_m = sorted(
                all_results.items(),
                key=lambda x: x[1]["momentum_pct"],
                reverse=True,
            )
            logger.info("--- Trending UP in Nigeria ---")
            for slug, d in sorted_m[:5]:
                logger.info(
                    f"  {slug}: {d['avg_interest']:.0f}/100 "
                    f"({d['momentum_pct']:+.1f}% WoW)"
                )

        status = (
            "success" if records_failed == 0
            else "partial" if records_inserted > 0
            else "failed"
        )
        logger.success(f"Saved {records_inserted} metrics")

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
            records_attempted=len(artists),
            records_inserted=records_inserted,
            records_failed=records_failed,
            error_message=error_message,
            duration_seconds=duration,
        )
        logger.info(f"Done. {status}. {duration:.1f}s")


if __name__ == "__main__":
    run()