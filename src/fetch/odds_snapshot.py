"""
Odds snapshot: captures The Odds API data at named checkpoints.

Slots:
  midnight — 06:00 UTC = midnight MDT (game eve)
  morning  — 14:00 UTC = 8:00 AM MDT  (day-of)

Persists to data/odds_history_YYYY-MM-DD.json.
Schema:
  {
    "date": "YYYY-MM-DD",
    "snapshots": {
      "midnight": {
        "slot":        "midnight",
        "captured_at": "2025-04-15T06:00:00Z",
        "label":       "Midnight MT",
        "odds": { "NYY_BOS": {...}, ... }
      },
      "morning": { ... }
    }
  }

Run via:
  python -m src.fetch.odds_snapshot midnight
  python -m src.fetch.odds_snapshot morning [--date YYYY-MM-DD]
"""

import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).parents[2]
sys.path.insert(0, str(ROOT))

from src.fetch.odds import fetch_odds

logger  = logging.getLogger(__name__)
DATA_DIR = ROOT / "data"

SLOTS: dict[str, str] = {
    "midnight": "Midnight MT",
    "morning":  "8am MT",
}


def save_odds_snapshot(slot: str, date_str: str | None = None) -> None:
    """
    Fetch current odds from The Odds API and save them under `slot`
    in data/odds_history_{date_str}.json.
    """
    if slot not in SLOTS:
        raise ValueError(f"Unknown slot {slot!r}. Must be one of: {list(SLOTS)}")

    if date_str is None:
        date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    history_path = DATA_DIR / f"odds_history_{date_str}.json"
    if history_path.exists():
        history = json.loads(history_path.read_text())
    else:
        history = {"date": date_str, "snapshots": {}}

    logger.info("Fetching odds for slot=%s date=%s …", slot, date_str)
    odds = fetch_odds()

    if not odds:
        logger.warning("No odds returned — snapshot NOT saved for slot=%s", slot)
        return

    history["snapshots"][slot] = {
        "slot":        slot,
        "captured_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "label":       SLOTS[slot],
        "odds":        odds,
    }

    DATA_DIR.mkdir(exist_ok=True)
    history_path.write_text(json.dumps(history, indent=2))
    logger.info(
        "Saved odds snapshot: slot=%s  date=%s  games=%d",
        slot, date_str, len(odds),
    )


if __name__ == "__main__":
    import argparse
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

    parser = argparse.ArgumentParser(description="Save an odds checkpoint to odds_history_YYYY-MM-DD.json")
    parser.add_argument("slot",   choices=list(SLOTS), help="Checkpoint name")
    parser.add_argument("--date", default=None,         help="YYYY-MM-DD (default: today UTC)")
    args = parser.parse_args()

    save_odds_snapshot(args.slot, args.date)
