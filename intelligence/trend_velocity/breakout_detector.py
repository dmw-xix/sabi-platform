# intelligence/trend_velocity/breakout_detector.py
# Cross-source breakout detection engine
# Flags artists showing simultaneous positive signals
# across 3+ data sources within a rolling 72-hour window
#
# Signal types detected:
# - streaming_surge: Spotify daily streams up significantly
# - chart_climb: jumped 10+ positions on any chart
# - new_chart_entry: appeared on chart for first time
# - press_surge: multiple press mentions in 24 hours
# - audiomack_surge: Audiomack play count growing fast
# - youtube_surge: recent video performing above artist average
# - global_crossover: charting in new countries on Kworb

from datetime import datetime, timedelta, date
from loguru import logger
from database.client import get_supabase, log_scraper_run

SCRAPER_NAME = "breakout_detector"

# Minimum sources required to flag a breakout
MIN_SOURCES_FOR_BREAKOUT = 2

# Thresholds
CHART_CLIMB_THRESHOLD = 5       # positions gained to count as a surge
STREAM_GROWTH_THRESHOLD = 0.20  # 20% day-over-day stream growth
PRESS_MENTIONS_THRESHOLD = 2    # articles in 48 hours to flag press surge
YOUTUBE_VIEWS_THRESHOLD = 500000  # views on a recent video


def detect_chart_signals(db, artist_id: str, artist_name: str) -> list[dict]:
    """
    Detects chart-based signals:
    - New chart entry (first appearance)
    - Significant position climb
    - Multiple chart appearances simultaneously
    """
    signals = []
    today = date.today()
    yesterday = today - timedelta(days=1)
    two_days_ago = today - timedelta(days=2)

    try:
        # Get recent chart positions for this artist
        result = db.table("chart_positions").select(
            "chart_name, position, chart_date, raw_title"
        ).eq("raw_artist", artist_name).gte(
            "chart_date", str(two_days_ago)
        ).order("chart_date", desc=True).execute()

        positions = result.data
        if not positions:
            return []

        # Group by chart and track
        chart_track_history = {}
        for p in positions:
            key = f"{p['chart_name']}::{p['raw_title']}"
            if key not in chart_track_history:
                chart_track_history[key] = []
            chart_track_history[key].append(p)

        for key, history in chart_track_history.items():
            chart_name = history[0]["chart_name"]
            title = history[0]["raw_title"]

            history_sorted = sorted(history, key=lambda x: x["chart_date"])

            if len(history_sorted) >= 2:
                latest = history_sorted[-1]
                previous = history_sorted[-2]

                climb = previous["position"] - latest["position"]

                if climb >= CHART_CLIMB_THRESHOLD:
                    signals.append({
                        "signal_type": "chart_climb",
                        "source": chart_name,
                        "strength": min(100, climb * 5),
                        "detail": (
                            f"'{title}' climbed {climb} positions "
                            f"to #{latest['position']} on {chart_name}"
                        ),
                        "data": {
                            "chart": chart_name,
                            "title": title,
                            "from_position": previous["position"],
                            "to_position": latest["position"],
                            "climb": climb,
                        }
                    })

            elif len(history_sorted) == 1:
                # First appearance — new entry
                entry = history_sorted[0]
                if str(entry["chart_date"]) in [str(today), str(yesterday)]:
                    signals.append({
                        "signal_type": "new_chart_entry",
                        "source": chart_name,
                        "strength": max(20, 100 - entry["position"]),
                        "detail": (
                            f"'{title}' entered {chart_name} "
                            f"at #{entry['position']}"
                        ),
                        "data": {
                            "chart": chart_name,
                            "title": title,
                            "position": entry["position"],
                        }
                    })

    except Exception as e:
        logger.debug(f"  Chart signal error for {artist_name}: {e}")

    return signals


