# intelligence/health_score/calculator.py
# Calculates weekly Artist Health Score (0-100) for each artist
# Combines all data sources with weighted scoring
# Run weekly — ideally Sunday night for Monday morning reports

from datetime import date, datetime, timedelta
from loguru import logger
from database.client import get_supabase, log_scraper_run

SCRAPER_NAME = "artist_health_score"
TODAY = str(date.today())

# Scoring weights — must sum to 100
WEIGHTS_BY_TIER = {
    1: {  # Major artists — global reach and chart dominance
        "streaming": 25,
        "charts": 20,
        "social": 15,
        "momentum": 20,
        "global": 20,
    },
    2: {  # Mid-level — chart presence + growth
        "streaming": 30,
        "charts": 25,
        "social": 15,
        "momentum": 25,
        "global": 5,
    },
    3: {  # Emerging — momentum is everything
        "streaming": 25,
        "charts": 20,
        "social": 10,
        "momentum": 40,
        "global": 5,
    },
    4: {  # Underground — week-over-week velocity only signal
        "streaming": 20,
        "charts": 15,
        "social": 5,
        "momentum": 55,
        "global": 5,
    },
}



def get_streaming_score(db, artist_id: str, artist_name: str) -> float:
    """Score based on Audiomack monthly listeners and Spotify streams."""
    score = 0

    try:
        # Audiomack monthly listeners
        result = db.table("artist_snapshots").select(
            "metric_value"
        ).eq("artist_id", artist_id).eq(
            "source", "audiomack"
        ).eq("metric_name", "monthly_listeners").order(
            "snapshot_date", desc=True
        ).limit(1).execute()

        if result.data:
            listeners = result.data[0]["metric_value"]
            # Scale: 5M listeners = 100 points
            score += min(50, (listeners / 5_000_000) * 50)

        # Spotify daily streams (from Kworb)
        result = db.table("chart_positions").select(
            "daily_streams, streams_7day"
        ).eq("raw_artist", artist_name).eq(
            "chart_name", "spotify_ng_daily"
        ).order("chart_date", desc=True).limit(5).execute()

        if result.data:
            avg_streams = sum(
                r["daily_streams"] for r in result.data
                if r.get("daily_streams")
            ) / max(1, len(result.data))
            # Scale: 500k daily streams = 50 points
            score += min(50, (avg_streams / 500_000) * 50)

    except Exception as e:
        logger.debug(f"Streaming score error for {artist_name}: {e}")

    return min(100, score)


def get_chart_score(db, artist_id: str, artist_name: str) -> float:
    """Score based on chart positions across all platforms."""
    score = 0
    week_ago = str(date.today() - timedelta(days=7))

    try:
        result = db.table("chart_positions").select(
            "chart_name, position, chart_date"
        ).eq("raw_artist", artist_name).gte(
            "chart_date", week_ago
        ).execute()

        for entry in result.data:
            pos = entry["position"]
            chart = entry["chart_name"]

            # Weight different charts differently
            chart_weight = {
                "turntable_ng_top100": 3.0,  # Most authoritative
                "spotify_ng_daily": 2.0,
                "shazam_ng_top200": 1.5,
                "apple_music_ng": 1.5,
                "itunes_ng": 1.0,
                "spotify_ng_weekly": 1.0,
            }.get(chart, 1.0)

            # Position score: #1 = 100pts, #50 = 50pts, #200 = 5pts
            position_score = max(0, 105 - pos) * chart_weight
            score += position_score

        # Normalize to 0-100
        score = min(100, score / 50)

    except Exception as e:
        logger.debug(f"Chart score error for {artist_name}: {e}")

    return score


