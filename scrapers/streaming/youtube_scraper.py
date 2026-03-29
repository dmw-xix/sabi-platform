# scrapers/streaming/youtube_scraper.py
# Tracks YouTube metrics for Nigerian artists
# Uses YouTube Data API v3 — free, 10,000 units/day quota
# Cost per run: ~3 units per artist (1 search + 1 channel + 1 videos)

import re
import html
import time
from datetime import date, datetime
from loguru import logger
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from database.client import get_supabase, log_scraper_run
from config.settings import YOUTUBE_API_KEY

SCRAPER_NAME = "youtube_artist_snapshots"
SOURCE = "youtube"
TODAY = str(date.today())

# Units consumed per API call type
# search: 100 units, channels: 1 unit, videos: 1 unit
# With 10,000 daily units and 10 artists: ~300 units total — well within limit


def get_youtube_client():
    """Build and return YouTube API client."""
    if not YOUTUBE_API_KEY:
        raise ValueError(
            "YOUTUBE_API_KEY not set in .env file"
        )
    return build("youtube", "v3", developerKey=YOUTUBE_API_KEY)


def find_channel_id(youtube, artist_name: str, artist_slug: str) -> str | None:
    """
    Finds a YouTube channel ID for an artist.
    First checks if we have it stored, then searches YouTube.
    """
    db = get_supabase()

    # Check database first
    result = db.table("artists").select(
        "youtube_channel_id"
    ).eq("slug", artist_slug).single().execute()

    if result.data and result.data.get("youtube_channel_id"):
        channel_id = result.data["youtube_channel_id"]
        logger.debug(f"  Using stored channel ID: {channel_id}")
        return channel_id

    # Search YouTube for the channel
    # Uses 100 units — expensive, so we store the result
    logger.info(f"  Searching YouTube for: {artist_name}")
    try:
        response = youtube.search().list(
            part="snippet",
            q=f"{artist_name} official",
            type="channel",
            maxResults=3,
        ).execute()

        items = response.get("items", [])
        if not items:
            logger.warning(f"  No YouTube channel found for {artist_name}")
            return None

        # Take the first result — usually correct for major artists
        channel_id = items[0]["snippet"]["channelId"]
        channel_title = items[0]["snippet"]["title"]

        logger.info(
            f"  Found channel: '{channel_title}' ({channel_id})"
        )

        # Store it so we don't use 100 units every day
        db.table("artists").update(
            {"youtube_channel_id": channel_id}
        ).eq("slug", artist_slug).execute()

        return channel_id

    except HttpError as e:
        logger.error(f"  YouTube search error for {artist_name}: {e}")
        return None


def get_channel_stats(youtube, channel_id: str) -> dict | None:
    """
    Fetches channel-level statistics.
    Returns: subscribers, total_views, video_count
    Costs: 1 unit
    """
    try:
        response = youtube.channels().list(
            part="statistics,snippet",
            id=channel_id,
        ).execute()

        items = response.get("items", [])
        if not items:
            return None

        stats = items[0].get("statistics", {})

        return {
            "subscribers": int(stats.get("subscriberCount", 0)),
            "total_views": int(stats.get("viewCount", 0)),
            "video_count": int(stats.get("videoCount", 0)),
            "channel_title": items[0]["snippet"]["title"],
        }

    except HttpError as e:
        logger.error(f"  Channel stats error for {channel_id}: {e}")
        return None


def get_recent_videos(youtube, channel_id: str, max_results: int = 5) -> list[dict]:
    """
    Gets the most recent videos from a channel.
    Returns basic video info — we fetch full stats separately.
    Costs: 100 units (search call)

    Note: We use search because it's the only way to get recent videos
    sorted by date without the expensive Activities API.
    """
    try:
        response = youtube.search().list(
            part="snippet",
            channelId=channel_id,
            order="date",
            type="video",
            maxResults=max_results,
        ).execute()

        videos = []
        for item in response.get("items", []):
            video_id = item["id"]["videoId"]
            snippet = item["snippet"]
            videos.append({
                "video_id": video_id,
                "title": snippet.get("title", ""),
                "published_at": snippet.get("publishedAt", ""),
            })

        return videos

    except HttpError as e:
        logger.error(f"  Recent videos error for {channel_id}: {e}")
        return []


def get_video_stats(youtube, video_ids: list[str]) -> dict:
    """
    Gets view counts, likes, comments for a list of video IDs.
    Can fetch up to 50 videos in one call.
    Costs: 1 unit per call.
    Returns dict of {video_id: stats}
    """
    if not video_ids:
        return {}

    try:
        response = youtube.videos().list(
            part="statistics",
            id=",".join(video_ids),
        ).execute()

        result = {}
        for item in response.get("items", []):
            vid_id = item["id"]
            stats = item.get("statistics", {})
            result[vid_id] = {
                "views": int(stats.get("viewCount", 0)),
                "likes": int(stats.get("likeCount", 0)),
                "comments": int(stats.get("commentCount", 0)),
            }

        return result

    except HttpError as e:
        logger.error(f"  Video stats error: {e}")
        return {}