def detect_spotify_stream_surge(
    db, artist_id: str, artist_name: str
) -> list[dict]:
    """
    Detects Spotify streaming surges using Kworb data.
    Looks for tracks where streams_change is positive and significant.
    """
    signals = []

    try:
        today = date.today()
        yesterday = today - timedelta(days=1)

        result = db.table("chart_positions").select(
            "raw_title, position, daily_streams, streams_change, "
            "streams_7day, chart_date"
        ).eq("raw_artist", artist_name).eq(
            "chart_name", "spotify_ng_daily"
        ).gte("chart_date", str(yesterday)).execute()

        for row in result.data:
            if not row.get("streams_change") or not row.get("daily_streams"):
                continue

            change = row["streams_change"]
            current = row["daily_streams"]

            if current > 0 and change > 0:
                growth_pct = change / (current - change) if current > change else 0

                if growth_pct >= STREAM_GROWTH_THRESHOLD:
                    signals.append({
                        "signal_type": "streaming_surge",
                        "source": "spotify_ng_daily",
                        "strength": min(100, int(growth_pct * 100)),
                        "detail": (
                            f"'{row['raw_title']}' streams up "
                            f"{growth_pct:.0%} to {current:,.0f}/day"
                        ),
                        "data": {
                            "title": row["raw_title"],
                            "daily_streams": current,
                            "streams_change": change,
                            "growth_pct": round(growth_pct, 3),
                            "position": row["position"],
                        }
                    })

    except Exception as e:
        logger.debug(f"  Stream surge error for {artist_name}: {e}")

    return signals


def detect_press_surge(
    db, artist_id: str
) -> list[dict]:
    """
    Detects press mention surges.
    Flags when an artist gets multiple articles in 48 hours.
    """
    signals = []

    try:
        cutoff = datetime.now() - timedelta(hours=48)

        result = db.table("press_articles").select(
            "id, headline, publication, sentiment_label, published_at"
        ).contains("artist_ids", [artist_id]).gte(
            "published_at", cutoff.isoformat()
        ).execute()

        articles = result.data

        if len(articles) >= PRESS_MENTIONS_THRESHOLD:
            positive = sum(
                1 for a in articles
                if a.get("sentiment_label") == "positive"
            )
            signals.append({
                "signal_type": "press_surge",
                "source": "press",
                "strength": min(100, len(articles) * 20),
                "detail": (
                    f"{len(articles)} press articles in 48 hours "
                    f"({positive} positive)"
                ),
                "data": {
                    "article_count": len(articles),
                    "positive_count": positive,
                    "publications": list({
                        a["publication"] for a in articles
                    }),
                    "headlines": [
                        a["headline"] for a in articles[:3]
                    ],
                }
            })

    except Exception as e:
        logger.debug(f"  Press surge error: {e}")

    return signals


def detect_youtube_surge(
    db, artist_id: str
) -> list[dict]:
    """
    Detects YouTube view surges on recent videos.
    """
    signals = []

    try:
        two_days_ago = date.today() - timedelta(days=2)

        # Get recent YouTube video snapshots
        result = db.table("track_snapshots").select(
            "track_id, metric_value, snapshot_date"
        ).eq("source", "youtube").eq(
            "metric_name", "views"
        ).gte("snapshot_date", str(two_days_ago)).execute()

        for snap in result.data:
            if snap["metric_value"] and snap["metric_value"] >= YOUTUBE_VIEWS_THRESHOLD:
                # Get track title
                track_result = db.table("tracks").select(
                    "title"
                ).eq("id", snap["track_id"]).eq(
                    "artist_id", artist_id
                ).execute()

                if track_result.data:
                    title = track_result.data[0]["title"]
                    signals.append({
                        "signal_type": "youtube_surge",
                        "source": "youtube",
                        "strength": min(
                            100,
                            int(snap["metric_value"] / 100000)
                        ),
                        "detail": (
                            f"'{title}' has "
                            f"{snap['metric_value']:,.0f} YouTube views"
                        ),
                        "data": {
                            "title": title,
                            "views": snap["metric_value"],
                        }
                    })

    except Exception as e:
        logger.debug(f"  YouTube surge error: {e}")

    return signals


