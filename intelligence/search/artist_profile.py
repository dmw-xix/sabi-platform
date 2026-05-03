# intelligence/search/artist_profile.py
# Search for any tracked artist and get a full data profile
# Usage: python intelligence/search/artist_profile.py "Asake"
#        python intelligence/search/artist_profile.py "Asake" --json
#        python intelligence/search/artist_profile.py "Asake" --export

import argparse
import json
from datetime import date, datetime, timedelta
from loguru import logger
from database.client import get_supabase

# ─────────────────────────────────────────────
# Data fetchers — one per source
# ─────────────────────────────────────────────

def find_artist(db, query: str) -> dict | None:
    """Find artist by name (fuzzy match)."""
    result = db.table("artists").select("*").ilike(
        "name", f"%{query}%"
    ).limit(1).execute()
    return result.data[0] if result.data else None


def get_latest_metric(
    db, artist_id: str, source: str, metric_name: str
) -> float | None:
    result = db.table("artist_snapshots").select(
        "metric_value, snapshot_date"
    ).eq("artist_id", artist_id).eq(
        "source", source
    ).eq("metric_name", metric_name).order(
        "snapshot_date", desc=True
    ).limit(1).execute()
    if result.data:
        return result.data[0]["metric_value"]
    return None


def get_audiomack_data(db, artist_id: str) -> dict:
    metrics = ["followers", "monthly_listeners", "total_plays"]
    data = {}
    for m in metrics:
        val = get_latest_metric(db, artist_id, "audiomack", m)
        if val:
            data[m] = int(val)
    return data


def get_youtube_data(db, artist_id: str) -> dict:
    metrics = [
        "youtube_subscribers",
        "youtube_total_views",
        "youtube_video_count",
    ]
    data = {}
    for m in metrics:
        val = get_latest_metric(db, artist_id, "youtube", m)
        if val:
            data[m] = int(val)
    return data


def get_kworb_data(db, artist_id: str) -> dict:
    data = {}
    countries = get_latest_metric(
        db, artist_id, "kworb_itunes", "kworb_countries_charting"
    )
    if countries:
        data["countries_charting"] = int(countries)
    global_score = get_latest_metric(
        db, artist_id, "kworb_itunes", "kworb_global_score"
    )
    if global_score:
        data["global_score"] = int(global_score)
    return data


def get_trends_data(db, artist_id: str) -> dict:
    data = {}
    for m in ["google_trends_avg", "google_trends_peak",
              "google_trends_momentum", "google_trends_score"]:
        val = get_latest_metric(db, artist_id, "google_trends", m)
        if val is not None:
            data[m] = round(float(val), 2)
    return data


def get_chart_positions(db, artist_name: str, days: int = 14) -> list:
    cutoff = str(date.today() - timedelta(days=days))
    result = db.table("chart_positions").select(
        "chart_name, position, chart_date, raw_title, "
        "daily_streams, streams_change, streams_7day"
    ).eq("raw_artist", artist_name).gte(
        "chart_date", cutoff
    ).order("chart_date", desc=True).limit(50).execute()
    return result.data


def get_top_tracks(db, artist_id: str) -> list:
    tracks = db.table("tracks").select(
        "id, title, release_date"
    ).eq("artist_id", artist_id).execute().data

    enriched = []
    for track in tracks:
        plays_result = db.table("track_snapshots").select(
            "metric_value"
        ).eq("track_id", track["id"]).eq(
            "source", "audiomack"
        ).eq("metric_name", "plays").order(
            "snapshot_date", desc=True
        ).limit(1).execute()

        plays = None
        if plays_result.data:
            plays = int(plays_result.data[0]["metric_value"])

        if plays:
            enriched.append({
                "title": track["title"],
                "plays": plays,
                "release_date": track.get("release_date"),
            })

    return sorted(enriched, key=lambda x: x["plays"], reverse=True)[:10]


def get_press_coverage(db, artist_id: str, days: int = 30) -> dict:
    cutoff = (datetime.now() - timedelta(days=days)).isoformat()
    result = db.table("press_articles").select(
        "headline, publication, sentiment_label, published_at"
    ).contains("artist_ids", [artist_id]).gte(
        "published_at", cutoff
    ).order("published_at", desc=True).execute()

    articles = result.data
    positive = sum(
        1 for a in articles if a.get("sentiment_label") == "positive"
    )
    negative = sum(
        1 for a in articles if a.get("sentiment_label") == "negative"
    )

    return {
        "total": len(articles),
        "positive": positive,
        "negative": negative,
        "neutral": len(articles) - positive - negative,
        "publications": list({a["publication"] for a in articles}),
        "recent_headlines": [a["headline"] for a in articles[:5]],
    }


