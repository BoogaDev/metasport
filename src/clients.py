import os
import httpx
from supabase import create_client, Client
from supabase.client import ClientOptions

from .config import SUPABASE_URL, SUPABASE_SERVICE_ROLE, SUPABASE_SCHEMA, APISPORTS_KEY


def create_supabase_client() -> Client:
    url = SUPABASE_URL
    key = SUPABASE_SERVICE_ROLE or os.environ.get("SUPABASE_KEY", "")
    if not url or not key:
        raise RuntimeError("Missing SUPABASE_URL or SUPABASE_SERVICE_ROLE/KEY")
    return create_client(url, key, options=ClientOptions(schema=SUPABASE_SCHEMA))


def create_apisports_client(timeout: float = 20.0) -> httpx.Client:
    if not APISPORTS_KEY:
        raise RuntimeError("Missing APISPORTS_KEY")
    headers = {"x-apisports-key": APISPORTS_KEY}
    return httpx.Client(timeout=timeout, headers=headers)