def get_social_score(db, artist_id: str) -> float:
    """Score based on YouTube metrics and press mentions."""
    score = 0

    try:
        # YouTube subscribers
        result = db.table("artist_snapshots").select(
            "metric_value"
        ).eq("artist_id", artist_id).eq(
            "source", "youtube"
        ).eq("metric_name", "youtube_subscribers").order(
            "snapshot_date", desc=True
        ).limit(1).execute()

        if result.data:
            subs = result.data[0]["metric_value"]
            score += min(40, (subs / 6_000_000) * 40)

        # Press mentions in last 7 days
        result = db.table("press_articles").select(
            "id, sentiment_label"
        ).contains("artist_ids", [artist_id]).gte(
            "published_at",
            (datetime.now() - timedelta(days=7)).isoformat()
        ).execute()

        article_count = len(result.data)
        positive_count = sum(
            1 for a in result.data
            if a.get("sentiment_label") == "positive"
        )

        # Up to 30 points for press
        press_score = min(30, article_count * 5)
        # Sentiment bonus/penalty
        if article_count > 0:
            sentiment_ratio = positive_count / article_count
            press_score *= (0.5 + sentiment_ratio * 0.5)

        score += press_score

    except Exception as e:
        logger.debug(f"Social score error: {e}")

    return min(100, score)


def get_momentum_score(db, artist_id: str, artist_name: str) -> float:
    """Score based on week-over-week growth across metrics."""
    score = 50  # Neutral starting point

    try:
        today = date.today()
        week_ago = today - timedelta(days=7)
        two_weeks_ago = today - timedelta(days=14)

        # Compare Audiomack monthly listeners: this week vs last week
        current = db.table("artist_snapshots").select(
            "metric_value"
        ).eq("artist_id", artist_id).eq(
            "source", "audiomack"
        ).eq("metric_name", "monthly_listeners").gte(
            "snapshot_date", str(week_ago)
        ).order("snapshot_date", desc=True).limit(1).execute()

        previous = db.table("artist_snapshots").select(
            "metric_value"
        ).eq("artist_id", artist_id).eq(
            "source", "audiomack"
        ).eq("metric_name", "monthly_listeners").gte(
            "snapshot_date", str(two_weeks_ago)
        ).lt("snapshot_date", str(week_ago)).order(
            "snapshot_date", desc=True
        ).limit(1).execute()

        if current.data and previous.data:
            curr_val = current.data[0]["metric_value"]
            prev_val = previous.data[0]["metric_value"]

            if prev_val and prev_val > 0:
                growth = (curr_val - prev_val) / prev_val
                # +20% growth = +25 points, -20% = -25 points
                score += min(50, max(-50, growth * 125))

        # Compare Spotify stream velocity (streams_change from Kworb)
        stream_changes = db.table("chart_positions").select(
            "streams_change"
        ).eq("raw_artist", artist_name).eq(
            "chart_name", "spotify_ng_daily"
        ).gte("chart_date", str(week_ago)).execute()

        if stream_changes.data:
            avg_change = sum(
                r["streams_change"] for r in stream_changes.data
                if r.get("streams_change")
            ) / max(1, len(stream_changes.data))

            if avg_change > 0:
                score += min(25, avg_change / 10000)
            else:
                score += max(-25, avg_change / 10000)

    except Exception as e:
        logger.debug(f"Momentum score error for {artist_name}: {e}")

    return min(100, max(0, score))


def get_global_score(db, artist_id: str) -> float:
    """Score based on number of countries charting globally."""
    try:
        result = db.table("artist_snapshots").select(
            "metric_value"
        ).eq("artist_id", artist_id).eq(
            "source", "kworb_itunes"
        ).eq("metric_name", "kworb_countries_charting").order(
            "snapshot_date", desc=True
        ).limit(1).execute()

        if result.data:
            countries = result.data[0]["metric_value"]
            # 20 countries = 100 points
            return min(100, (countries / 20) * 100)

    except Exception as e:
        logger.debug(f"Global score error: {e}")

    return 0

# In intelligence/health_score/calculator.py
def calculate_velocity(
    current_value: float,
    previous_value: float,
    days_between: int = 7
    ) -> float:
    """
    Calculates normalized velocity score.
    Returns daily percentage growth rate.
    """
    if not previous_value or previous_value == 0:
        return 0.0
    total_growth = (current_value - previous_value) / previous_value
    daily_rate = total_growth / days_between
    return round(daily_rate * 100, 4)  # As percentage per day

