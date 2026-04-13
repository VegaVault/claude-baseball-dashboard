"""
Snapshot builder: orchestrates all fetchers into a daily JSON snapshot.

Writes to data/YYYY-MM-DD.json. Idempotent — re-running replaces the file.
Partial failures are caught, logged, and recorded in fetch_errors.
Per CLAUDE.md rule #3: never swallow errors silently.
"""

import dataclasses
import json
import logging
import sys
import os
from datetime import date, datetime, timezone
from pathlib import Path

# Allow running as script directly
sys.path.insert(0, str(Path(__file__).parents[2]))

from src.fetch.schedule   import fetch_schedule
from src.fetch.probables  import fetch_probables_mlbapi
from src.fetch.lineups    import fetch_confirmed_lineup, fetch_projected_lineup
from src.fetch.handedness import fetch_handedness
from src.fetch.pitcher_stats import fetch_pitcher_stats
from src.fetch.batter_stats  import fetch_batter_stats
from src.fetch.team_stats    import fetch_team_stats
from src.models import (
    Batter, BatterSeasonStats,
    DailySnapshot, Game,
    Pitcher, PitcherSeasonStats,
    TeamRanks,
)

logger = logging.getLogger(__name__)

DATA_DIR = Path(__file__).parents[2] / "data"


def _now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _safe_fetch(label: str, fn, fetch_errors: list, default):
    """Call fn(), appending to fetch_errors on exception. Returns default on failure."""
    try:
        return fn()
    except Exception as e:
        msg = f"{label}: {e}"
        logger.error(msg)
        fetch_errors.append(msg)
        return default


