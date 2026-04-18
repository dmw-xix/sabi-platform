# intelligence/milestones/milestone_tracker.py
# Detects and records small wins for Tier 3-4 artists
# These are the signals that matter before mainstream charts

from datetime import datetime, date, timedelta
from loguru import logger
from database.client import get_supabase, log_scraper_run

SCRAPER_NAME = "milestone_tracker"

# Define milestone thresholds
MILESTONES = {
    # Audiomack follower milestones
    "audiomack_1k_followers": {
        "source": "audiomack",
        "metric": "followers",
        "threshold": 1_000,
        "text": "Crossed 1,000 Audiomack followers",
        "tier_relevant": [3, 4],
    },
    "audiomack_10k_followers": {
        "source": "audiomack",
        "metric": "followers",
        "threshold": 10_000,
        "text": "Crossed 10,000 Audiomack followers",
        "tier_relevant": [3, 4],
    },
    "audiomack_100k_followers": {
        "source": "audiomack",
        "metric": "followers",
        "threshold": 100_000,
        "text": "Crossed 100,000 Audiomack followers",
        "tier_relevant": [2, 3],
    },
    "audiomack_1m_followers": {
        "source": "audiomack",
        "metric": "followers",
        "threshold": 1_000_000,
        "text": "Crossed 1 million Audiomack followers",
        "tier_relevant": [1, 2],
    },
    # Monthly listener milestones
    "audiomack_10k_monthly": {
        "source": "audiomack",
        "metric": "monthly_listeners",
        "threshold": 10_000,
        "text": "Crossed 10,000 monthly listeners on Audiomack",
        "tier_relevant": [3, 4],
    },
    "audiomack_100k_monthly": {
        "source": "audiomack",
        "metric": "monthly_listeners",
        "threshold": 100_000,
        "text": "Crossed 100,000 monthly listeners on Audiomack",
        "tier_relevant": [2, 3],
    },
    "audiomack_1m_monthly": {
        "source": "audiomack",
        "metric": "monthly_listeners",
        "threshold": 1_000_000,
        "text": "Crossed 1 million monthly listeners on Audiomack",
        "tier_relevant": [1, 2],
    },
    # YouTube milestones
    "youtube_1k_subs": {
        "source": "youtube",
        "metric": "youtube_subscribers",
        "threshold": 1_000,
        "text": "Crossed 1,000 YouTube subscribers",
        "tier_relevant": [3, 4],
    },
    "youtube_10k_subs": {
        "source": "youtube",
        "metric": "youtube_subscribers",
        "threshold": 10_000,
        "text": "Crossed 10,000 YouTube subscribers",
        "tier_relevant": [3, 4],
    },
    "youtube_100k_subs": {
        "source": "youtube",
        "metric": "youtube_subscribers",
        "threshold": 100_000,
        "text": "Crossed 100,000 YouTube subscribers — Silver Play Button eligible",
        "tier_relevant": [2, 3],
    },
    # Shazam milestones
    "first_shazam_top200": {
        "type": "chart_entry",
        "chart": "shazam_ng_top200",
        "text": "First entry on Shazam Nigeria Top 200",
        "tier_relevant": [3, 4],
    },
    "first_shazam_top50": {
        "type": "chart_entry",
        "chart": "shazam_ng_top200",
        "position_threshold": 50,
        "text": "First entry in Shazam Nigeria Top 50",
        "tier_relevant": [2, 3],
    },
    # Spotify milestones
    "first_spotify_ng_chart": {
        "type": "chart_entry",
        "chart": "spotify_ng_daily",
        "text": "First entry on Spotify Nigeria Daily Chart",
        "tier_relevant": [3, 4],
    },
    "spotify_ng_top50": {
        "type": "chart_entry",
        "chart": "spotify_ng_daily",
        "position_threshold": 50,
        "text": "First time in Spotify Nigeria Top 50",
        "tier_relevant": [2, 3],
    },
    "spotify_ng_top10": {
        "type": "chart_entry",
        "chart": "spotify_ng_daily",
        "position_threshold": 10,
        "text": "First time in Spotify Nigeria Top 10 🔥",
        "tier_relevant": [1, 2],
    },
    # TurnTable milestone
    "first_turntable_entry": {
        "type": "chart_entry",
        "chart": "turntable_ng_top100",
        "text": "First entry on the Official Nigeria Top 100",
        "tier_relevant": [2, 3],
    },
    # Press milestone
    "first_press_mention": {
        "type": "press",
        "text": "First press mention in a major Nigerian music publication",
        "tier_relevant": [3, 4],
    },
    # Google Trends milestone
    "google_trends_first_spike": {
        "source": "google_trends",
        "metric": "google_trends_avg",
        "threshold": 25,
        "text": "First significant Google search interest spike in Nigeria",
        "tier_relevant": [3, 4],
    },
    # Velocity milestone
    "audiomack_40pct_weekly_growth": {
        "type": "velocity",
        "metric": "monthly_listeners",
        "source": "audiomack",
        "growth_threshold": 40,
        "text": "Audiomack monthly listeners grew 40%+ in one week",
        "tier_relevant": [3, 4],
    },
}


def already_achieved(
    db, artist_id: str, milestone_type: str
) -> bool:
    """Check if this milestone was already recorded."""
    result = db.table("artist_milestones").select("id").eq(
        "artist_id", artist_id
    ).eq("milestone_type", milestone_type).execute()
    return len(result.data) > 0


