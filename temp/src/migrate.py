from __future__ import annotations

import os
import psycopg
from dotenv import load_dotenv


def run_migrations() -> None:
    # Load .env if present
    load_dotenv()
    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        raise SystemExit("DATABASE_URL not set")
    with psycopg.connect(dsn, autocommit=True) as conn:
        cur = conn.cursor()
        with open("migrations/001_add_external_refs.sql", "r", encoding="utf-8") as f:
            cur.execute(f.read())
        try:
            with open("migrations/002_add_team_api_columns.sql", "r", encoding="utf-8") as f:
                cur.execute(f.read())
        except FileNotFoundError:
            pass


if __name__ == "__main__":
    run_migrations()