def get_health_score(db, artist_id: str) -> dict | None:
    result = db.table("artist_health_scores").select("*").eq(
        "artist_id", artist_id
    ).order("score_date", desc=True).limit(1).execute()
    return result.data[0] if result.data else None


def get_breakout_signals(db, artist_id: str) -> list:
    result = db.table("breakout_signals").select(
        "signal_type, strength, sources, signal_data, detected_at"
    ).eq("artist_id", artist_id).eq(
        "status", "active"
    ).order("detected_at", desc=True).limit(5).execute()
    return result.data


def get_milestones(db, artist_id: str) -> list:
    result = db.table("artist_milestones").select(
        "milestone_text, achieved_at, source"
    ).eq("artist_id", artist_id).order(
        "achieved_at", desc=True
    ).limit(20).execute()
    return result.data


def get_prediction(db, artist_id: str) -> dict | None:
    pos = get_latest_metric(
        db, artist_id, "prediction_model", "shazam_predicted_position"
    )
    conf = get_latest_metric(
        db, artist_id, "prediction_model", "shazam_prediction_confidence"
    )
    if pos:
        return {
            "predicted_position": int(pos),
            "confidence_pct": round(float(conf), 1) if conf else None,
        }
    return None


# ─────────────────────────────────────────────
# Display
# ─────────────────────────────────────────────

def fmt(n: int | float | None, suffix: str = "") -> str:
    if n is None:
        return "—"
    if n >= 1_000_000:
        return f"{n/1_000_000:.1f}M{suffix}"
    elif n >= 1_000:
        return f"{n/1_000:.0f}K{suffix}"
    return f"{int(n)}{suffix}"


