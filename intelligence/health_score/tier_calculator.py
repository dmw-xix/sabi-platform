# intelligence/health_score/tier_calculator.py
# Automatically calculates and updates artist tiers weekly
# based on objective data across all tracked platforms.
#
# Scoring system (max 100 points):
#   Monthly listeners  → 0-20 pts
#   Followers          → 0-15 pts
#   YouTube subs       → 0-15 pts
#   Chart presence     → 0-25 pts
#   Global reach       → 0-15 pts
#   Momentum           → 0-10 pts
#
# Tier thresholds:
#   Tier 1 (Major)       = 65+ pts
#   Tier 2 (Established) = 35-64 pts
#   Tier 3 (Emerging)    = 12-34 pts
#   Tier 4 (Underground) = 0-11 pts
#
# Momentum override: 50%+ weekly growth = bump up one tier
#
# Run weekly on Sundays AFTER health scores are calculated.
# Usage: python intelligence/health_score/tier_calculator.py

import time
from datetime import date, datetime, timedelta
from loguru import logger
from database.client import get_supabase, log_scraper_run

SCRAPER_NAME = "tier_calculator"
TODAY = str(date.today())

TIER_LABELS = {
    1: "Major",
    2: "Established",
    3: "Emerging",
    4: "Underground",
}

# ─────────────────────────────────────────────────────────────
# SCORING TABLES
# Each table: list of (threshold, points) sorted descending
# ─────────────────────────────────────────────────────────────

LISTENER_TABLE = [
    (5_000_000, 20),
    (2_000_000, 18),
    (1_000_000, 16),
    (500_000,   14),
    (200_000,   11),
    (100_000,    8),
    (50_000,     5),
    (20_000,     3),
    (10_000,     1),
    (0,          0),
]

FOLLOWER_TABLE = [
    (3_000_000, 15),
    (1_000_000, 13),
    (500_000,   11),
    (200_000,    8),
    (100_000,    5),
    (50_000,     3),
    (10_000,     1),
    (0,          0),
]

YOUTUBE_TABLE = [
    (5_000_000, 15),
    (2_000_000, 13),
    (1_000_000, 11),
    (500_000,    8),
    (200_000,    5),
    (100_000,    3),
    (50_000,     1),
    (0,          0),
]

GLOBAL_TABLE = [
    (30, 15),
    (20, 12),
    (15, 10),
    (10,  7),
    (5,   4),
    (2,   2),
    (1,   1),
    (0,   0),
]

MOMENTUM_TABLE = [
    (100, 10),
    (50,   8),
    (30,   6),
    (20,   5),
    (10,   3),
    (5,    2),
    (0,    1),
    (-999, 0),
]

# Chart weights — how much each platform counts toward chart score
CHART_WEIGHTS = {
    "turntable_ng_top100": 3.0,
    "spotify_ng_daily":    2.5,
    "shazam_ng_top200":    2.0,
    "apple_music_ng":      2.0,
    "itunes_ng":           1.5,
    "spotify_ng_weekly":   1.5,
}

# Tier thresholds: (min_score, tier)
TIER_THRESHOLDS = [
    (65, 1),
    (35, 2),
    (12, 3),
    (0,  4),
]


# ─────────────────────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────────────────────

def lookup_score(value: float, table: list) -> int:
    """Return points for a value by scanning a threshold table."""
    for threshold, points in table:
        if value >= threshold:
            return points
    return 0


def get_latest_metric(
    db,
    artist_id: str,
    source: str,
    metric_name: str,
) -> float:
    """Fetch the most recent value for a single metric. Returns 0.0 on miss."""
    try:
        result = db.table("artist_snapshots").select(
            "metric_value"
        ).eq("artist_id", artist_id).eq(
            "source", source
        ).eq("metric_name", metric_name).order(
            "snapshot_date", desc=True
        ).limit(1).execute()

        if result.data and result.data[0]["metric_value"] is not None:
            return float(result.data[0]["metric_value"])
    except Exception as e:
        logger.debug(f"  metric fetch error ({source}/{metric_name}): {e}")
    return 0.0


