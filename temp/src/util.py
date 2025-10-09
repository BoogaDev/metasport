from __future__ import annotations

from datetime import datetime, timezone
from dateutil import parser
import re


def parse_datetime_to_utc(dt_str: str) -> datetime:
    dt = parser.isoparse(dt_str)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def decimal_to_american(decimal_odds: float) -> int:
    if decimal_odds >= 2.0:
        return int(round((decimal_odds - 1.0) * 100))
    else:
        return int(round(-100 / (decimal_odds - 1.0)))


def american_to_decimal(american_odds: int | float) -> float:
    try:
        a = float(american_odds)
    except Exception:
        return 0.0
    if a >= 100:
        return round(1.0 + (a / 100.0), 4)
    if a <= -100:
        return round(1.0 + (100.0 / abs(a)), 4)
    return 0.0


def build_game_slug(
    home_abbr: str,
    away_abbr: str,
    date_yyyy_mm_dd: str,
    doubleheader_seq: int | None = None,
    week: int | None = None,
) -> str:
    slug = f"{home_abbr}_{away_abbr}_{date_yyyy_mm_dd}"
    if doubleheader_seq is not None:
        slug = f"{slug}_G{doubleheader_seq}"
    if week is not None:
        slug = f"{slug}_W{week}"
    return slug


def normalize_team_name(name: str) -> str:
    if not name:
        return ""
    s = name.strip().upper()
    # Normalize common punctuation variants
    s = s.replace("ST.", "ST.")
    s = s.replace("ST ", "ST. ")
    s = s.replace("&", "AND")
    s = re.sub(r"\s+", " ", s)
    return s


def derive_team_name_only(full_name_upper: str) -> str:
    s = normalize_team_name(full_name_upper)
    parts = s.split(" ")
    if len(parts) >= 2:
        if parts[-2:] in (["RED", "SOX"], ["WHITE", "SOX"]):
            return "SOX"
    return parts[-1] if parts else s


def deep_sum_numbers(value) -> int:
    total = 0
    if isinstance(value, dict):
        for v in value.values():
            total += deep_sum_numbers(v)
    elif isinstance(value, list):
        for v in value:
            total += deep_sum_numbers(v)
    else:
        try:
            if isinstance(value, (int, float)):
                total += int(value)
        except Exception:
            pass
    return total


def slugify_team_name_only(name: str) -> str:
    """Return uppercase hyphenated team segment for slugs.

    Examples:
    - "Blue Jays" -> "BLUE-JAYS"
    - "Red Sox" -> "RED-SOX"
    - "Rangers" -> "RANGERS"
    - None/empty -> ""
    """
    if not name:
        return ""
    s = normalize_team_name(str(name))
    # Replace internal spaces with hyphen for multi-word team-name-only parts
    s = s.replace(" ", "-")
    return s

