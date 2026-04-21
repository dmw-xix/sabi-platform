import os
from dotenv import load_dotenv

load_dotenv()

# ── Supabase ──────────────────────────────────────
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_ANON_KEY = os.getenv("SUPABASE_ANON_KEY")
SUPABASE_SERVICE_KEY = os.getenv("SUPABASE_SERVICE_KEY")
DATABASE_URL = os.getenv("DATABASE_URL")

# ── API Keys ──────────────────────────────────────
YOUTUBE_API_KEY = os.getenv("YOUTUBE_API_KEY", "")
TWITTER_BEARER_TOKEN = os.getenv("TWITTER_BEARER_TOKEN", "")

# ── Scraper Settings ──────────────────────────────
SCRAPER_DELAY_SECONDS = 2
SCRAPER_TIMEOUT_SECONDS = 30
MAX_RETRIES = 3

# ── Chart URLs ────────────────────────────────────
CHART_SOURCES = {
    "spotify_ng_daily": "https://charts.spotify.com/charts/view/regional-ng-daily/latest",
    "apple_music_ng": "https://music.apple.com/ng/charts",
    "shazam_ng": "https://www.shazam.com/charts/top-200/nigeria",
    "turntable_ng": "https://turntablecharts.com/",
}

# ── Press RSS Feeds ───────────────────────────────
RSS_FEEDS = {
    "pulse_nigeria": "https://www.pulse.ng/entertainment/music/feed/",
    "the_native": "https://thenativemag.com/feed/",
    "notjustok": "https://www.notjustok.com/feed/",
    "okay_africa": "https://www.okayafrica.com/feed/",
    "vanguard_ent": "https://www.vanguardngr.com/entertainment/feed/",
    "instablog9ja": "https://instablog9ja.com/feed/",

}

# ── Logging ───────────────────────────────────────
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
LOG_DIR = "logs"

def validate_config():
    required = ["SUPABASE_URL", "SUPABASE_ANON_KEY", "SUPABASE_SERVICE_KEY"]
    missing = [key for key in required if not os.getenv(key)]
    if missing:
        raise ValueError(f"Missing required environment variables: {missing}")

if __name__ == "__main__":
    validate_config()
    print("✓ Config valid")

GOOGLE_TRENDS_COOKIE = os.getenv("GOOGLE_TRENDS_COOKIE", "")