# ─────────────────────────────────────────────────────────────
# SCORING FUNCTIONS
# ─────────────────────────────────────────────────────────────

def score_streaming(db, artist_id: str) -> tuple[int, int, dict]:
    """
    Returns (listener_pts, follower_pts, values_dict).
    Max combined: 35 pts.
    """
    listeners = get_latest_metric(
        db, artist_id, "audiomack", "monthly_listeners"
    )
    followers = get_latest_metric(
        db, artist_id, "audiomack", "followers"
    )

    listener_pts = lookup_score(listeners, LISTENER_TABLE)
    follower_pts = lookup_score(followers, FOLLOWER_TABLE)

    return listener_pts, follower_pts, {
        "monthly_listeners": int(listeners),
        "followers": int(followers),
    }


def score_youtube(db, artist_id: str) -> tuple[int, dict]:
    """
    Returns (youtube_pts, values_dict).
    Max: 15 pts.
    """
    subs = get_latest_metric(
        db, artist_id, "youtube", "youtube_subscribers"
    )
    pts = lookup_score(subs, YOUTUBE_TABLE)

    return pts, {"youtube_subscribers": int(subs)}


def score_global(db, artist_id: str) -> tuple[int, dict]:
    """
    Returns (global_pts, values_dict).
    Max: 15 pts.
    """
    countries = get_latest_metric(
        db, artist_id, "kworb_itunes", "kworb_countries_charting"
    )
    pts = lookup_score(countries, GLOBAL_TABLE)

    return pts, {"countries_charting": int(countries)}


def score_charts(db, artist_name: str) -> tuple[int, dict]:
    """
    Returns (chart_pts, breakdown_dict).
    Max: 25 pts.

    Uses exact match on raw_artist — avoids full table scan.
    """
    cutoff = str(date.today() - timedelta(days=14))

    try:
        result = db.table("chart_positions").select(
            "chart_name, position"
        ).eq("raw_artist", artist_name).gte(
            "chart_date", cutoff
        ).limit(300).execute()
    except Exception as e:
        logger.debug(f"  chart query error for {artist_name}: {e}")
        return 0, {}

    if not result.data:
        return 0, {}

    # Best position per platform
    platform_best: dict = {}
    for row in result.data:
        chart = row["chart_name"]
        pos = row["position"]
        if chart not in platform_best or pos < platform_best[chart]:
            platform_best[chart] = pos

    total = 0.0
    breakdown = {}

    for chart, best_pos in platform_best.items():
        weight = CHART_WEIGHTS.get(chart, 1.0)

        if best_pos <= 10:
            multiplier = 3.0
            label = "Top 10"
        elif best_pos <= 50:
            multiplier = 2.0
            label = "Top 50"
        elif best_pos <= 100:
            multiplier = 1.0
            label = "Top 100"
        else:
            multiplier = 0.5
            label = f"#{best_pos}"

        pts = weight * multiplier
        total += pts
        breakdown[chart] = {
            "best_position": best_pos,
            "label": label,
            "score": round(pts, 2),
        }

    # Multi-platform bonus
    n = len(platform_best)
    if n >= 4:
        total += 5
    elif n >= 3:
        total += 3
    elif n >= 2:
        total += 1

    return min(25, int(total)), breakdown


