# intelligence/predictions/shazam_peak_predictor.py
# Predicts when an artist will peak on Shazam Nigeria
# using Google Trends momentum + Audiomack play velocity
# Method: Ordinary Least Squares linear regression
# on historical data to project forward
#
# The observed Nigerian music breakout sequence:
#   Google Trends spike → Shazam entry → Spotify surge
#   Typically: Trends leads Shazam by 5-14 days
#              Audiomack velocity leads Shazam by 3-10 days
#
# Run after google_trends_scraper and audiomack_scraper

import numpy as np
from datetime import date, datetime, timedelta
from loguru import logger
from database.client import get_supabase, log_scraper_run

SCRAPER_NAME = "shazam_peak_predictor"
TODAY = date.today()


def get_trends_history(
    db, artist_id: str, days: int = 30
) -> list[tuple[date, float]]:
    """
    Returns (date, google_trends_avg) pairs for an artist.
    """
    cutoff = str(TODAY - timedelta(days=days))
    result = db.table("artist_snapshots").select(
        "metric_value, snapshot_date"
    ).eq("artist_id", artist_id).eq(
        "source", "google_trends"
    ).eq("metric_name", "google_trends_avg").gte(
        "snapshot_date", cutoff
    ).order("snapshot_date").execute()

    return [
        (
            date.fromisoformat(r["snapshot_date"]),
            float(r["metric_value"] or 0)
        )
        for r in result.data
    ]


def get_audiomack_velocity_history(
    db, artist_id: str, days: int = 30
) -> list[tuple[date, float]]:
    """
    Returns (date, monthly_listeners) pairs.
    Velocity is calculated as day-over-day % change.
    """
    cutoff = str(TODAY - timedelta(days=days))
    result = db.table("artist_snapshots").select(
        "metric_value, snapshot_date"
    ).eq("artist_id", artist_id).eq(
        "source", "audiomack"
    ).eq("metric_name", "monthly_listeners").gte(
        "snapshot_date", cutoff
    ).order("snapshot_date").execute()

    raw = [
        (
            date.fromisoformat(r["snapshot_date"]),
            float(r["metric_value"] or 0)
        )
        for r in result.data
    ]

    # Calculate velocity (% change between consecutive days)
    velocity = []
    for i in range(1, len(raw)):
        prev_val = raw[i-1][1]
        curr_val = raw[i][1]
        if prev_val > 0:
            pct_change = ((curr_val - prev_val) / prev_val) * 100
        else:
            pct_change = 0.0
        velocity.append((raw[i][0], pct_change))

    return velocity


def get_shazam_history(
    db, artist_name: str, days: int = 30
) -> list[tuple[date, int]]:
    """
    Returns (date, best_shazam_position) pairs.
    Lower position = better (inverted for regression).
    """
    cutoff = str(TODAY - timedelta(days=days))
    result = db.table("chart_positions").select(
        "position, chart_date"
    ).eq("raw_artist", artist_name).eq(
        "chart_name", "shazam_ng_top200"
    ).gte("chart_date", cutoff).order("chart_date").execute()

    # Group by date, take best position per day
    by_date: dict = {}
    for r in result.data:
        d = date.fromisoformat(r["chart_date"])
        pos = r["position"]
        if d not in by_date or pos < by_date[d]:
            by_date[d] = pos

    return sorted(by_date.items())


def align_series(
    series_a: list[tuple],
    series_b: list[tuple],
    lag_days: int = 0
) -> tuple[np.ndarray, np.ndarray]:
    """
    Aligns two time series by date, applying a lag to series_a.
    Returns (X, y) arrays for regression.
    lag_days: how many days ahead series_a leads series_b.
    """
    # Shift series_a forward by lag_days
    a_shifted = {
        d + timedelta(days=lag_days): v
        for d, v in series_a
    }

    # Find common dates
    common_dates = sorted(
        set(a_shifted.keys()) & {d for d, _ in series_b}
    )

    if len(common_dates) < 3:
        return np.array([]), np.array([])

    b_dict = {d: v for d, v in series_b}

    X = np.array([a_shifted[d] for d in common_dates])
    y = np.array([b_dict[d] for d in common_dates])

    return X, y


def linear_regression(X: np.ndarray, y: np.ndarray) -> tuple:
    """
    Simple OLS linear regression: y = a + b*X
    Returns (slope, intercept, r_squared)
    """
    if len(X) < 3:
        return 0.0, 0.0, 0.0

    n = len(X)
    x_mean = X.mean()
    y_mean = y.mean()

    # Slope (b)
    numerator = np.sum((X - x_mean) * (y - y_mean))
    denominator = np.sum((X - x_mean) ** 2)

    if denominator == 0:
        return 0.0, y_mean, 0.0

    slope = numerator / denominator
    intercept = y_mean - slope * x_mean

    # R-squared
    y_pred = slope * X + intercept
    ss_res = np.sum((y - y_pred) ** 2)
    ss_tot = np.sum((y - y_mean) ** 2)
    r_squared = 1 - (ss_res / ss_tot) if ss_tot > 0 else 0.0

    return float(slope), float(intercept), float(r_squared)


