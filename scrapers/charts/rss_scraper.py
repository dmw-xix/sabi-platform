# scrapers/charts/rss_scraper.py
# Ingests music press RSS feeds from Nigerian and African music media
# Detects which artists are mentioned, scores sentiment
# Free — no API key required

import hashlib
import time
from datetime import datetime, timezone
from loguru import logger
import feedparser
import requests
from database.client import get_supabase, log_scraper_run

SCRAPER_NAME = "rss_press_feeds"
TODAY = datetime.now(timezone.utc).isoformat()

RSS_FEEDS = {
    "notjustok":       "https://www.notjustok.com/feed/",
    "the_native":      "https://thenativemag.com/feed/",
    "dailypost_ng":    "https://dailypost.ng/feed/",
    "linda_ikeji":      "https://www.lindaikejisblog.com/feeds/",
    "bellanaija_music": "https://www.bellanaija.com/feed/",
    "vanguard_ent":    "https://www.vanguardngr.com/category/entertainment/feed/",
}

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (compatible; SabiBot/1.0; "
        "+https://sabi.music/bot)"
    )
}

# Simple positive/negative word lists tuned for music press
POSITIVE_WORDS = {
    "hit", "banger", "fire", "amazing", "incredible", "outstanding",
    "massive", "successful", "topped", "number one", "milestone",
    "record", "award", "win", "winner", "best", "great", "excellent",
    "viral", "trending", "breakthrough", "impressive", "celebrated",
    "anthem", "classic", "iconic", "legendary", "genius", "talented",
}

NEGATIVE_WORDS = {
    "controversy", "beef", "fight", "arrested", "lawsuit", "plagiarism",
    "disappointing", "flop", "failed", "cancelled", "banned", "accused",
    "scandal", "feud", "alleged", "attack", "backlash", "criticism",
    "stolen", "fraud", "fake", "exposed", "dropped",
}


def score_sentiment(text: str) -> tuple[float, str]:
    """
    Simple lexicon-based sentiment scoring.
    Returns (score, label) where score is -1.0 to 1.0.
    Not as accurate as a model but free and fast.
    We'll upgrade to afro-xlmr later.
    """
    if not text:
        return 0.0, "neutral"

    words = text.lower().split()
    word_set = set(words)

    positive_hits = len(word_set & POSITIVE_WORDS)
    negative_hits = len(word_set & NEGATIVE_WORDS)

    total = positive_hits + negative_hits
    if total == 0:
        return 0.0, "neutral"

    score = (positive_hits - negative_hits) / total

    if score > 0.1:
        label = "positive"
    elif score < -0.1:
        label = "negative"
    else:
        label = "neutral"

    return round(score, 3), label


def detect_artists(text: str, artist_lookup: dict) -> list[str]:
    """
    Finds which tracked artists are mentioned in an article.
    artist_lookup: {artist_name.lower(): artist_id}
    Returns list of artist UUIDs.
    """
    text_lower = text.lower()
    found_ids = []

    for name_lower, artist_id in artist_lookup.items():
        if name_lower in text_lower:
            found_ids.append(artist_id)

    return found_ids


def fetch_feed(feed_name: str, feed_url: str) -> list[dict]:
    """
    Fetches and parses a single RSS feed.
    Returns list of article dicts.
    """
    articles = []

    try:
        logger.info(f"  Fetching: {feed_name}")

        # Use requests first to handle redirects cleanly
        response = requests.get(
            feed_url,
            headers=HEADERS,
            timeout=15,
            allow_redirects=True,
        )

        if response.status_code != 200:
            logger.warning(
                f"  {feed_name}: HTTP {response.status_code}"
            )
            return []

        # Parse with feedparser
        feed = feedparser.parse(response.content)

        if feed.bozo and not feed.entries:
            logger.warning(
                f"  {feed_name}: Feed parse error — {feed.bozo_exception}"
            )
            return []

        logger.info(
            f"  {feed_name}: {len(feed.entries)} entries"
        )

        for entry in feed.entries[:20]:  # Max 20 articles per feed per run
            try:
                # Extract URL
                url = entry.get("link", "")
                if not url:
                    continue

                # Generate URL hash for deduplication
                url_hash = hashlib.md5(url.encode()).hexdigest()

                # Extract headline
                headline = entry.get("title", "").strip()

                # Extract summary/description
                summary = ""
                if hasattr(entry, "summary"):
                    # Strip HTML tags from summary
                    import re
                    summary = re.sub(r'<[^>]+>', '', entry.summary)
                    summary = summary.strip()[:500]  # Truncate long summaries

                # Extract published date
                published_at = None
                if hasattr(entry, "published_parsed") and entry.published_parsed:
                    try:
                        published_at = datetime(
                            *entry.published_parsed[:6],
                            tzinfo=timezone.utc
                        ).isoformat()
                    except Exception:
                        pass

                # Combine text for analysis
                full_text = f"{headline} {summary}"

                articles.append({
                    "url": url,
                    "url_hash": url_hash,
                    "publication": feed_name,
                    "headline": headline,
                    "summary": summary,
                    "published_at": published_at,
                    "full_text": full_text,
                })

            except Exception as e:
                logger.debug(f"  Failed to parse entry: {e}")
                continue

    except requests.RequestException as e:
        logger.error(f"  {feed_name}: Request failed — {e}")
    except Exception as e:
        logger.error(f"  {feed_name}: Unexpected error — {e}")

    return articles