def score_momentum(db, artist_id: str) -> tuple[int, float]:
    """
    Returns (momentum_pts, growth_pct).
    Compares current week vs previous week monthly listeners.
    Max: 10 pts.
    """
    week_ago = str(date.today() - timedelta(days=7))
    two_weeks_ago = str(date.today() - timedelta(days=14))

    try:
        current = db.table("artist_snapshots").select(
            "metric_value"
        ).eq("artist_id", artist_id).eq(
            "source", "audiomack"
        ).eq("metric_name", "monthly_listeners").gte(
            "snapshot_date", week_ago
        ).order("snapshot_date", desc=True).limit(1).execute()

        previous = db.table("artist_snapshots").select(
            "metric_value"
        ).eq("artist_id", artist_id).eq(
            "source", "audiomack"
        ).eq("metric_name", "monthly_listeners").gte(
            "snapshot_date", two_weeks_ago
        ).lt(
            "snapshot_date", week_ago
        ).order("snapshot_date", desc=True).limit(1).execute()

        if not current.data or not previous.data:
            return 1, 0.0

        curr = float(current.data[0]["metric_value"] or 0)
        prev = float(previous.data[0]["metric_value"] or 0)

        if prev == 0:
            return 1, 0.0

        growth_pct = ((curr - prev) / prev) * 100
        pts = lookup_score(growth_pct, MOMENTUM_TABLE)
        return pts, round(growth_pct, 2)

    except Exception as e:
        logger.debug(f"  momentum error: {e}")
        return 1, 0.0


# ─────────────────────────────────────────────────────────────
# CORE CALCULATION
# ─────────────────────────────────────────────────────────────

def calculate_artist_tier(db, artist: dict) -> dict:
    """
    Runs all scoring functions for a single artist.
    Returns full result dict including score breakdown,
    calculated tier, and whether the tier changed.
    """
    artist_id   = artist["id"]
    artist_name = artist["name"]
    current_tier = artist.get("tier", 4)

    # Run each scorer
    listener_pts, follower_pts, streaming_vals = score_streaming(
        db, artist_id
    )
    youtube_pts, youtube_vals = score_youtube(db, artist_id)
    global_pts, global_vals   = score_global(db, artist_id)
    chart_pts, chart_breakdown = score_charts(db, artist_name)
    momentum_pts, growth_pct   = score_momentum(db, artist_id)

    total = (
        listener_pts
        + follower_pts
        + youtube_pts
        + chart_pts
        + global_pts
        + momentum_pts
    )

    # Determine base tier
    calculated_tier = 4
    for threshold, tier in TIER_THRESHOLDS:
        if total >= threshold:
            calculated_tier = tier
            break

    # Momentum override: 50%+ weekly growth → bump up one tier
    momentum_override = False
    if growth_pct >= 50 and calculated_tier > 1:
        calculated_tier -= 1
        momentum_override = True

    return {
        "artist_id":        artist_id,
        "artist_name":      artist_name,
        "current_tier":     current_tier,
        "calculated_tier":  calculated_tier,
        "tier_changed":     current_tier != calculated_tier,
        "momentum_override": momentum_override,
        "total_score":      total,
        "components": {
            "monthly_listeners": {
                "value":  streaming_vals["monthly_listeners"],
                "points": listener_pts,
                "max":    20,
            },
            "followers": {
                "value":  streaming_vals["followers"],
                "points": follower_pts,
                "max":    15,
            },
            "youtube_subscribers": {
                "value":  youtube_vals["youtube_subscribers"],
                "points": youtube_pts,
                "max":    15,
            },
            "chart_presence": {
                "points":    chart_pts,
                "max":       25,
                "breakdown": chart_breakdown,
            },
            "global_reach": {
                "value":  global_vals["countries_charting"],
                "points": global_pts,
                "max":    15,
            },
            "momentum": {
                "growth_pct": growth_pct,
                "points":     momentum_pts,
                "max":        10,
            },
        },
        "calculated_at": TODAY,
    }


# ─────────────────────────────────────────────────────────────
# DATABASE WRITE
# ─────────────────────────────────────────────────────────────

