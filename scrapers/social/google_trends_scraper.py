# scrapers/social/google_trends_scraper.py
import time
from datetime import date, datetime, timedelta
from loguru import logger
from database.client import get_supabase, log_scraper_run
import pandas as pd
pd.set_option('future.no_silent_downcasting', True)

SCRAPER_NAME = "google_trends_ng"
SOURCE = "google_trends"
TODAY = str(date.today())
BATCH_SIZE = 5
BATCH_DELAY = 25


def get_artists(db) -> list[dict]:
    result = db.table("artists").select(
        "id, name, slug, tier"
    ).eq("is_active", True).order("tier").execute()
    return result.data


def build_pytrends():
    """
    Build TrendReq with urllib3 compatibility handling.
    Handles both old and new urllib3 versions.
    """
    from pytrends.request import TrendReq
    import requests
    from requests.adapters import HTTPAdapter

    try:
        # Try new urllib3 v2.x style first
        from urllib3.util.retry import Retry
        retry = Retry(
            total=3,
            backoff_factor=1.0,
            allowed_methods=["GET", "POST"],
        )
    except TypeError:
        # Fall back to old urllib3 v1.x style
        from urllib3.util.retry import Retry
        retry = Retry(
            total=3,
            backoff_factor=1.0,
            method_whitelist=["GET", "POST"],
        )

    # Build TrendReq with conservative settings
    pytrends = TrendReq(
        hl="en-US",
        tz=60,        # WAT = UTC+1
        timeout=(15, 30),
        retries=2,
        backoff_factor=0.8,
    )
    return pytrends


def fetch_trends_batch(pytrends, artists: list[dict]) -> dict:
    """
    Fetch Google Trends data for a batch of up to 5 artists.
    Returns {artist_slug: metrics_dict}
    """
    keywords = [a["name"] for a in artists]
    slug_map = {a["name"]: a for a in artists}
    results = {}

    try:
        # Current 7-day window
        pytrends.build_payload(
        keywords,
        geo="NG",
        timeframe="today 7-d",   # changed from "now 7-d"
    # cat=35 removed — causes 400 errors
        )
        current_df = pytrends.interest_over_time()
        time.sleep(4)

        if current_df.empty:
            logger.warning(
                f"  No data returned for: {keywords}"
            )
            return {}

        # Previous 7-day window for momentum calculation
        week_ago_end = date.today() - timedelta(days=7)
        week_ago_start = week_ago_end - timedelta(days=7)
        timeframe_prev = (
            f"{week_ago_start.strftime('%Y-%m-%d')} "
            f"{week_ago_end.strftime('%Y-%m-%d')}"
        )

        pytrends.build_payload(
        keywords,
        geo="NG",
        timeframe="today 14-d",  # simpler format
    # cat=35 removed — causes 400 errors
        )
        prev_df = pytrends.interest_over_time()
        time.sleep(4)

        for name in keywords:
            if name not in current_df.columns:
                continue

            artist = slug_map[name]
            current_avg = float(current_df[name].mean())
            current_peak = float(current_df[name].max())

            # Momentum: current week vs previous week
            momentum = 0.0
            if not prev_df.empty and name in prev_df.columns:
                prev_avg = float(prev_df[name].mean())
                if prev_avg > 0:
                    momentum = ((current_avg - prev_avg) / prev_avg) * 100

            results[artist["slug"]] = {
                "artist_id": artist["id"],
                "tier": artist["tier"],
                "avg_interest": round(current_avg, 2),
                "peak_interest": round(current_peak, 2),
                "momentum_pct": round(momentum, 2),
            }

            logger.info(
                f"  {name}: avg={current_avg:.1f} | "
                f"peak={current_peak:.1f} | "
                f"momentum={momentum:+.1f}%"
            )

    except Exception as e:
        logger.error(f"  Trends batch error: {e}")
        import traceback
        logger.debug(traceback.format_exc())

    return results


def save_trends(db, results: dict) -> int:
    saved = 0
    for slug, data in results.items():
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
                    "metric_value": value,
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

        for i in range(0, len(artists), BATCH_SIZE):
            batch = artists[i:i + BATCH_SIZE]
            logger.info(
                f"Batch {i//BATCH_SIZE + 1}: "
                f"{[a['name'] for a in batch]}"
            )
            results = fetch_trends_batch(pytrends, batch)
            all_results.update(results)

            if i + BATCH_SIZE < len(artists):
                logger.debug(f"  Waiting {BATCH_DELAY}s...")
                time.sleep(BATCH_DELAY)

        records_inserted = save_trends(db, all_results)

        # Show momentum leaderboard
        if all_results:
            sorted_momentum = sorted(
                all_results.items(),
                key=lambda x: x[1]["momentum_pct"],
                reverse=True
            )
            logger.info("--- Trending UP in Nigeria ---")
            for slug, d in sorted_momentum[:5]:
                logger.info(
                    f"  {slug}: {d['avg_interest']:.0f}/100 "
                    f"({d['momentum_pct']:+.1f}% week-over-week)"
                )

        status = "success"
        logger.success(f"Saved {records_inserted} trend metrics")

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
            records_failed=0,
            error_message=error_message,
            duration_seconds=duration,
        )
        logger.info(f"Done. {status}. {duration:.1f}s")


if __name__ == "__main__":
    run()