def check_metric_milestones(
    db, artist: dict
) -> list[dict]:
    """Check all metric-based milestones for an artist."""
    artist_id = artist["id"]
    tier = artist.get("tier", 2)
    achieved = []

    for milestone_id, config in MILESTONES.items():
        # Skip if not relevant for this tier
        if tier not in config.get("tier_relevant", [1, 2, 3, 4]):
            continue

        # Skip non-metric milestones
        if config.get("type") and config["type"] != "velocity":
            continue

        # Check if already achieved
        if already_achieved(db, artist_id, milestone_id):
            continue

        if config.get("type") == "velocity":
            # Check week-over-week growth
            current = db.table("artist_snapshots").select(
                "metric_value"
            ).eq("artist_id", artist_id).eq(
                "source", config["source"]
            ).eq("metric_name", config["metric"]).order(
                "snapshot_date", desc=True
            ).limit(1).execute()

            previous = db.table("artist_snapshots").select(
                "metric_value"
            ).eq("artist_id", artist_id).eq(
                "source", config["source"]
            ).eq("metric_name", config["metric"]).gte(
                "snapshot_date",
                str(date.today() - timedelta(days=14))
            ).lt(
                "snapshot_date",
                str(date.today() - timedelta(days=6))
            ).order("snapshot_date", desc=True).limit(1).execute()

            if current.data and previous.data:
                curr = current.data[0]["metric_value"]
                prev = previous.data[0]["metric_value"]
                if prev and prev > 0:
                    growth = (curr - prev) / prev * 100
                    if growth >= config["growth_threshold"]:
                        achieved.append({
                            "milestone_type": milestone_id,
                            "milestone_value": round(growth, 1),
                            "milestone_text": config["text"],
                            "source": config["source"],
                        })

        else:
            # Standard threshold check
            result = db.table("artist_snapshots").select(
                "metric_value"
            ).eq("artist_id", artist_id).eq(
                "source", config.get("source", "")
            ).eq("metric_name", config.get("metric", "")).order(
                "snapshot_date", desc=True
            ).limit(1).execute()

            if result.data:
                value = result.data[0]["metric_value"]
                if value and value >= config.get("threshold", 0):
                    achieved.append({
                        "milestone_type": milestone_id,
                        "milestone_value": value,
                        "milestone_text": config["text"],
                        "source": config.get("source", ""),
                    })

    return achieved


def check_chart_milestones(db, artist: dict) -> list[dict]:
    """Check chart entry milestones."""
    artist_id = artist["id"]
    artist_name = artist["name"]
    tier = artist.get("tier", 2)
    achieved = []

    for milestone_id, config in MILESTONES.items():
        if config.get("type") != "chart_entry":
            continue
        if tier not in config.get("tier_relevant", [1, 2, 3, 4]):
            continue
        if already_achieved(db, artist_id, milestone_id):
            continue

        chart = config.get("chart")
        pos_threshold = config.get("position_threshold", 200)

        result = db.table("chart_positions").select(
            "position, chart_date"
        ).eq("raw_artist", artist_name).eq(
            "chart_name", chart
        ).lte("position", pos_threshold).order(
            "chart_date"
        ).limit(1).execute()

        if result.data:
            achieved.append({
                "milestone_type": milestone_id,
                "milestone_value": result.data[0]["position"],
                "milestone_text": config["text"],
                "source": chart,
            })

    return achieved


def check_press_milestones(db, artist: dict) -> list[dict]:
    """Check press mention milestones."""
    artist_id = artist["id"]
    tier = artist.get("tier", 2)
    achieved = []

    milestone_id = "first_press_mention"
    config = MILESTONES[milestone_id]

    if (
        tier in config.get("tier_relevant", [])
        and not already_achieved(db, artist_id, milestone_id)
    ):
        result = db.table("press_articles").select(
            "id"
        ).contains("artist_ids", [artist_id]).limit(1).execute()

        if result.data:
            achieved.append({
                "milestone_type": milestone_id,
                "milestone_value": 1,
                "milestone_text": config["text"],
                "source": "press",
            })

    return achieved


def save_milestones(
    db, artist_id: str, milestones: list[dict]
) -> int:
    """Save achieved milestones to database."""
    saved = 0
    for m in milestones:
        try:
            db.table("artist_milestones").insert({
                "artist_id": artist_id,
                "milestone_type": m["milestone_type"],
                "milestone_value": m.get("milestone_value"),
                "milestone_text": m["milestone_text"],
                "source": m.get("source", ""),
                "achieved_at": datetime.now().isoformat(),
            }).execute()
            saved += 1
            logger.success(
                f"  🏆 MILESTONE: {m['milestone_text']} "
                f"(value: {m.get('milestone_value')})"
            )
        except Exception as e:
            logger.error(f"  Milestone save error: {e}")
    return saved


def run():
    start_time = datetime.now()
    logger.info(f"=== Starting {SCRAPER_NAME} ===")
    status = "failed"
    records_inserted = 0
    error_message = None

    try:
        db = get_supabase()

        result = db.table("artists").select(
            "id, name, slug, tier"
        ).eq("is_active", True).execute()

        artists = result.data
        logger.info(
            f"Checking milestones for {len(artists)} artists"
        )

        total_new_milestones = 0

        for artist in artists:
            all_milestones = []
            all_milestones.extend(
                check_metric_milestones(db, artist)
            )
            all_milestones.extend(
                check_chart_milestones(db, artist)
            )
            all_milestones.extend(
                check_press_milestones(db, artist)
            )

            if all_milestones:
                logger.info(
                    f"{artist['name']} (Tier {artist['tier']}): "
                    f"{len(all_milestones)} new milestone(s)"
                )
                saved = save_milestones(
                    db, artist["id"], all_milestones
                )
                total_new_milestones += saved

        records_inserted = total_new_milestones
        status = "success"
        logger.success(
            f"Complete: {total_new_milestones} new milestones recorded"
        )

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