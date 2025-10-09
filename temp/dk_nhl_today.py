import json, math, sys
from pathlib import Path
from datetime import datetime, timedelta
import pytz, requests, pandas as pd
from dateutil import parser

# ---------- Config ----------
DK_EVENTGROUP_URL = (
    "https://sportsbook.draftkings.com/sites/US-SB/api/v5/eventgroups/42133?format=json"
)
BOOK_NAME = "DraftKings"
SPORTSBOOK_ID = "4d7a01fa-2e62-48d2-aa87-41820bebf031"  # keep constant per your sample
OUTPUT = "llmjsonoutput_today.json"
LOCAL_TZ = pytz.timezone("America/Los_Angeles")
# ----------------------------


def round_to_half_hour(dt: datetime) -> datetime:
    # Round to nearest 0 or 30 minutes
    minute = dt.minute
    if minute < 15:
        minute_rounded = 0
    elif minute < 45:
        minute_rounded = 30
    else:
        # round up to next hour
        dt = dt + timedelta(hours=1)
        minute_rounded = 0
    return dt.replace(minute=minute_rounded, second=0, microsecond=0)


def american_to_decimal(american: int) -> float:
    # +X => 1 + X/100 ; -X => 1 + 100/X_abs
    if american is None:
        return None
    if american >= 100:
        return round(1 + (american / 100.0), 2)
    elif american <= -100:
        return round(1 + (100.0 / abs(american)), 2)
    else:
        # very rare cases; fall back to None
        return None


def load_team_map(csv_path: Path):
    # Expecting a CSV with team names and stable IDs used in your system
    # Create a permissive map (uppercased, various aliases)
    df = pd.read_csv(csv_path)
    # Try common columns; adjust if your schema differs
    # e.g., columns: team_id, team_name, display_name, dk_name, alias1, alias2, etc.
    cols = [c for c in df.columns]
    name_cols = [c for c in cols if "name" in c.lower() or "alias" in c.lower()]
    id_col = None
    for c in cols:
        if "id" in c.lower():
            id_col = c
            break
    if not id_col or not name_cols:
        raise RuntimeError(
            "teams_rows.csv must have at least one *name* column and an *id* column."
        )

    mapping = {}
    for _, r in df.iterrows():
        tid = str(r[id_col])
        for c in name_cols:
            val = str(r[c]).strip()
            if val and val.lower() != "nan":
                mapping[val.upper()] = tid
    return mapping


def pick_moneyline_offer(category):
    # Find moneyline offer in event’s "offers"
    # In DK API, categoryName "Moneyline" or "Game Lines" w/ label Moneyline
    for offer_cat in category.get("offerCategories", []):
        for subcat in offer_cat.get("offerSubcategoryDescriptors", []):
            for descriptor in subcat.get("offerSubcategory", {}).get("offers", []):
                # each descriptor is a list of markets; we look for Moneyline market
                for market in descriptor:
                    label = market.get("label", "")
                    if label.lower() == "moneyline":
                        return market
    return None


def pick_puckline_offer(category):
    # Look for Puck Line / Spread (-1.5 / +1.5)
    for offer_cat in category.get("offerCategories", []):
        for subcat in offer_cat.get("offerSubcategoryDescriptors", []):
            for descriptor in subcat.get("offerSubcategory", {}).get("offers", []):
                for market in descriptor:
                    label = market.get("label", "").lower()
                    if "puck line" in label or label == "spread":
                        return market
    return None


def pick_total_offer(category):
    # Look for Totals market (Over/Under)
    for offer_cat in category.get("offerCategories", []):
        for subcat in offer_cat.get("offerSubcategoryDescriptors", []):
            for descriptor in subcat.get("offerSubcategory", {}).get("offers", []):
                for market in descriptor:
                    label = market.get("label", "").lower()
                    if label in ("total", "totals", "over/under", "total goals"):
                        return market
    return None


def parse_runner_price(runner):
    # Returns (team_name_or_side, american_price, line_number_if_any)
    # For moneyline: runner['participant'] has team name; for totals: 'over'/'under' in label
    name = (runner.get("participant", {}) or {}).get("name") or runner.get("label")
    price_american = runner.get("oddsAmerican")
    line = runner.get("line")
    # Normalize American as int where possible
    try:
        if isinstance(price_american, str) and price_american.strip().lower() != "ev":
            price_american = int(price_american)
        elif isinstance(price_american, str) and price_american.strip().lower() == "ev":
            price_american = 100  # even money
    except Exception:
        price_american = None
    return name, price_american, line