def save_articles(
    articles: list[dict],
    artist_lookup: dict
) -> tuple[int, int]:
    """
    Saves articles to press_articles table.
    Skips duplicates via url_hash.
    Returns (inserted, skipped).
    """
    db = get_supabase()
    inserted = 0
    skipped = 0

    for article in articles:
        try:
            # Score sentiment
            sentiment_score, sentiment_label = score_sentiment(
                article["full_text"]
            )

            # Detect mentioned artists
            artist_ids = detect_artists(
                article["full_text"], artist_lookup
            )

            # Upsert — url_hash is unique so duplicates are skipped
            result = db.table("press_articles").upsert(
                {
                    "url": article["url"],
                    "url_hash": article["url_hash"],
                    "publication": article["publication"],
                    "headline": article["headline"],
                    "summary": article.get("summary", ""),
                    "artist_ids": artist_ids if artist_ids else None,
                    "sentiment_score": sentiment_score,
                    "sentiment_label": sentiment_label,
                    "published_at": article.get("published_at"),
                    "scraped_at": TODAY,
                },
                on_conflict="url_hash"
            ).execute()

            if result.data:
                inserted += 1
                if artist_ids:
                    logger.debug(
                        f"  Saved: '{article['headline'][:60]}' "
                        f"| artists={len(artist_ids)} "
                        f"| sentiment={sentiment_label}"
                    )
            else:
                skipped += 1

        except Exception as e:
            # Unique constraint violation = already exists
            if "unique" in str(e).lower() or "23505" in str(e):
                skipped += 1
            else:
                logger.error(
                    f"  DB error saving '{article.get('headline', '')[:40]}': {e}"
                )

    return inserted, skipped


def run():
    """Main entry point."""
    start_time = datetime.now()
    logger.info(f"=== Starting {SCRAPER_NAME} ===")

    status = "failed"
    records_attempted = 0
    records_inserted = 0
    records_failed = 0
    error_message = None

    try:
        db = get_supabase()

        # Build artist lookup: {name_lowercase: id}
        result = db.table("artists").select(
            "id, name"
        ).eq("is_active", True).execute()

        artist_lookup = {
            a["name"].lower(): a["id"]
            for a in result.data
        }

        # Also add common name variants
        name_variants = {
            "burna": artist_lookup.get("burna boy"),
            "wiz": artist_lookup.get("wizkid"),
            "davido": artist_lookup.get("davido"),
            "bnxn": artist_lookup.get("bnxn"),
            "buju benson": artist_lookup.get("bnxn"),
            "omah lay": artist_lookup.get("omah lay"),
            "ayra": artist_lookup.get("ayra starr"),
            "fireboy": artist_lookup.get("fireboy dml"),
            "asake": artist_lookup.get("asake"),
        }
        # Add non-None variants
        artist_lookup.update(
            {k: v for k, v in name_variants.items() if v}
        )

        logger.info(
            f"Tracking {len(result.data)} artists in press mentions"
        )

        # Process each feed
        all_articles = []
        for feed_name, feed_url in RSS_FEEDS.items():
            articles = fetch_feed(feed_name, feed_url)
            all_articles.extend(articles)
            records_attempted += len(articles)

            # Small delay between feeds
            time.sleep(1)

        logger.info(
            f"Total articles fetched: {len(all_articles)}"
        )

        # Save to database
        inserted, skipped = save_articles(all_articles, artist_lookup)
        records_inserted = inserted

        logger.info(
            f"Articles: {inserted} new, {skipped} already existed"
        )

        # Log which artists got press coverage today
        mentioned_artists = set()
        for article in all_articles:
            for name, aid in artist_lookup.items():
                if name in article.get("full_text", "").lower():
                    mentioned_artists.add(name)

        if mentioned_artists:
            logger.info(
                f"Artists mentioned in press today: "
                f"{', '.join(sorted(mentioned_artists))}"
            )

        status = "success" if records_failed == 0 else "partial"
        logger.success(
            f"Complete: {records_inserted} articles saved"
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