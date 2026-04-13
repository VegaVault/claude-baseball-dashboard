"""
Fetcher: today's MLB schedule via MLB Stats API.

Returns a list of game stubs (game_pk, teams, first_pitch_utc, status,
probable pitchers) that seed the daily snapshot.
"""

import logging
from datetime import datetime, timezone

import statsapi

logger = logging.getLogger(__name__)

# MLB team ID → 3-letter abbreviation (2026 season)
TEAM_ID_TO_ABBR = {
    108: "LAA", 109: "ARI", 110: "BAL", 111: "BOS", 112: "CHC",
    113: "CIN", 114: "CLE", 115: "COL", 116: "DET", 117: "HOU",
    118: "KC",  119: "LAD", 120: "WSH", 121: "NYM", 133: "ATH",
    134: "PIT", 135: "SD",  136: "SEA", 137: "SF",  138: "STL",
    139: "TB",  140: "TEX", 141: "TOR", 142: "MIN", 143: "PHI",
    144: "ATL", 145: "CWS", 146: "MIA", 147: "NYY", 158: "MIL",
}

# MLB Stats API status strings → our schema values
STATUS_MAP = {
    "Scheduled":        "scheduled",
    "Pre-Game":         "scheduled",
    "Warmup":           "scheduled",
    "In Progress":      "in_progress",
    "Live":             "in_progress",
    "Final":            "final",
    "Game Over":        "final",
    "Completed Early":  "final",
    "Postponed":        "scheduled",
    "Cancelled":        "scheduled",
}


def fetch_schedule(date: str) -> list[dict]:
    """
    Fetch today's schedule from the MLB Stats API.

    Args:
        date: Date string in YYYY-MM-DD format.

    Returns:
        List of game stub dicts with keys:
            game_pk, status, first_pitch_utc,
            away_team, home_team, final_score,
            away_pitcher (dict|None), home_pitcher (dict|None)
    """
    raw_games = statsapi.schedule(date=date)
    games = []

    for g in raw_games:
        # Skip non-regular-season games
        if g.get("game_type") not in ("R", "S", "F", "D", "L", "W"):
            continue

        status = STATUS_MAP.get(g.get("status", ""), "scheduled")

        away_abbr = TEAM_ID_TO_ABBR.get(g.get("away_id"), g.get("away_name", "UNK")[:3].upper())
        home_abbr = TEAM_ID_TO_ABBR.get(g.get("home_id"), g.get("home_name", "UNK")[:3].upper())

        # game_datetime from statsapi is already UTC ISO 8601
        first_pitch_utc = g.get("game_datetime", "")

        # Final score
        final_score = None
        if status == "final":
            away_score = g.get("away_score", "")
            home_score = g.get("home_score", "")
            if away_score != "" and home_score != "":
                final_score = f"{away_score}-{home_score}"

        # Probable pitchers (name + mlbam_id)
        away_pitcher = None
        if g.get("away_probable_pitcher"):
            away_pitcher = {
                "name": g["away_probable_pitcher"],
                "mlbam_id": str(g.get("away_pitcher_id", "")),
            }

        home_pitcher = None
        if g.get("home_probable_pitcher"):
            home_pitcher = {
                "name": g["home_probable_pitcher"],
                "mlbam_id": str(g.get("home_pitcher_id", "")),
            }

        games.append({
            "game_pk":        g["game_id"],
            "status":         status,
            "first_pitch_utc": first_pitch_utc,
            "away_team":      away_abbr,
            "home_team":      home_abbr,
            "away_team_id":   g.get("away_id"),
            "home_team_id":   g.get("home_id"),
            "final_score":    final_score,
            "away_pitcher":   away_pitcher,
            "home_pitcher":   home_pitcher,
        })

    logger.info("fetch_schedule: found %d games for %s", len(games), date)
    return games


if __name__ == "__main__":
    import json
    from datetime import date

    today = date.today().strftime("%Y-%m-%d")
    print(f"Fetching schedule for {today}...\n")
    games = fetch_schedule(today)

    if not games:
        print("No games found.")
    else:
        for g in games:
            away_p = g["away_pitcher"]["name"] if g["away_pitcher"] else "TBD"
            home_p = g["home_pitcher"]["name"] if g["home_pitcher"] else "TBD"
            print(
                f"  {g['away_team']} @ {g['home_team']}"
                f"  |  {g['first_pitch_utc']}"
                f"  |  status={g['status']}"
                f"  |  {away_p} vs {home_p}"
                f"  |  game_pk={g['game_pk']}"
            )