def main():
    # Load DraftKings data
    r = requests.get(DK_EVENTGROUP_URL, timeout=20)
    r.raise_for_status()
    data = r.json()

    # Load team map for IDs
    team_map = load_team_map(Path("teams_rows.csv"))

    # Build per-day filter for *today* in America/Los_Angeles
    now_local = datetime.now(LOCAL_TZ)
    today_local_date = now_local.date()
    start_local = LOCAL_TZ.localize(
        datetime.combine(today_local_date, datetime.min.time())
    )
    end_local = start_local + timedelta(days=1)

    out = {
        "time_zone": "America/Los_Angeles",
        "book": BOOK_NAME,
        "sportsbook_id": SPORTSBOOK_ID,
        "games": [],
    }

    # eventGroup -> events + categories contain offers/odds
    events = data.get("eventGroup", {}).get("events", []) or []
    categories = data.get("eventGroup", {}).get("offerCategories", []) or []

    # Index categories by eventId for quick lookup of offers
    cat_by_event = {}
    for cat in categories:
        for eventCat in cat.get("eventGroupOfferCategories", []):
            for e in eventCat.get("eventCategoryOfferIds", []):
                # v5 tends to carry mapping: {"eventId": ..., "offerCategoryId": ...}
                ev_id = e.get("eventId")
                if ev_id is not None:
                    cat_by_event.setdefault(ev_id, []).append(cat)

    for ev in events:
        # Parse event time (UTC)
        ev_id = ev.get("eventId")
        home = (ev.get("team1", {}) or {}).get("name", "")
        away = (ev.get("team2", {}) or {}).get("name", "")
        # DK sometimes uses 'name' or 'fullName'; normalize
        home = home or (ev.get("team1", {}) or {}).get("fullName", "")
        away = away or (ev.get("team2", {}) or {}).get("fullName", "")

        # Parse start time
        start_utc = parser.isoparse(ev["startDate"]) if ev.get("startDate") else None
        if not start_utc:
            continue
        # Convert to local, filter today
        start_local_dt = start_utc.astimezone(LOCAL_TZ)
        if not (start_local <= start_local_dt < end_local):
            continue  # only today's games in America/Los_Angeles

        # Round to nearest half-hour (local), then carry UTC string (rounded) as requested
        rounded_local = round_to_half_hour(start_local_dt)
        rounded_utc = rounded_local.astimezone(pytz.UTC)

        # Map team IDs using teams_rows.csv (case-insensitive)
        def find_id(name):
            # Try direct
            t = team_map.get(name.upper())
            if t:
                return t
            # Try without city (e.g., "Toronto Maple Leafs" -> "Maple Leafs")
            parts = name.upper().split()
            if len(parts) >= 2:
                short = " ".join(parts[-2:])
                t = team_map.get(short)
                if t:
                    return t
            # As-is fallback
            return None

        away_id = find_id(away)
        home_id = find_id(home)

        # Grab odds for this event from offers categories
        ev_cats = cat_by_event.get(ev_id, [])
        # Merge all offer trees for this event into a pseudo-category
        merged = {"offerCategories": []}
        for c in ev_cats:
            merged["offerCategories"].append(c)

        moneyline = pick_moneyline_offer(merged)
        puckline = pick_puckline_offer(merged)
        totals = pick_total_offer(merged)

        # Moneyline odds
        ml_away_am = ml_home_am = None
        if moneyline:
            for runner in (
                moneyline.get("outcomes", [])
                or moneyline.get("outcomesBySection", [])
                or []
            ):
                # outcomes sometimes nested; normalize list of runners
                if (
                    isinstance(runner, dict)
                    and "label" in runner
                    and "oddsAmerican" in runner
                ):
                    nm, price, _ = parse_runner_price(runner)
                    if nm and away.upper() in nm.upper():
                        ml_away_am = price
                    elif nm and home.upper() in nm.upper():
                        ml_home_am = price

        # Spread (puck line) odds
        pl_away_line = pl_home_line = None
        pl_away_am = pl_home_am = None
        if puckline:
            for runner in puckline.get("outcomes", []) or []:
                nm, price, line = parse_runner_price(runner)
                if nm and away.upper() in nm.upper():
                    pl_away_line = float(line) if line is not None else None
                    pl_away_am = price
                elif nm and home.upper() in nm.upper():
                    pl_home_line = float(line) if line is not None else None
                    pl_home_am = price

        # Totals
        total_number = None
        ou_over_am = ou_under_am = None
        if totals:
            for runner in totals.get("outcomes", []) or []:
                nm, price, line = parse_runner_price(runner)
                if nm and "over" in nm.lower():
                    total_number = float(line) if line is not None else total_number
                    ou_over_am = price
                elif nm and "under" in nm.lower():
                    total_number = float(line) if line is not None else total_number
                    ou_under_am = price

        game_obj = {
            "game_id": f"{(away[:3] or 'AWY').upper()}_{(home[:3] or 'HME').upper()}_{rounded_local.date().isoformat()}",
            "game_date": rounded_local.date().isoformat(),
            "start_time": rounded_utc.strftime("%Y-%m-%d %H:%M:%S+00"),
            "timestamp_utc": datetime.now(pytz.UTC).strftime("%Y-%m-%d %H:%M:%S.%f+00"),
            "away_team": away.upper(),
            "away_team_id": away_id,
            "home_team": home.upper(),
            "home_team_id": home_id,
            "market": {
                "moneyline": {
                    "away": {
                        "price_american": ml_away_am,
                        "price_european": american_to_decimal(ml_away_am),
                    },
                    "home": {
                        "price_american": ml_home_am,
                        "price_european": american_to_decimal(ml_home_am),
                    },
                },
                "spread": {"away": pl_away_line, "home": pl_home_line},
                "spread_odds": {
                    "away": {
                        "price_american": pl_away_am,
                        "price_european": american_to_decimal(pl_away_am),
                    },
                    "home": {
                        "price_american": pl_home_am,
                        "price_european": american_to_decimal(pl_home_am),
                    },
                },
                "total_goals": {"number": total_number},
                "total_goals_odds": {
                    "over": {
                        "price_american": ou_over_am,
                        "price_european": american_to_decimal(ou_over_am),
                    },
                    "under": {
                        "price_american": ou_under_am,
                        "price_european": american_to_decimal(ou_under_am),
                    },
                },
            },
        }

        out["games"].append(game_obj)

    # Finalize
    out["time_zone"] = "America/Los_Angeles"
    out["book"] = BOOK_NAME
    out["sportsbook_id"] = SPORTSBOOK_ID

    # Basic sanity: ensure all today’s games captured
    if not out["games"]:
        print(
            "Warning: No NHL games for today in America/Los_Angeles detected from DK feed.",
            file=sys.stderr,
        )

    Path(OUTPUT).write_text(json.dumps(out, indent=4))
    print(f"Wrote {OUTPUT} with {len(out['games'])} games.")


if __name__ == "__main__":
    main()
