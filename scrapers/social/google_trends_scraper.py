# scrapers/social/google_trends_scraper.py
import time
import re
from datetime import date, datetime, timedelta
from loguru import logger
from pytrends.request import TrendReq
from pytrends.exceptions import TooManyRequestsError
from database.client import get_supabase, log_scraper_run

SCRAPER_NAME = "google_trends_ng"
SOURCE = "google_trends"
TODAY = str(date.today())
BATCH_SIZE = 5
BATCH_DELAY = 20


def get_artists(db) -> list[dict]:
    result = db.table("artists").select(
        "id, name, slug, tier"
    ).eq("is_active", True).order("tier").execute()
    return result.data


def fetch_trends_batch(pytrends, artists: list[dict]) -> dict:
    keywords = [a["name"] for a in artists]
    slug_map = {a["name"]: a for a in artists}
    results = {}

    try:
        # Current week
        pytrends.build_payload(
            keywords, geo="NG",
            timeframe="now 7-d", cat=35
        )
        current_df = pytrends.interest_over_time()
        time.sleep(3)

        # Previous week for momentum
        pytrends.build_payload(
            keywords, geo="NG",
            timeframe="now 14-d", cat=35
        )
        historical_df = pytrends.interest_over_time()

        if current_df.empty:
            return {}

        for name in keywords:
            if name not in current_df.columns:
                continue

            artist = slug_map[name]
            current_avg = float(current_df[name].mean())
            current_peak = float(current_df[name].max())

            # Momentum: compare first half vs second half of 14-day window
            momentum = 0.0
            if not historical_df.empty and name in historical_df.columns:
                midpoint = len(historical_df) // 2
                first_half = float(historical_df[name].iloc[:midpoint].mean())
                second_half = float(historical_df[name].iloc[midpoint:].mean())
                if first_half > 0:
                    momentum = ((second_half - first_half) / first_half) * 100

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

    except TooManyRequestsError:
        logger.warning("Rate limited — waiting 90s...")
        time.sleep(90)
        return fetch_trends_batch(pytrends, artists)
    except Exception as e:
        logger.error(f"Trends error: {e}")

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
                logger.error(f"DB error {slug}/{metric_name}: {e}")
    return saved


def run():
    start_time = datetime.now()
    logger.info(f"=== Starting {SCRAPER_NAME} ===")
    status = "failed"
    records_inserted = 0
    error_message = None

    try:
        db = get_supabase()
        artists = get_artists(db)

        pytrends = TrendReq(
            hl="en-US", tz=60,
            timeout=(10, 30), retries=3,
            backoff_factor=1.0,
        )

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
                time.sleep(BATCH_DELAY)

        records_inserted = save_trends(db, all_results)

        # Leaderboard by momentum — most interesting signal
        sorted_by_momentum = sorted(
            all_results.items(),
            key=lambda x: x[1]["momentum_pct"],
            reverse=True
        )
        logger.info("--- Trending UP this week ---")
        for slug, d in sorted_by_momentum[:5]:
            logger.info(
                f"  {slug}: {d['avg_interest']:.0f}/100 "
                f"({d['momentum_pct']:+.1f}% momentum)"
            )

        status = "success"
        logger.success(f"Saved {records_inserted} trend metrics")

    except Exception as e:
        error_message = str(e)
        logger.error(f"Crashed: {e}")
    finally:
        duration = (datetime.now() - start_time).total_seconds()
        log_scraper_run(
            scraper_name=SCRAPER_NAME, status=status,
            records_attempted=len(artists) if 'artists' in locals() else 0,
            records_inserted=records_inserted, records_failed=0,
            error_message=error_message, duration_seconds=duration
        )
        logger.info(f"Done. {status}. {duration:.1f}s")


if __name__ == "__main__":
    run()