def predict_shazam_peak(
    artist_name: str,
    artist_id: str,
    trends_history: list,
    velocity_history: list,
    shazam_history: list,
) -> dict | None:
    """
    Predicts when an artist will peak on Shazam and at what position.

    Uses two regression models:
    1. Google Trends → Shazam position (lag: 7-14 days)
    2. Audiomack velocity → Shazam position (lag: 3-7 days)

    Returns prediction dict with peak date, position, and confidence.
    """
    predictions = []

    # Convert Shazam positions: invert so higher = better
    # Position 1 = score 200, position 200 = score 1
    shazam_scores = [
        (d, 201 - pos) for d, pos in shazam_history
    ]

    if not shazam_scores:
        logger.debug(f"  {artist_name}: no Shazam history — cannot predict")
        return None

    # Model 1: Google Trends predicts Shazam (7-day lag)
    for lag in [7, 10, 14]:
        X, y = align_series(trends_history, shazam_scores, lag)
        if len(X) >= 3:
            slope, intercept, r2 = linear_regression(X, y)
            if r2 > 0.3:  # Meaningful correlation
                # Get current trends value to predict future Shazam
                if trends_history:
                    current_trends = trends_history[-1][1]
                    predicted_score = slope * current_trends + intercept
                    predicted_position = max(
                        1, min(200, int(201 - predicted_score))
                    )
                    predicted_date = TODAY + timedelta(days=lag)

                    predictions.append({
                        "model": f"google_trends_lag{lag}d",
                        "predicted_date": str(predicted_date),
                        "predicted_position": predicted_position,
                        "confidence": round(r2, 3),
                        "current_input": current_trends,
                        "slope": round(slope, 4),
                        "r_squared": round(r2, 3),
                    })
                    logger.debug(
                        f"  Model trends_lag{lag}d: "
                        f"pos={predicted_position} on {predicted_date} "
                        f"(R²={r2:.3f})"
                    )

    # Model 2: Audiomack velocity predicts Shazam (5-day lag)
    for lag in [3, 5, 7]:
        X, y = align_series(velocity_history, shazam_scores, lag)
        if len(X) >= 3:
            slope, intercept, r2 = linear_regression(X, y)
            if r2 > 0.2:
                if velocity_history:
                    current_velocity = velocity_history[-1][1]
                    predicted_score = slope * current_velocity + intercept
                    predicted_position = max(
                        1, min(200, int(201 - predicted_score))
                    )
                    predicted_date = TODAY + timedelta(days=lag)

                    predictions.append({
                        "model": f"audiomack_velocity_lag{lag}d",
                        "predicted_date": str(predicted_date),
                        "predicted_position": predicted_position,
                        "confidence": round(r2, 3),
                        "current_input": round(current_velocity, 2),
                        "slope": round(slope, 4),
                        "r_squared": round(r2, 3),
                    })
                    logger.debug(
                        f"  Model velocity_lag{lag}d: "
                        f"pos={predicted_position} on {predicted_date} "
                        f"(R²={r2:.3f})"
                    )

    if not predictions:
        logger.debug(
            f"  {artist_name}: insufficient data for prediction"
        )
        return None

    # Select best prediction (highest R²)
    best = max(predictions, key=lambda x: x["confidence"])

    # Ensemble: average position across all models if multiple exist
    if len(predictions) > 1:
        avg_position = int(
            np.mean([p["predicted_position"] for p in predictions])
        )
        avg_confidence = round(
            np.mean([p["confidence"] for p in predictions]), 3
        )
    else:
        avg_position = best["predicted_position"]
        avg_confidence = best["confidence"]

    result = {
        "artist_name": artist_name,
        "artist_id": artist_id,
        "predicted_peak_position": avg_position,
        "predicted_peak_date": best["predicted_date"],
        "confidence": avg_confidence,
        "best_model": best["model"],
        "all_models": predictions,
        "data_points": {
            "trends_days": len(trends_history),
            "velocity_days": len(velocity_history),
            "shazam_days": len(shazam_history),
        },
        "predicted_at": str(TODAY),
    }

    return result