import numpy as np
def pearsonr(x, y):
    x, y = np.array(x), np.array(y)
    return np.corrcoef(x, y)[0, 1]

def cross_platform_consistency(
    db, artist_name: str, days: int = 14
) -> float:
    """
    Measures how consistently an artist performs across platforms.
    High score = reliable commercial performer (good for brands).
    Low score = platform-specific appeal (niche or promotional gaming).
    Returns Pearson correlation coefficient 0-1.
    """
    platforms = [
        "spotify_ng_daily",
        "shazam_ng_top200",
        "apple_music_ng",
        "turntable_ng_top100",
    ]

    platform_positions = {}
    cutoff = str(date.today() - timedelta(days=days))

    for platform in platforms:
        result = db.table("chart_positions").select(
            "position, chart_date"
        ).eq("raw_artist", artist_name).eq(
            "chart_name", platform
        ).gte("chart_date", cutoff).execute()

        if result.data:
            # Average position on this platform
            avg_pos = sum(r["position"] for r in result.data) / len(result.data)
            platform_positions[platform] = avg_pos

    if len(platform_positions) < 2:
        return 0.0

    # Consistency score: lower variance = higher score
    positions = list(platform_positions.values())
    # Normalize positions (lower is better, so invert)
    normalized = [1 / p for p in positions]
    
    if len(normalized) > 1:
        variance = np.var(normalized)
        consistency = 1 / (1 + variance * 10)
        return round(float(consistency), 3)
    return 0.0

def leading_indicator_score(db, artist_id: str, artist_name: str) -> float:
    """
    Scores an artist on leading indicators that precede mainstream breakout.
    Based on the observed Nigerian music breakout sequence:
    Google Trends spike → Shazam entry → Spotify surge → TurnTable → Press
    
    Higher score = closer to breakout.
    """
    score = 0.0
    
    # Signal 1: Google Trends momentum (leads by ~7-14 days)
    trends = get_latest_metric(db, artist_id, "google_trends", "google_trends_momentum")
    if trends and trends > 20:  # >20% week-over-week trends growth
        score += 25
    elif trends and trends > 5:
        score += 10
    
    # Signal 2: Shazam entry / position improvement (leads by ~5-10 days)
    recent_shazam = db.table("chart_positions").select(
        "position"
    ).eq("raw_artist", artist_name).eq(
        "chart_name", "shazam_ng_top200"
    ).gte("chart_date", str(date.today() - timedelta(days=7))).execute()
    
    if recent_shazam.data:
        best_shazam = min(r["position"] for r in recent_shazam.data)
        if best_shazam <= 50:
            score += 25
        elif best_shazam <= 100:
            score += 15
        else:
            score += 5
    
    # Signal 3: Audiomack velocity (concurrent with breakout)
    am_listeners = get_latest_metric(
        db, artist_id, "audiomack", "monthly_listeners"
    )
    am_momentum = get_momentum_score(db, artist_id, artist_name)
    if am_momentum > 70:
        score += 25
    elif am_momentum > 55:
        score += 15
    
    # Signal 4: Spotify stream acceleration (streams_change positive)
    stream_changes = db.table("chart_positions").select(
        "streams_change"
    ).eq("raw_artist", artist_name).eq(
        "chart_name", "spotify_ng_daily"
    ).gte("chart_date", str(date.today() - timedelta(days=3))).execute()
    
    if stream_changes.data:
        avg_change = sum(
            r["streams_change"] for r in stream_changes.data 
            if r.get("streams_change") and r["streams_change"] > 0
        ) / max(1, len(stream_changes.data))
        if avg_change > 50000:
            score += 25
        elif avg_change > 10000:
            score += 15
    
    return min(100, score)

