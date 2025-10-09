from __future__ import annotations

import httpx
from .constants import HOCKEY_BASE, BASEBALL_BASE, FOOTBALL_BASE, DEFAULT_LEAGUE_IDS


def discover_league_ids(client: httpx.Client) -> dict:
    ids = dict(DEFAULT_LEAGUE_IDS)
    try:
        r = client.get(f"{HOCKEY_BASE}/leagues")
        r.raise_for_status()
        for item in r.json().get("response", []):
            if str(item.get("name", "")).lower() == "nhl":
                ids["NHL"] = item.get("id", ids["NHL"])
                break
    except Exception:
        pass
    try:
        r = client.get(f"{BASEBALL_BASE}/leagues")
        r.raise_for_status()
        for item in r.json().get("response", []):
            if "mlb" in str(item.get("name", "")).lower():
                ids["MLB"] = item.get("id", ids["MLB"])
                break
    except Exception:
        pass
    try:
        r = client.get(f"{FOOTBALL_BASE}/leagues")
        r.raise_for_status()
        for item in r.json().get("response", []):
            if str(item.get("name", "")).lower() == "nfl":
                ids["NFL"] = item.get("id", ids["NFL"])
                break
    except Exception:
        pass
    return ids