def save_artist_youtube_metrics(
    artist_id: str,
    channel_stats: dict,
    recent_video_stats: list[dict]
) -> int:
    """
    Saves YouTube metrics to artist_snapshots and track_snapshots.
    Returns number of metrics saved.
    """
    db = get_supabase()
    saved = 0

    # Channel-level metrics → artist_snapshots
    channel_metrics = {
        "youtube_subscribers": channel_stats.get("subscribers"),
        "youtube_total_views": channel_stats.get("total_views"),
        "youtube_video_count": channel_stats.get("video_count"),
    }

    for metric_name, value in channel_metrics.items():
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
            logger.error(f"  DB error saving {metric_name}: {e}")

    # Video-level metrics → track_snapshots
    for video in recent_video_stats:
        if not video.get("title") or not video.get("video_id"):
            continue

        try:
            import html
            title = html.unescape(video.get("title", ""))

            title_slug = re.sub(r'[^a-z0-9]+', '-', title.lower()).strip('-')
            track_slug = f"{title_slug}-yt"[:100]

            # Upsert track record
            track_result = db.table("tracks").upsert(
                {
                    "artist_id": artist_id,
                    "title": title,
                    "slug": track_slug,
                    "youtube_video_id": video["video_id"],
                    "release_date": video.get("published_at", "")[:10] or None,
                },
                on_conflict="slug"
            ).execute()

            if not track_result.data:
                continue

            track_id = track_result.data[0]["id"]

            # Save view count
            if video.get("views") is not None:
                db.table("track_snapshots").upsert(
                    {
                        "track_id": track_id,
                        "source": SOURCE,
                        "metric_name": "views",
                        "metric_value": video["views"],
                        "snapshot_date": TODAY,
                        "captured_at": datetime.now().isoformat(),
                    },
                    on_conflict="track_id,source,metric_name,snapshot_date"
                ).execute()
                saved += 1

            # Save likes
            if video.get("likes") is not None:
                db.table("track_snapshots").upsert(
                    {
                        "track_id": track_id,
                        "source": SOURCE,
                        "metric_name": "likes",
                        "metric_value": video["likes"],
                        "snapshot_date": TODAY,
                        "captured_at": datetime.now().isoformat(),
                    },
                    on_conflict="track_id,source,metric_name,snapshot_date"
                ).execute()

            logger.debug(
                f"  Video: '{title[:40]}' | "
                f"views={video.get('views'):,} | "
                f"likes={video.get('likes'):,}"
            )

        except Exception as e:
            logger.error(f"  DB error saving video '{video.get('title')}': {e}")

    return saved


def run():
    """
    Main entry point.
    Fetches YouTube metrics for all active artists.
    """
    start_time = datetime.now()
    logger.info(f"=== Starting {SCRAPER_NAME} ===")

    status = "failed"
    records_attempted = 0
    records_inserted = 0
    records_failed = 0
    error_message = None

    try:
        youtube = get_youtube_client()

        # Load active artists
        db = get_supabase()
        result = db.table("artists").select(
            "id, name, slug, youtube_channel_id, tier"
        ).eq("is_active", True).order("tier").execute()

        artists = result.data
        if not artists:
            logger.warning("No artists found in database")
            return

        logger.info(f"Processing {len(artists)} artists")

        for i, artist in enumerate(artists, 1):
            artist_id = artist["id"]
            artist_name = artist["name"]
            artist_slug = artist["slug"]

            logger.info(f"[{i}/{len(artists)}] {artist_name}")
            records_attempted += 1

            # Step 1: Get or find YouTube channel ID
            channel_id = find_channel_id(youtube, artist_name, artist_slug)
            if not channel_id:
                records_failed += 1
                logger.warning(f"  Skipping {artist_name} — no channel found")
                continue

            # Step 2: Get channel stats (subscribers, views, etc.)
            channel_stats = get_channel_stats(youtube, channel_id)
            if not channel_stats:
                records_failed += 1
                logger.warning(f"  No channel stats for {artist_name}")
                continue

            logger.info(
                f"  Subscribers: {channel_stats['subscribers']:,} | "
                f"Total views: {channel_stats['total_views']:,}"
            )

            # Step 3: Get recent videos
            recent_videos = get_recent_videos(youtube, channel_id, max_results=5)

            # Step 4: Get video stats (views, likes, comments)
            video_ids = [v["video_id"] for v in recent_videos]
            video_stats_map = get_video_stats(youtube, video_ids) if video_ids else {}

            # Merge stats into video dicts
            enriched_videos = []
            for video in recent_videos:
                vid_stats = video_stats_map.get(video["video_id"], {})
                enriched_videos.append({
                    **video,
                    "views": vid_stats.get("views"),
                    "likes": vid_stats.get("likes"),
                    "comments": vid_stats.get("comments"),
                })

            # Step 5: Save everything
            saved = save_artist_youtube_metrics(
                artist_id, channel_stats, enriched_videos
            )
            records_inserted += saved
            logger.info(f"  Saved {saved} YouTube metrics")

            # Polite delay — YouTube API doesn't need much but be respectful
            if i < len(artists):
                time.sleep(1)

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
        import traceback
        logger.debug(traceback.format_exc())

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
            f"Logged. Status: {status}. Duration: {duration:.1f}s"
        )


if __name__ == "__main__":
    run()