def authenticity_score(db, artist_id: str) -> float:
    """
    Detects suspicious metric ratios that suggest artificial inflation.
    Returns 0-100 where 100 = highly authentic.
    """
    followers = get_latest_metric(
        db, artist_id, "audiomack", "followers"
    )
    monthly = get_latest_metric(
        db, artist_id, "audiomack", "monthly_listeners"
    )
    total_plays = get_latest_metric(
        db, artist_id, "audiomack", "total_plays"
    )

    if not followers or followers == 0:
        return 50.0  # Unknown

    score = 100.0

    # Check 1: Monthly listeners should be 3-30% of followers for healthy account
    if monthly:
        listener_ratio = monthly / followers
        if listener_ratio < 0.01:  # Less than 1% — very suspicious
            score -= 40
        elif listener_ratio < 0.03:
            score -= 20
        elif listener_ratio > 0.5:  # More listeners than followers — unusual
            score -= 10

    # Check 2: Total plays should scale with followers
    if total_plays:
        plays_per_follower = total_plays / followers
        if plays_per_follower < 5:  # Very low engagement on uploads
            score -= 20
        elif plays_per_follower > 1000:  # Unusually high — check
            score -= 10

    return max(0, min(100, score))




def calculate_health_score(db, artist: dict) -> dict | None:
    artist_id = artist["id"]
    artist_name = artist["name"]
    tier = artist.get("tier", 2)
    weights = WEIGHTS_BY_TIER.get(tier, WEIGHTS_BY_TIER[2])

    try:
        streaming = get_streaming_score(db, artist_id, artist_name)
        charts = get_chart_score(db, artist_id, artist_name)
        social = get_social_score(db, artist_id)
        momentum = get_momentum_score(db, artist_id, artist_name)
        global_reach = get_global_score(db, artist_id)

        composite = (
            streaming * weights["streaming"] / 100 +
            charts * weights["charts"] / 100 +
            social * weights["social"] / 100 +
            momentum * weights["momentum"] / 100 +
            global_reach * weights["global"] / 100
        )

        return {
            "artist_id": artist_id,
            "score": round(composite, 2),
            "streaming_score": round(streaming, 2),
            "social_score": round(social, 2),
            "radio_score": round(charts, 2),
            "momentum_score": round(momentum, 2),
            "youtube_score": round(global_reach, 2),
            "component_data": {
                "streaming": streaming,
                "charts": charts,
                "social": social,
                "momentum": momentum,
                "global_reach": global_reach,
                "weights_used": weights,
                "tier": tier,
            },
            "score_date": str(date.today()),
        }
    except Exception as e:
        logger.error(f"Score calculation failed for {artist_name}: {e}")
        return None


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
        ).eq("is_active", True).order("tier").execute()

        artists = result.data
        logger.info(f"Calculating scores for {len(artists)} artists")

        scores = []
        for artist in artists:
            score_data = calculate_health_score(db, artist)
            if score_data:
                scores.append((artist["name"], score_data))

        # Sort by score descending for display
        scores.sort(key=lambda x: x[1]["score"], reverse=True)

        logger.info("--- Artist Health Score Rankings ---")
        for name, data in scores:
            logger.info(
                f"  {name}: {data['score']:.1f} | "
                f"streaming={data['streaming_score']:.0f} "
                f"charts={data['radio_score']:.0f} "
                f"social={data['social_score']:.0f} "
                f"momentum={data['momentum_score']:.0f} "
                f"global={data['youtube_score']:.0f}"
            )

        # Save to database
        for name, score_data in scores:
            try:
                db.table("artist_health_scores").upsert(
                    score_data,
                    on_conflict="artist_id,score_date"
                ).execute()
                records_inserted += 1
            except Exception as e:
                logger.error(f"  DB error saving score for {name}: {e}")

        status = "success"
        logger.success(
            f"Scores calculated and saved for {records_inserted} artists"
        )

    except Exception as e:
        error_message = str(e)
        logger.error(f"Crashed: {e}")

    finally:
        duration = (datetime.now() - start_time).total_seconds()
        log_scraper_run(
            scraper_name=SCRAPER_NAME,
            status=status,
            records_attempted=len(artists) if 'artists' in locals() else 0,
            records_inserted=records_inserted,
            records_failed=0,
            error_message=error_message,
            duration_seconds=duration
        )
        logger.info(f"Done. {status}. {duration:.1f}s")


if __name__ == "__main__":
    run()