def print_profile(artist: dict, profile: dict):
    name = artist["name"]
    tier = artist.get("tier", "?")
    slug = artist.get("slug", "")

    line = "─" * 60
    print(f"\n{line}")
    print(f"  SABI ARTIST PROFILE")
    print(f"  {name}  |  Tier {tier}  |  @{slug}")
    print(f"{line}\n")

    # Health Score
    hs = profile.get("health_score")
    if hs:
        score = hs.get("score", 0)
        bar_filled = int((score / 100) * 20)
        bar = "█" * bar_filled + "░" * (20 - bar_filled)
        print(f"  HEALTH SCORE   [{bar}]  {score:.0f}/100")
        print(
            f"  Streaming: {hs.get('streaming_score', 0):.0f}  "
            f"Charts: {hs.get('radio_score', 0):.0f}  "
            f"Social: {hs.get('social_score', 0):.0f}  "
            f"Momentum: {hs.get('momentum_score', 0):.0f}  "
            f"Global: {hs.get('youtube_score', 0):.0f}"
        )
        print()

    # Streaming
    am = profile.get("audiomack", {})
    yt = profile.get("youtube", {})
    print(f"  STREAMING")
    print(
        f"  Audiomack followers:       {fmt(am.get('followers'))}"
    )
    print(
        f"  Audiomack monthly listeners: {fmt(am.get('monthly_listeners'))}"
    )
    print(
        f"  Audiomack total plays:     {fmt(am.get('total_plays'))}"
    )
    print(
        f"  YouTube subscribers:       {fmt(yt.get('youtube_subscribers'))}"
    )
    print(
        f"  YouTube total views:       {fmt(yt.get('youtube_total_views'))}"
    )
    print()

    # Charts
    charts = profile.get("charts", [])
    if charts:
        print(f"  CHART POSITIONS (last 14 days)")
        seen = set()
        for c in charts:
            key = f"{c['chart_name']}:{c['raw_title']}"
            if key in seen:
                continue
            seen.add(key)
            streams_str = (
                f"  {fmt(c['daily_streams'])}/day"
                if c.get("daily_streams") else ""
            )
            print(
                f"  #{c['position']:>3}  {c['chart_name']:<30}  "
                f"'{c['raw_title'][:25]}'{streams_str}"
            )
            if len(seen) >= 10:
                break
        print()

    # Global
    kw = profile.get("kworb", {})
    print(f"  GLOBAL PRESENCE")
    print(f"  Countries charting: {kw.get('countries_charting', '—')}")
    print(f"  Global score: {kw.get('global_score', '—')}")
    print()

    # Top Tracks
    tracks = profile.get("top_tracks", [])
    if tracks:
        print(f"  TOP TRACKS (Audiomack plays)")
        for t in tracks[:5]:
            print(
                f"  {fmt(t['plays']):>8}  {t['title'][:45]}"
            )
        print()

    # Press
    press = profile.get("press", {})
    if press.get("total"):
        print(
            f"  PRESS  (30 days)  "
            f"{press['total']} articles  |  "
            f"+{press['positive']} positive  "
            f"-{press['negative']} negative"
        )
        for h in press.get("recent_headlines", [])[:3]:
            print(f"  → {h[:65]}")
        print()

    # Google Trends
    trends = profile.get("trends", {})
    if trends:
        print(f"  SEARCH INTEREST (Nigeria)")
        if trends.get("google_trends_avg"):
            print(
                f"  7-day average: {trends['google_trends_avg']:.0f}/100  "
                f"Peak: {trends.get('google_trends_peak', 0):.0f}/100"
            )
        if trends.get("google_trends_momentum"):
            m = trends["google_trends_momentum"]
            arrow = "↑" if m > 0 else "↓"
            print(f"  Week-over-week: {arrow} {abs(m):.1f}%")
        print()

    # Prediction
    pred = profile.get("prediction")
    if pred:
        print(
            f"  SHAZAM PEAK PREDICTION  "
            f"#{pred['predicted_position']}  "
            f"({pred.get('confidence_pct', '?')}% confidence)"
        )
        print()

    # Breakout Signals
    signals = profile.get("breakout_signals", [])
    if signals:
        print(f"  BREAKOUT SIGNALS")
        for s in signals[:3]:
            summary = (
                s.get("signal_data", {}).get("summary", "")[:65]
            )
            print(
                f"  [{s['strength']:.0f}] {s['signal_type']}: "
                f"{summary}"
            )
        print()

    # Milestones
    milestones = profile.get("milestones", [])
    if milestones:
        print(f"  RECENT MILESTONES")
        for m in milestones[:5]:
            achieved = m["achieved_at"][:10]
            print(f"  {achieved}  {m['milestone_text']}")
        print()

    print(f"{line}\n")


# ─────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────

def search(query: str) -> dict | None:
    db = get_supabase()

    artist = find_artist(db, query)
    if not artist:
        logger.error(f"No artist found matching: '{query}'")
        return None

    artist_id = artist["id"]
    artist_name = artist["name"]

    logger.info(f"Loading profile for: {artist_name}")

    profile = {
        "artist": artist,
        "audiomack": get_audiomack_data(db, artist_id),
        "youtube": get_youtube_data(db, artist_id),
        "kworb": get_kworb_data(db, artist_id),
        "trends": get_trends_data(db, artist_id),
        "charts": get_chart_positions(db, artist_name),
        "top_tracks": get_top_tracks(db, artist_id),
        "press": get_press_coverage(db, artist_id),
        "health_score": get_health_score(db, artist_id),
        "breakout_signals": get_breakout_signals(db, artist_id),
        "milestones": get_milestones(db, artist_id),
        "prediction": get_prediction(db, artist_id),
        "generated_at": datetime.now().isoformat(),
    }

    return profile


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Sabi Artist Profile Search"
    )
    parser.add_argument("artist", help="Artist name to search")
    parser.add_argument(
        "--json", action="store_true",
        help="Output as JSON"
    )
    parser.add_argument(
        "--export", action="store_true",
        help="Save JSON to reports/output/"
    )
    args = parser.parse_args()

    profile = search(args.artist)

    if not profile:
        exit(1)

    if args.json or args.export:
        output = json.dumps(profile, indent=2, default=str)
        if args.export:
            import os
            os.makedirs("reports/output", exist_ok=True)
            filename = (
                f"reports/output/"
                f"{profile['artist']['slug']}_profile_"
                f"{date.today()}.json"
            )
            with open(filename, "w") as f:
                f.write(output)
            print(f"Saved: {filename}")
        else:
            print(output)
    else:
        print_profile(profile["artist"], profile)