def build_snapshot(date_str: str) -> None:
    """
    Run all fetchers and write data/{date_str}.json.

    Args:
        date_str: Date string YYYY-MM-DD.
    """
    logger.info("=== Building snapshot for %s ===", date_str)
    fetch_errors: list[str] = []

    current_year = int(date_str[:4])
    prior_year   = current_year - 1
    today        = date.fromisoformat(date_str)

    # ------------------------------------------------------------------
    # 1. Schedule — must succeed for anything to work
    # ------------------------------------------------------------------
    schedule_games = fetch_schedule(date_str)
    if not schedule_games:
        logger.warning("No games found for %s — writing empty snapshot.", date_str)

    # ------------------------------------------------------------------
    # 2. Probables (better ID resolution than raw schedule)
    # ------------------------------------------------------------------
    probables = _safe_fetch(
        "probables", lambda: fetch_probables_mlbapi(date_str), fetch_errors, {}
    )

    # ------------------------------------------------------------------
    # 3. Season stats — fetch once each, shared across all games
    # ------------------------------------------------------------------
    logger.info("Fetching pitcher stats (current + prior year)...")
    pitcher_cur = _safe_fetch(
        f"pitcher_stats:{current_year}",
        lambda: fetch_pitcher_stats(current_year), fetch_errors, {}
    )
    pitcher_pri = _safe_fetch(
        f"pitcher_stats:{prior_year}",
        lambda: fetch_pitcher_stats(prior_year), fetch_errors, {}
    )

    logger.info("Fetching batter stats (current + prior year)...")
    batter_cur = _safe_fetch(
        f"batter_stats:{current_year}",
        lambda: fetch_batter_stats(current_year), fetch_errors, {}
    )
    batter_pri = _safe_fetch(
        f"batter_stats:{prior_year}",
        lambda: fetch_batter_stats(prior_year), fetch_errors, {}
    )

    logger.info("Fetching team stats...")
    team_stats = _safe_fetch(
        "team_stats",
        lambda: fetch_team_stats(current_year), fetch_errors, {}
    )

    # ------------------------------------------------------------------
    # 4. Per-game lineups — collect all player IDs for batch handedness
    # ------------------------------------------------------------------
    logger.info("Fetching lineups for %d games...", len(schedule_games))
    game_lineup_map: dict[int, tuple[dict, str]] = {}  # game_pk -> (lineup, status)
    all_player_ids: set[str] = set()

    for sg in schedule_games:
        game_pk = sg["game_pk"]

        # Skip games already in progress / final — freeze lineup
        if sg["status"] in ("in_progress", "final"):
            confirmed = _safe_fetch(
                f"lineup:confirmed:{game_pk}",
                lambda pk=game_pk: fetch_confirmed_lineup(pk),
                fetch_errors, None
            )
            lineup = confirmed or {"away": [], "home": []}
            status = "frozen"
        else:
            confirmed = _safe_fetch(
                f"lineup:confirmed:{game_pk}",
                lambda pk=game_pk: fetch_confirmed_lineup(pk),
                fetch_errors, None
            )
            if confirmed:
                lineup = confirmed
                status = "confirmed"
            else:
                lineup = _safe_fetch(
                    f"lineup:projected:{game_pk}",
                    lambda sg=sg: fetch_projected_lineup(
                        sg["away_team_id"], sg["home_team_id"], today
                    ),
                    fetch_errors, {"away": [], "home": []}
                )
                status = "projected"

        game_lineup_map[game_pk] = (lineup, status)

        for side in ("away", "home"):
            for b in lineup.get(side, []):
                if b.get("mlbam_id"):
                    all_player_ids.add(b["mlbam_id"])

        # Add pitcher IDs from probables
        for side in ("away", "home"):
            p = probables.get(game_pk, {}).get(side)
            if p and p.get("mlbam_id"):
                all_player_ids.add(p["mlbam_id"])

    # ------------------------------------------------------------------
    # 5. Batch handedness lookup for all players
    # ------------------------------------------------------------------
    logger.info("Fetching handedness for %d players...", len(all_player_ids))
    handedness = _safe_fetch(
        "handedness",
        lambda: fetch_handedness(list(all_player_ids)),
        fetch_errors, {}
    )

    # ------------------------------------------------------------------
    # 6. Assemble Game objects
    # ------------------------------------------------------------------
    games: list[Game] = []

    for sg in schedule_games:
        game_pk = sg["game_pk"]
        lineup, lineup_status = game_lineup_map.get(game_pk, ({"away": [], "home": []}, "projected"))
        prob = probables.get(game_pk, {})

        # --- Pitchers ---
        pitchers: dict = {}
        for side in ("away", "home"):
            p_info = prob.get(side) or sg.get(f"{side}_pitcher")
            if not p_info or not p_info.get("name"):
                pitchers[side] = None
                continue

            mlbam = p_info.get("mlbam_id")
            h     = handedness.get(mlbam, {}) if mlbam else {}

            def _ps(stats_dict, mid):
                s = stats_dict.get(mid)
                return dataclasses.asdict(s) if s else None

            pitchers[side] = {
                "name":     p_info["name"],
                "mlbam_id": mlbam,
                "throws":   h.get("throws"),
                "current_year": _ps(pitcher_cur, mlbam),
                "prior_year":   _ps(pitcher_pri, mlbam),
            }

        # --- Lineups ---
        lineups_out: dict = {}
        for side in ("away", "home"):
            batters = []
            for b in lineup.get(side, []):
                mlbam = b.get("mlbam_id")
                h     = handedness.get(mlbam, {}) if mlbam else {}

                def _bs(stats_dict, mid):
                    s = stats_dict.get(mid)
                    return dataclasses.asdict(s) if s else None

                batters.append({
                    "order":        b["order"],
                    "name":         b["name"],
                    "mlbam_id":     mlbam,
                    "bats":         h.get("bats"),
                    "current_year": _bs(batter_cur, mlbam),
                    "prior_year":   _bs(batter_pri, mlbam),
                })
            lineups_out[side] = batters

        # --- Team ranks ---
        team_ranks_out: dict = {}
        for side, abbr in (("away", sg["away_team"]), ("home", sg["home_team"])):
            tr = team_stats.get(abbr)
            team_ranks_out[side] = dataclasses.asdict(tr) if tr else None

        games.append({
            "game_pk":            game_pk,
            "status":             sg["status"],
            "first_pitch_utc":    sg["first_pitch_utc"],
            "away_team":          sg["away_team"],
            "home_team":          sg["home_team"],
            "final_score":        sg.get("final_score"),
            "lineup_status":      lineup_status,
            "lineup_last_checked": _now_utc(),
            "pitchers":           pitchers,
            "lineups":            lineups_out,
            "team_ranks":         team_ranks_out,
        })

    # ------------------------------------------------------------------
    # 7. Write JSON
    # ------------------------------------------------------------------
    snapshot = {
        "date":         date_str,
        "last_updated": _now_utc(),
        "fetch_errors": fetch_errors,
        "games":        games,
    }

    DATA_DIR.mkdir(exist_ok=True)
    out_path = DATA_DIR / f"{date_str}.json"
    out_path.write_text(json.dumps(snapshot, indent=2))
    logger.info("Wrote %s  (%d games, %d fetch errors)", out_path, len(games), len(fetch_errors))

    if fetch_errors:
        logger.warning("Fetch errors:\n  %s", "\n  ".join(fetch_errors))


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    date_str = sys.argv[1] if len(sys.argv) > 1 else date.today().strftime("%Y-%m-%d")
    build_snapshot(date_str)