def detect_global_crossover(
    db, artist_id: str, artist_name: str
) -> list[dict]:
    """
    Detects global crossover signals from Kworb data.
    Flags when an artist is charting in many countries.
    """
    signals = []

    try:
        result = db.table("artist_snapshots").select(
            "metric_value, snapshot_date"
        ).eq("artist_id", artist_id).eq(
            "source", "kworb_itunes"
        ).eq(
            "metric_name", "kworb_countries_charting"
        ).order("snapshot_date", desc=True).limit(1).execute()

        if result.data:
            countries_count = result.data[0]["metric_value"]
            if countries_count and countries_count >= 5:
                signals.append({
                    "signal_type": "global_crossover",
                    "source": "kworb_itunes",
                    "strength": min(100, int(countries_count * 5)),
                    "detail": (
                        f"Charting in {int(countries_count)} "
                        "countries globally"
                    ),
                    "data": {
                        "countries_count": countries_count,
                    }
                })

    except Exception as e:
        logger.debug(f"  Global crossover error: {e}")

    return signals


def save_breakout_signal(
    db,
    artist_id: str,
    signals: list[dict]
) -> bool:
    """
    Saves a breakout signal to the breakout_signals table.
    """
    try:
        sources = list({s["source"] for s in signals})
        signal_types = list({s["signal_type"] for s in signals})
        avg_strength = sum(s["strength"] for s in signals) / len(signals)

        # Build combined signal data
        signal_data = {}
        for s in signals:
            signal_data[s["signal_type"]] = s["data"]

        # Summary for human review
        summary = " | ".join(s["detail"] for s in signals[:3])

        db.table("breakout_signals").insert({
            "artist_id": artist_id,
            "signal_type": "cross_platform_breakout",
            "strength": round(avg_strength, 2),
            "sources": sources,
            "signal_data": {
                "signal_types": signal_types,
                "signals": signal_data,
                "summary": summary,
            },
            "detected_at": datetime.now().isoformat(),
            "status": "active",
        }).execute()

        return True

    except Exception as e:
        logger.error(f"  Failed to save breakout signal: {e}")
        return False


def run():
    start_time = datetime.now()
    logger.info(f"=== Starting {SCRAPER_NAME} ===")

    status = "failed"
    breakouts_detected = 0
    error_message = None

    try:
        db = get_supabase()

        # Load all active artists
        result = db.table("artists").select(
            "id, name, slug, tier"
        ).eq("is_active", True).order("tier").execute()

        artists = result.data
        logger.info(f"Checking {len(artists)} artists for breakouts...")

        for artist in artists:
            artist_id = artist["id"]
            artist_name = artist["name"]
            artist_slug = artist["slug"]

            all_signals = []

            # Run all signal detectors
            all_signals.extend(
                detect_chart_signals(db, artist_id, artist_name)
            )
            all_signals.extend(
                detect_spotify_stream_surge(db, artist_id, artist_name)
            )
            all_signals.extend(
                detect_press_surge(db, artist_id)
            )
            all_signals.extend(
                detect_youtube_surge(db, artist_id)
            )
            all_signals.extend(
                detect_global_crossover(db, artist_id, artist_name)
            )

            if not all_signals:
                logger.debug(f"  {artist_name}: no signals")
                continue

            # Count unique sources
            unique_sources = len({s["source"] for s in all_signals})

            logger.info(
                f"  {artist_name}: {len(all_signals)} signals "
                f"across {unique_sources} sources"
            )

            for s in all_signals:
                logger.debug(f"    [{s['signal_type']}] {s['detail']}")

            # Breakout = signals from multiple sources
            if unique_sources >= MIN_SOURCES_FOR_BREAKOUT:
                avg_strength = sum(
                    s["strength"] for s in all_signals
                ) / len(all_signals)

                logger.success(
                    f"  🚀 BREAKOUT DETECTED: {artist_name} | "
                    f"sources={unique_sources} | "
                    f"strength={avg_strength:.0f}"
                )

                if save_breakout_signal(db, artist_id, all_signals):
                    breakouts_detected += 1

        status = "success"
        logger.success(
            f"Detection complete. {breakouts_detected} breakouts flagged."
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
            records_attempted=len(artists) if 'artists' in locals() else 0,
            records_inserted=breakouts_detected,
            records_failed=0,
            error_message=error_message,
            duration_seconds=duration
        )
        logger.info(f"Done. {status}. {duration:.1f}s")


if __name__ == "__main__":
    run()