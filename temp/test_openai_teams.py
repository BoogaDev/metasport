from __future__ import annotations

import argparse
import json
from pathlib import Path

from dotenv import load_dotenv
from openai import OpenAI


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--teams",
        default=str(Path(__file__).parent / "teams_rows.json"),
        help="Path to teams_rows.json",
    )
    ap.add_argument(
        "--question",
        default=(
            "Given teams_rows.json, return ONLY JSON with: "
            "count (num teams) and sample_names (up to 5 names)."
        ),
    )
    ap.add_argument("--model", default="gpt-5")
    args = ap.parse_args()

    # Load env so OPENAI_API_KEY is picked up
    load_dotenv()

    teams_path = Path(args.teams)
    if not teams_path.exists():
        raise SystemExit(f"Missing file: {teams_path}")
    teams_text = teams_path.read_text()

    client = OpenAI()

    # Send a simple JSON-only request including the file contents
    messages = [
        {"role": "system", "content": "Respond with JSON only."},
        {
            "role": "user",
            "content": [
                {"type": "text", "text": args.question},
                {"type": "text", "text": "teams_rows.json:"},
                {"type": "text", "text": teams_text},
            ],
        },
    ]

    resp = client.chat.completions.create(
        model=args.model,
        messages=messages,
        response_format={"type": "json_object"},
    )

    raw = resp.choices[0].message.content if resp and resp.choices else "{}"
    try:
        data = json.loads(raw)
    except Exception:
        # Extract best-effort JSON
        s = raw[raw.find("{") : raw.rfind("}") + 1]
        data = json.loads(s or "{}")

    print(json.dumps(data, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


