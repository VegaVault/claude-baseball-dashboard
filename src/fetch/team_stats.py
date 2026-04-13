"""
Fetcher: team-level stats from Baseball Savant leaderboards.

- Team xwOBA (offense):        Savant expected stats, per-team requests, PA-weighted avg
- Team xwOBA-against (pitching): same endpoint for pitchers
- Team OAA (defense):          Savant team OAA leaderboard (already aggregated)

Computes 1-based ranks across all 30 teams (1 = best).
"""

import logging
import time
from io import StringIO

import requests
import pandas as pd

try:
    from src.models import TeamRanks
except ModuleNotFoundError:
    import sys, os
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
    from src.models import TeamRanks

logger = logging.getLogger(__name__)

_HEADERS = {"User-Agent": "Mozilla/5.0"}

# MLB team ID → 3-letter abbreviation (must match schedule.py)
TEAM_ID_TO_ABBR = {
    108: "LAA", 109: "ARI", 110: "BAL", 111: "BOS", 112: "CHC",
    113: "CIN", 114: "CLE", 115: "COL", 116: "DET", 117: "HOU",
    118: "KC",  119: "LAD", 120: "WSH", 121: "NYM", 133: "ATH",
    134: "PIT", 135: "SD",  136: "SEA", 137: "SF",  138: "STL",
    139: "TB",  140: "TEX", 141: "TOR", 142: "MIN", 143: "PHI",
    144: "ATL", 145: "CWS", 146: "MIA", 147: "NYY", 158: "MIL",
}


def _savant_expected_stats(year: int, stat_type: str, team_id: int) -> pd.DataFrame:
    """
    Fetch Savant expected stats for one team.
    stat_type: 'batter' or 'pitcher'
    """
    url = (
        f"https://baseballsavant.mlb.com/leaderboard/expected_statistics"
        f"?type={stat_type}&year={year}&position=&team={team_id}&min=0&csv=true"
    )
    resp = requests.get(url, headers=_HEADERS, timeout=20)
    resp.raise_for_status()
    return pd.read_csv(StringIO(resp.text))


def _pa_weighted_xwoba(df: pd.DataFrame) -> float | None:
    """Compute PA-weighted average xwOBA from a Savant expected stats DataFrame."""
    df = df.dropna(subset=["est_woba", "pa"])
    if df.empty or df["pa"].sum() == 0:
        return None
    return round(float((df["est_woba"] * df["pa"]).sum() / df["pa"].sum()), 3)


def _fetch_team_xwoba(year: int, stat_type: str) -> dict[str, float]:
    """
    Fetch PA-weighted xwOBA for all 30 teams.
    stat_type: 'batter' (offense) or 'pitcher' (pitching staff xwOBA-against)
    Returns dict: team_abbr -> xwoba
    """
    result = {}
    for team_id, abbr in TEAM_ID_TO_ABBR.items():
        try:
            df = _savant_expected_stats(year, stat_type, team_id)
            xwoba = _pa_weighted_xwoba(df)
            if xwoba is not None:
                result[abbr] = xwoba
            time.sleep(0.2)  # be polite to Savant
        except Exception as e:
            logger.warning("Savant %s xwOBA failed for %s (%d): %s", stat_type, abbr, team_id, e)
    return result


def _fetch_team_oaa(year: int) -> dict[str, int]:
    """
    Fetch team OAA from Savant team fielding leaderboard.
    Returns dict: team_abbr -> oaa (total across all positions)
    """
    url = (
        f"https://baseballsavant.mlb.com/leaderboard/outs_above_average"
        f"?type=Fielding_Team&year={year}&team=&min=0&csv=true"
    )
    resp = requests.get(url, headers=_HEADERS, timeout=30)
    resp.raise_for_status()
    df = pd.read_csv(StringIO(resp.text))

    # Map team_id (int) → abbr, sum OAA across all position rows per team
    result = {}
    for _, row in df.iterrows():
        tid = int(row["team_id"])
        abbr = TEAM_ID_TO_ABBR.get(tid)
        if not abbr:
            continue
        oaa = int(row["outs_above_average"]) if pd.notna(row["outs_above_average"]) else 0
        result[abbr] = result.get(abbr, 0) + oaa
    return result


