"""
Fetcher: last-15-games W/L record + runs-per-game averages for a team.

Uses the MLB Stats API schedule endpoint. One call per team covers the full
season, giving us both season-long and L15 run averages in one pass.
"""

import logging
import sys
from datetime import date, timedelta
from pathlib import Path

import statsapi

logger = logging.getLogger(__name__)


def _runs(game: dict, team_id: int) -> tuple[int, int]:
    """Return (runs_scored, runs_allowed) for team_id in a completed game."""
    if game.get("home_id") == team_id:
        return (game.get("home_score") or 0), (game.get("away_score") or 0)
    return (game.get("away_score") or 0), (game.get("home_score") or 0)


def _team_won(game: dict, team_id: int) -> bool:
    scored, allowed = _runs(game, team_id)
    return scored > allowed


def fetch_team_form(team_id: int, as_of_date: str) -> dict:
    """
    Return W/L record and run averages for a team.

    Looks back from as_of_date to Jan 1 of the same year (covers full season).
    L15 = the 15 most recently completed games within that window.

    Returns:
        {
          "wins": int, "losses": int, "games": int, "streak": str,
          "season_rpg": float,   # runs scored per game, full season
          "season_rapg": float,  # runs allowed per game, full season
          "l15_rpg": float,      # runs scored per game, last 15
          "l15_rapg": float,     # runs allowed per game, last 15
        }
    """
    end_dt   = date.fromisoformat(as_of_date) - timedelta(days=1)
    start_dt = date(end_dt.year, 1, 1)   # Jan 1 = full season

    try:
        raw = statsapi.schedule(
            team=team_id,
            start_date=start_dt.strftime("%m/%d/%Y"),
            end_date=end_dt.strftime("%m/%d/%Y"),
        )
    except Exception as exc:
        logger.warning("fetch_team_form(%s): %s", team_id, exc)
        return _empty()

    final_games = [g for g in raw if g.get("status") == "Final"]

    if not final_games:
        return _empty()

    # Season averages (all completed games)
    season_rs = [_runs(g, team_id)[0] for g in final_games]
    season_ra = [_runs(g, team_id)[1] for g in final_games]
    season_rpg  = round(sum(season_rs) / len(season_rs), 2)
    season_rapg = round(sum(season_ra) / len(season_ra), 2)

    # L15
    l15 = final_games[-15:]
    results = ["W" if _team_won(g, team_id) else "L" for g in l15]
    wins    = results.count("W")
    losses  = results.count("L")

    l15_rs = [_runs(g, team_id)[0] for g in l15]
    l15_ra = [_runs(g, team_id)[1] for g in l15]
    l15_rpg  = round(sum(l15_rs) / len(l15_rs), 2)
    l15_rapg = round(sum(l15_ra) / len(l15_ra), 2)

    # Streak
    streak_char = results[-1]
    streak_len  = sum(1 for _ in
                      (r for r in reversed(results) if r == streak_char)
                      .__iter__())
    # simpler streak count:
    streak_len = 0
    for r in reversed(results):
        if r == streak_char:
            streak_len += 1
        else:
            break
    streak = f"{streak_char}{streak_len}"

    return {
        "wins":        wins,
        "losses":      losses,
        "games":       len(l15),
        "streak":      streak,
        "season_rpg":  season_rpg,
        "season_rapg": season_rapg,
        "l15_rpg":     l15_rpg,
        "l15_rapg":    l15_rapg,
    }


def _empty() -> dict:
    return {
        "wins": None, "losses": None, "games": 0, "streak": "",
        "season_rpg": None, "season_rapg": None,
        "l15_rpg": None,    "l15_rapg": None,
    }


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

    try:
        from src.fetch.schedule import TEAM_ID_TO_ABBR
    except ImportError:
        sys.path.insert(0, str(Path(__file__).parents[2]))
        from src.fetch.schedule import TEAM_ID_TO_ABBR

    today = date.today().strftime("%Y-%m-%d")
    print(f"Fetching team form as of {today}...\n")
    print(f"{'Team':<6} {'L15':>7} {'Streak':>7} {'S RPG':>7} {'S RAPG':>7} {'L15 RPG':>8} {'L15 RAPG':>9}")
    print("-" * 58)

    sample = {147: "NYY", 111: "BOS", 119: "LAD", 117: "HOU"}
    for tid, abbr in sample.items():
        f = fetch_team_form(tid, today)
        l15_str = f"{f['wins']}-{f['losses']}" if f['wins'] is not None else "—"
        print(
            f"  {abbr:<4} {l15_str:>7} {f['streak']:>7}"
            f"  {str(f['season_rpg']):>6}  {str(f['season_rapg']):>6}"
            f"  {str(f['l15_rpg']):>7}  {str(f['l15_rapg']):>8}"
        )
