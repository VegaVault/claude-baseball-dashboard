"""
Lineup updater: lightweight job that runs every 15 minutes.

For each game in today's snapshot:
  - If first_pitch has passed → freeze (per CLAUDE.md rule #6)
  - If lineup_status == "projected" AND first_pitch is 15–120 min away
    → check MLB Stats API for confirmed lineup
    → if confirmed: enrich with handedness + xwOBA and update JSON
  - Never touches a game that is already frozen.

Rewrites data/YYYY-MM-DD.json in place only if something changed.
"""

import dataclasses
import json
import logging
import sys
from datetime import date, datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[2]))

from src.fetch.lineups    import fetch_confirmed_lineup
from src.fetch.handedness import fetch_handedness
from src.fetch.batter_stats import fetch_batter_stats

logger = logging.getLogger(__name__)

DATA_DIR = Path(__file__).parents[2] / "data"

# Check window: only poll for confirmed lineup within this range before first pitch
_MIN_MINUTES = 15
_MAX_MINUTES = 120


def _now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _minutes_until(first_pitch_utc: str) -> float:
    """Return minutes between now and first pitch (negative if past)."""
    fp = datetime.fromisoformat(first_pitch_utc.replace("Z", "+00:00"))
    return (fp - datetime.now(timezone.utc)).total_seconds() / 60


def update_lineups(date_str: str) -> None:
    """
    Check and update lineup statuses for the given date's snapshot.

    Args:
        date_str: Date string YYYY-MM-DD.
    """
    json_path = DATA_DIR / f"{date_str}.json"
    if not json_path.exists():
        logger.error("No snapshot found for %s — run snapshot.py first.", date_str)
        return

    data = json.loads(json_path.read_text())
    changed = False

    # ------------------------------------------------------------------
    # Pass 1: freeze any games where first pitch has already passed
    # ------------------------------------------------------------------
    for game in data["games"]:
        if game["lineup_status"] == "frozen":
            continue
        mins = _minutes_until(game["first_pitch_utc"])
        if mins < 0:
            logger.info(
                "Freezing %s @ %s (game_pk=%d) — first pitch has passed.",
                game["away_team"], game["home_team"], game["game_pk"],
            )
            game["lineup_status"] = "frozen"
            changed = True

    # ------------------------------------------------------------------
    # Pass 2: find projected games in the check window
    # ------------------------------------------------------------------
    games_to_check = [
        g for g in data["games"]
        if g["lineup_status"] == "projected"
        and _MIN_MINUTES <= _minutes_until(g["first_pitch_utc"]) <= _MAX_MINUTES
    ]

    if not games_to_check:
        logger.info("No games in the T-%d to T-%d window.", _MAX_MINUTES, _MIN_MINUTES)
        if changed:
            data["last_updated"] = _now_utc()
            json_path.write_text(json.dumps(data, indent=2))
            logger.info("Wrote frozen status updates to %s", json_path)
        return

    logger.info("%d game(s) in check window — fetching batter stats...", len(games_to_check))

    # ------------------------------------------------------------------
    # Load batter stats once for enrichment (cached by pybaseball)
    # ------------------------------------------------------------------
    current_year = int(date_str[:4])
    prior_year   = current_year - 1

    try:
        batter_cur = fetch_batter_stats(current_year)
    except Exception as e:
        logger.error("batter_stats current year failed: %s — xwOBA will be None", e)
        batter_cur = {}

    try:
        batter_pri = fetch_batter_stats(prior_year)
    except Exception as e:
        logger.error("batter_stats prior year failed: %s — xwOBA will be None", e)
        batter_pri = {}

    # ------------------------------------------------------------------
    # Pass 3: check each game for confirmed lineup
    # ------------------------------------------------------------------
    for game in games_to_check:
        game_pk   = game["game_pk"]
        away_team = game["away_team"]
        home_team = game["home_team"]
        mins      = _minutes_until(game["first_pitch_utc"])

        logger.info(
            "Checking %s @ %s (game_pk=%d, T-%.0f min)...",
            away_team, home_team, game_pk, mins,
        )

        confirmed = None
        try:
            confirmed = fetch_confirmed_lineup(game_pk)
        except Exception as e:
            logger.error("fetch_confirmed_lineup(%d) failed: %s", game_pk, e)

        # Always update last_checked timestamp
        game["lineup_last_checked"] = _now_utc()
        changed = True

        if not confirmed:
            logger.info("  → Not yet confirmed.")
            continue

        # --- Enrich confirmed lineup with handedness + xwOBA ---
        all_ids = [
            b["mlbam_id"]
            for side in ("away", "home")
            for b in confirmed.get(side, [])
            if b.get("mlbam_id")
        ]

        try:
            handedness = fetch_handedness(all_ids)
        except Exception as e:
            logger.error("handedness lookup failed for game_pk=%d: %s", game_pk, e)
            handedness = {}

        for side in ("away", "home"):
            batters = []
            for b in confirmed.get(side, []):
                mlbam = b.get("mlbam_id")
                h     = handedness.get(mlbam, {}) if mlbam else {}
                cur   = batter_cur.get(mlbam) if mlbam else None
                pri   = batter_pri.get(mlbam) if mlbam else None

                batters.append({
                    "order":        b["order"],
                    "name":         b["name"],
                    "mlbam_id":     mlbam,
                    "bats":         h.get("bats"),
                    "current_year": dataclasses.asdict(cur) if cur else None,
                    "prior_year":   dataclasses.asdict(pri) if pri else None,
                })
            game["lineups"][side] = batters

        game["lineup_status"] = "confirmed"
        logger.info(
            "  ✓ Confirmed lineup set for %s @ %s.", away_team, home_team
        )

    # ------------------------------------------------------------------
    # Write JSON only if something changed
    # ------------------------------------------------------------------
    if changed:
        data["last_updated"] = _now_utc()
        json_path.write_text(json.dumps(data, indent=2))
        logger.info("Wrote updated snapshot to %s", json_path)
    else:
        logger.info("No changes — JSON not rewritten.")


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    date_str = sys.argv[1] if len(sys.argv) > 1 else date.today().strftime("%Y-%m-%d")
    update_lineups(date_str)