def _rank(data: dict[str, float], higher_is_better: bool = True) -> dict[str, int]:
    """
    Convert raw values to 1-based ranks (1 = best).
    Ties get the same rank.
    """
    sorted_items = sorted(data.items(), key=lambda x: x[1], reverse=higher_is_better)
    ranks = {}
    for i, (abbr, _) in enumerate(sorted_items, start=1):
        ranks[abbr] = i
    return ranks


def fetch_team_stats(year: int) -> dict[str, TeamRanks]:
    """
    Fetch team stats and compute ranks for the given year.

    Args:
        year: Season year (e.g. 2025).

    Returns:
        Dict mapping 3-letter team abbreviation -> TeamRanks.
    """
    # --- Offense: team xwOBA ---
    hitting_xwoba: dict[str, float] = {}
    try:
        logger.info("Fetching team hitting xwOBA (30 requests)...")
        hitting_xwoba = _fetch_team_xwoba(year, "batter")
        logger.info("Team hitting xwOBA: %d teams", len(hitting_xwoba))
    except Exception as e:
        logger.error("Team hitting xwOBA failed: %s", e)

    # --- Pitching: team xwOBA-against ---
    pitching_xwoba: dict[str, float] = {}
    try:
        logger.info("Fetching team pitching xwOBA-against (30 requests)...")
        pitching_xwoba = _fetch_team_xwoba(year, "pitcher")
        logger.info("Team pitching xwOBA-against: %d teams", len(pitching_xwoba))
    except Exception as e:
        logger.error("Team pitching xwOBA failed: %s", e)

    # --- Defense: team OAA ---
    team_oaa: dict[str, int] = {}
    try:
        team_oaa = _fetch_team_oaa(year)
        logger.info("Team OAA: %d teams", len(team_oaa))
    except Exception as e:
        logger.error("Team OAA failed: %s", e)

    # --- Compute ranks ---
    hitting_ranks  = _rank(hitting_xwoba,  higher_is_better=True)   # higher xwOBA = better offense
    pitching_ranks = _rank(pitching_xwoba, higher_is_better=False)  # lower xwOBA-against = better pitching
    oaa_ranks      = _rank(team_oaa,       higher_is_better=True)   # higher OAA = better defense

    # --- Merge into TeamRanks ---
    all_abbrs = set(TEAM_ID_TO_ABBR.values())
    return {
        abbr: TeamRanks(
            hitting_xwoba_rank=hitting_ranks.get(abbr),
            pitching_xwoba_against_rank=pitching_ranks.get(abbr),
            defense_oaa_rank=oaa_ranks.get(abbr),
        )
        for abbr in all_abbrs
    }


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    year = int(sys.argv[1]) if len(sys.argv) > 1 else 2025

    print(f"\nFetching team stats for {year}...")
    print("(Will make ~60 requests to Savant — takes ~30 seconds)\n")

    ranks = fetch_team_stats(year)
    print(f"\nTotal teams: {len(ranks)}\n")

    # Show a handful of teams sorted by hitting rank
    print(f"{'Team':<6} {'Hit xwOBA Rank':>14} {'Pit xwOBA Rank':>14} {'OAA Rank':>9}")
    print("-" * 48)
    sorted_teams = sorted(ranks.items(), key=lambda x: x[1].hitting_xwoba_rank or 99)
    for abbr, r in sorted_teams[:10]:
        print(
            f"{abbr:<6} {str(r.hitting_xwoba_rank):>14} "
            f"{str(r.pitching_xwoba_against_rank):>14} "
            f"{str(r.defense_oaa_rank):>9}"
        )
