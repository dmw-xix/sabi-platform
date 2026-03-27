from supabase import create_client, Client
from config.settings import SUPABASE_URL, SUPABASE_SERVICE_KEY
from loguru import logger

_client: Client = None

def get_supabase() -> Client:
    global _client
    if _client is None:
        if not SUPABASE_URL or not SUPABASE_SERVICE_KEY:
            raise ValueError(
                "Supabase credentials not found. "
                "Check your .env file."
            )
        _client = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)
        logger.info("Supabase client initialized")
    return _client


def log_scraper_run(
    scraper_name: str,
    status: str,
    records_attempted: int = 0,
    records_inserted: int = 0,
    records_failed: int = 0,
    error_message: str = None,
    duration_seconds: float = None
):
    db = get_supabase()
    try:
        db.table("scraper_runs").insert({
            "scraper_name": scraper_name,
            "status": status,
            "records_attempted": records_attempted,
            "records_inserted": records_inserted,
            "records_failed": records_failed,
            "error_message": error_message,
            "duration_seconds": duration_seconds
        }).execute()
    except Exception as e:
        logger.error(f"Failed to log scraper run: {e}")


if __name__ == "__main__":
    client = get_supabase()
    result = client.table("artists").select("*", count="exact").execute()
    print(f"✓ Database connected. Artists table has {result.count} rows.")