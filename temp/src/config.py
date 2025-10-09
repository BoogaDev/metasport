import os
from dotenv import load_dotenv

# Load variables from a local .env if present so scripts work without export
load_dotenv()

APISPORTS_KEY = os.environ.get("APISPORTS_KEY", "")
SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_SERVICE_ROLE = os.environ.get("SUPABASE_SERVICE_ROLE", "")
SUPABASE_SCHEMA = os.environ.get("SUPABASE_SCHEMA", "public")
TIMEZONE = os.environ.get("TZ", "America/New_York")

# The Odds API (V4)
ODDS_API_KEY = os.environ.get("ODDS_API_KEY", "")
ODDS_API_REGIONS = os.environ.get("ODDS_API_REGIONS", "us")


def require_env(var_name: str) -> str:
    value = os.environ.get(var_name)
    if not value:
        raise RuntimeError(f"Missing required environment variable: {var_name}")
    return value