def apply_tier_update(db, result: dict) -> bool:
    """
    Updates artists.tier and logs the change as a milestone.
    Returns True if the update succeeded.
    """
    artist_id  = result["artist_id"]
    old_tier   = result["current_tier"]
    new_tier   = result["calculated_tier"]
    direction  = "up" if new_tier < old_tier else "down"

    try:
        db.table("artists").update(
            {"tier": new_tier}
        ).eq("id", artist_id).execute()

        override_note = (
            " [momentum override: 50%+ weekly growth]"
            if result.get("momentum_override") else ""
        )

        db.table("artist_milestones").insert({
            "artist_id":       artist_id,
            "milestone_type":  f"tier_{direction}grade",
            "milestone_value": float(new_tier),
            "milestone_text": (
                f"Tier {direction}grade: "
                f"Tier {old_tier} ({TIER_LABELS.get(old_tier, '')}) → "
                f"Tier {new_tier} ({TIER_LABELS.get(new_tier, '')})"
                f"{override_note}"
            ),
            "source":       "tier_calculator",
            "achieved_at":  datetime.now().isoformat(),
        }).execute()

        logger.success(
            f"  {'↑' if direction == 'up' else '↓'} "
            f"{result['artist_name']}: "
            f"Tier {old_tier} → Tier {new_tier}  "
            f"(score {result['total_score']}/100)"
            + (" [MOMENTUM OVERRIDE]" if result["momentum_override"] else "")
        )
        return True

    except Exception as e:
        logger.error(
            f"  Tier update failed for {result['artist_name']}: {e}"
        )
        return False


# ─────────────────────────────────────────────────────────────
# REPORTING
# ─────────────────────────────────────────────────────────────

def print_score_breakdown(result: dict):
    """Logs the full scoring breakdown for one artist at DEBUG level."""
    c = result["components"]
    logger.debug(
        f"  {result['artist_name']} breakdown: "
        f"listeners={c['monthly_listeners']['points']}/20  "
        f"followers={c['followers']['points']}/15  "
        f"youtube={c['youtube_subscribers']['points']}/15  "
        f"charts={c['chart_presence']['points']}/25  "
        f"global={c['global_reach']['points']}/15  "
        f"momentum={c['momentum']['points']}/10  "
        f"TOTAL={result['total_score']}/100"
    )


def print_summary(
    results: list[dict],
    tier_changes: list[dict],
    tier_distribution: dict,
):
    """Prints the end-of-run summary to the log."""

    # Tier distribution bar chart
    logger.info("")
    logger.info("TIER DISTRIBUTION (post-calculation)")
    logger.info("─" * 50)
    for tier in [1, 2, 3, 4]:
        count = tier_distribution.get(tier, 0)
        bar = "█" * count
        logger.info(
            f"  Tier {tier}  {TIER_LABELS[tier]:<12}  {bar}  {count}"
        )

    # Tier changes
    logger.info("")
    if tier_changes:
        logger.info(f"TIER CHANGES THIS WEEK ({len(tier_changes)})")
        logger.info("─" * 50)
        for r in sorted(
            tier_changes,
            key=lambda x: x["total_score"],
            reverse=True,
        ):
            arrow = "↑" if r["calculated_tier"] < r["current_tier"] else "↓"
            override = " [MOMENTUM]" if r["momentum_override"] else ""
            logger.info(
                f"  {arrow}  {r['artist_name']:<22} "
                f"Tier {r['current_tier']} → Tier {r['calculated_tier']}  "
                f"score={r['total_score']:>3}/100{override}"
            )
    else:
        logger.info("No tier changes this week — all assignments stable")

    # Top 15 by score
    logger.info("")
    logger.info("TOP 15 ARTISTS BY TIER SCORE")
    logger.info("─" * 50)
    top = sorted(results, key=lambda x: x["total_score"], reverse=True)[:15]
    for r in top:
        listeners = r["components"]["monthly_listeners"]["value"]
        listeners_fmt = (
            f"{listeners/1_000_000:.1f}M"
            if listeners >= 1_000_000
            else f"{listeners/1_000:.0f}K"
            if listeners >= 1_000
            else str(listeners)
        )
        logger.info(
            f"  {r['artist_name']:<22} "
            f"Tier {r['calculated_tier']}  "
            f"score={r['total_score']:>3}/100  "
            f"listeners={listeners_fmt:>7}"
        )

    # Watch list — artists within 5 points of a tier boundary
    logger.info("")
    logger.info("WATCH LIST (within 5 pts of tier boundary)")
    logger.info("─" * 50)
    watch_found = False
    for r in sorted(results, key=lambda x: x["total_score"], reverse=True):
        score = r["total_score"]
        for threshold, tier in TIER_THRESHOLDS:
            distance = score - threshold
            if 0 <= distance <= 5:
                logger.info(
                    f"  {r['artist_name']:<22} "
                    f"score={score:>3}  "
                    f"{distance} pts from Tier {tier}  "
                    f"(currently Tier {r['calculated_tier']})"
                )
                watch_found = True
    if not watch_found:
        logger.info("  No artists near tier boundaries")

    # Momentum override list
    overrides = [r for r in results if r.get("momentum_override")]
    if overrides:
        logger.info("")
        logger.info("MOMENTUM OVERRIDES (50%+ weekly growth)")
        logger.info("─" * 50)
        for r in overrides:
            g = r["components"]["momentum"]["growth_pct"]
            logger.info(
                f"  {r['artist_name']:<22} "
                f"+{g:.1f}% WoW  →  bumped to Tier {r['calculated_tier']}"
            )


