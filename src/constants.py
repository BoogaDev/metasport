# API-Sports base URLs per sport
HOCKEY_BASE = "https://v1.hockey.api-sports.io"
BASEBALL_BASE = "https://v1.baseball.api-sports.io"
FOOTBALL_BASE = "https://v1.american-football.api-sports.io"

# Will fetch dynamically, but keep defaults to avoid blocking
DEFAULT_LEAGUE_IDS = {
    "NHL": 57,   # commonly NHL id in API-Sports Hockey
    "MLB": 1,    # commonly MLB id in API-Sports Baseball
    "NFL": 1,    # commonly NFL id in American Football; verify at runtime
}

SPORT_META = {
    "NHL": {
        "base": HOCKEY_BASE,
        "line_units": "goals",
    },
    "MLB": {
        "base": BASEBALL_BASE,
        "line_units": "runs",
    },
    "NFL": {
        "base": FOOTBALL_BASE,
        "line_units": "points",
    },
}