def save_prediction(db, prediction: dict) -> bool:
    try:
        confidence = max(0.0, min(1.0, float(prediction["confidence"])))
        predicted_pos = int(max(1, min(200, prediction["predicted_peak_position"])))
        # Store confidence as 0-100 percentage
        confidence_pct = round(confidence * 100, 2)

        # Save predicted position
        db.table("artist_snapshots").upsert({
            "artist_id": prediction["artist_id"],
            "source": "prediction_model",
            "metric_name": "shazam_predicted_position",
            "metric_value": float(predicted_pos),
            "metric_text": (
                f"Predicted Shazam peak: #{predicted_pos} "
                f"around {prediction['predicted_peak_date']} "
                f"(confidence: {confidence_pct:.0f}%)"
            ),
            "snapshot_date": str(TODAY),
            "captured_at": datetime.now().isoformat(),
        }, on_conflict="artist_id,source,metric_name,snapshot_date").execute()

        # Save confidence percentage
        db.table("artist_snapshots").upsert({
            "artist_id": prediction["artist_id"],
            "source": "prediction_model",
            "metric_name": "shazam_prediction_confidence",
            "metric_value": confidence_pct,
            "snapshot_date": str(TODAY),
            "captured_at": datetime.now().isoformat(),
        }, on_conflict="artist_id,source,metric_name,snapshot_date").execute()

        # Log breakout signal only if top 50 and high confidence
        if predicted_pos <= 50 and confidence >= 0.5:
            strength = float(min(100.0, round(
                confidence_pct * 0.7 + max(0, 50 - predicted_pos) * 0.6, 2
            )))
            db.table("breakout_signals").insert({
                "artist_id": prediction["artist_id"],
                "signal_type": "shazam_peak_predicted",
                "strength": strength,
                "sources": ["audiomack", "google_trends"],
                "signal_data": {
                    "predicted_position": predicted_pos,
                    "predicted_date": prediction["predicted_peak_date"],
                    "confidence_pct": confidence_pct,
                    "model": prediction["best_model"],
                    "data_points": prediction.get("data_points", {}),
                    "summary": (
                        f"Model predicts Shazam peak at "
                        f"#{predicted_pos} around "
                        f"{prediction['predicted_peak_date']} "
                        f"({confidence_pct:.0f}% confidence)."
                    ),
                },
                "detected_at": datetime.now().isoformat(),
                "status": "active",
            }).execute()

        logger.debug(
            f"  Saved: pos={predicted_pos}, "
            f"confidence={confidence_pct:.1f}%"
        )
        return True

    except Exception as e:
        logger.error(f"  Save prediction error: {e}")
        import traceback
        logger.debug(traceback.format_exc())
        return False


def run():
    start_time = datetime.now()
    logger.info(f"=== Starting {SCRAPER_NAME} ===")
    status = "failed"
    records_inserted = 0
    error_message = None

    try:
        db = get_supabase()

        artists = db.table("artists").select(
            "id, name, slug, tier"
        ).eq("is_active", True).order("tier").execute().data

        logger.info(f"Running predictions for {len(artists)} artists")
        logger.info("=" * 50)

        results = []

        for artist in artists:
            artist_id = artist["id"]
            artist_name = artist["name"]

            logger.info(f"Predicting: {artist_name}")

            # Gather all historical signals
            trends = get_trends_history(db, artist_id, days=30)
            velocity = get_audiomack_velocity_history(
                db, artist_id, days=30
            )
            shazam = get_shazam_history(db, artist_name, days=30)

            logger.debug(
                f"  Data: trends={len(trends)}d, "
                f"velocity={len(velocity)}d, "
                f"shazam={len(shazam)}d"
            )

            if len(trends) < 3 and len(velocity) < 3:
                logger.debug(
                    f"  {artist_name}: not enough data yet "
                    "(need at least 3 days of trends + velocity)"
                )
                continue

            prediction = predict_shazam_peak(
                artist_name, artist_id,
                trends, velocity, shazam
            )

            if prediction:
                results.append(prediction)
                if save_prediction(db, prediction):
                    records_inserted += 1

                logger.info(
                    f"  📈 Predicted peak: "
                    f"#{prediction['predicted_peak_position']} "
                    f"on {prediction['predicted_peak_date']} "
                    f"(confidence: {prediction['confidence']:.0%}, "
                    f"model: {prediction['best_model']})"
                )
            else:
                logger.debug(
                    f"  {artist_name}: no prediction generated"
                )

        # Summary table
        if results:
            logger.info("=" * 50)
            logger.info("--- SHAZAM PEAK PREDICTIONS ---")
            results.sort(
                key=lambda x: x["predicted_peak_position"]
            )
            for r in results:
                logger.info(
                    f"  {r['artist_name']}: "
                    f"#{r['predicted_peak_position']} "
                    f"by {r['predicted_peak_date']} "
                    f"({r['confidence']:.0%} confidence)"
                )

        status = "success"
        logger.success(
            f"Predictions saved for {records_inserted} artists"
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
            records_inserted=records_inserted,
            records_failed=0,
            error_message=error_message,
            duration_seconds=duration,
        )
        logger.info(f"Done. {status}. {duration:.1f}s")


if __name__ == "__main__":
    run()