# ─────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────

def run():
    start_time = datetime.now()
    logger.info(f"=== Starting {SCRAPER_NAME} ===")

    status          = "failed"
    records_inserted = 0
    error_message   = None
    artists         = []

    try:
        db = get_supabase()

        artists = db.table("artists").select(
            "id, name, slug, tier"
        ).eq("is_active", True).order("name").execute().data

        logger.info(f"Calculating tiers for {len(artists)} artists")
        logger.info("=" * 60)

        results: list[dict]      = []
        tier_changes: list[dict] = []
        tier_distribution        = {1: 0, 2: 0, 3: 0, 4: 0}

        for i, artist in enumerate(artists, 1):
            try:
                result = calculate_artist_tier(db, artist)
                results.append(result)

                new_tier = result["calculated_tier"]
                tier_distribution[new_tier] = (
                    tier_distribution.get(new_tier, 0) + 1
                )

                # Log compact line per artist
                change_str = ""
                if result["tier_changed"]:
                    arrow = (
                        "↑" if new_tier < result["current_tier"] else "↓"
                    )
                    change_str = (
                        f"  {arrow} Tier {result['current_tier']} → "
                        f"Tier {new_tier}"
                    )
                    tier_changes.append(result)
                    changed = apply_tier_update(db, result)
                    if changed:
                        records_inserted += 1
                else:
                    change_str = f"  Tier {new_tier} ✓"

                logger.info(
                    f"[{i:>3}/{len(artists)}] "
                    f"{artist['name']:<22} "
                    f"score={result['total_score']:>3}/100"
                    f"{change_str}"
                )

                # Print detailed breakdown at DEBUG level
                print_score_breakdown(result)

                # Throttle: pause every 15 artists to avoid
                # overwhelming Supabase connection
                if i % 15 == 0:
                    time.sleep(1.5)

            except Exception as e:
                logger.warning(
                    f"[{i:>3}/{len(artists)}] "
                    f"{artist['name']}: skipped — {e}"
                )
                # Keep current tier in distribution
                existing_tier = artist.get("tier", 4)
                tier_distribution[existing_tier] = (
                    tier_distribution.get(existing_tier, 0) + 1
                )
                continue

        # Print full summary
        logger.info("=" * 60)
        print_summary(results, tier_changes, tier_distribution)

        status = "success"
        logger.success(
            f"\nComplete: {len(artists)} artists processed  |  "
            f"{records_inserted} tier changes applied"
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
            records_attempted=len(artists),
            records_inserted=records_inserted,
            records_failed=0,
            error_message=error_message,
            duration_seconds=duration,
        )
        logger.info(f"Done. {status}. {duration:.1f}s")


if __name__ == "__main__":
    run()