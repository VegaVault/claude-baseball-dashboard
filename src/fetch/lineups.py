"""
Fetcher: game lineups via MLB Stats API.

- Confirmed lineups : statsapi.boxscore_data(game_pk) — posted ~60 min pre-game
- Projected lineups : most recent prior game's batting order for each team

Returns raw lineup dicts {order, name, mlbam_id} — the snapshot builder
enriches them with handedness and xwOBA from the other fetchers.
"""

import logging
from datetime import date, timedelta

import statsapi

logger = logging.getLogger(__name__)

# How many days back to search for a team's last lineup
_MAX_LOOKBACK_DAYS = 7


def _extract_lineup_from_boxscore(boxscore: dict, side: str) -> list[dict] | None:
    """
    Pull batting order from a boxscore_data response for one side ('away'/'home').
    Returns None if lineup is not yet posted.
    """
    team = boxscore.get(side, {})
    batter_ids = team.get("batters", [])

    if not batter_ids:
        return None

    players = team.get("players", {})
    lineup = []

    for player_id in batter_ids:
        key = f"ID{player_id}"
        player = players.get(key, {})
        batting_order_str = player.get("battingOrder", "")

        # battingOrder is like '100', '200' ... '900'; pitchers are '0' or ''
        try:
            order = int(batting_order_str) // 100
        except (ValueError, TypeError):
            continue
        if order < 1 or order > 9:
            continue

        name = player.get("person", {}).get("fullName", "")
        lineup.append({
            "order":     order,
            "name":      name,
            "mlbam_id":  str(player_id),
        })

    if not lineup:
        return None

    return sorted(lineup, key=lambda x: x["order"])


def fetch_confirmed_lineup(game_pk: int) -> dict[str, list[dict]] | None:
    """
    Attempt to fetch the confirmed lineup for a game.

    Args:
        game_pk: MLB Stats API game primary key.

    Returns:
        {"away": [...], "home": [...]} if lineup is confirmed, else None.
    """
    try:
        boxscore = statsapi.boxscore_data(game_pk)
    except Exception as e:
        logger.warning("boxscore_data(%d) failed: %s", game_pk, e)
        return None

    away = _extract_lineup_from_boxscore(boxscore, "away")
    home = _extract_lineup_from_boxscore(boxscore, "home")

    if not away or not home:
        return None

    return {"away": away, "home": home}


def _find_last_game_pk(team_id: int, before_date: date) -> int | None:
    """
    Search backwards up to _MAX_LOOKBACK_DAYS to find the most recent
    completed game for a team. Returns game_pk or None.
    """
    for days_back in range(1, _MAX_LOOKBACK_DAYS + 1):
        check_date = before_date - timedelta(days=days_back)
        try:
            games = statsapi.schedule(
                team=team_id,
                date=check_date.strftime("%Y-%m-%d"),
            )
        except Exception as e:
            logger.warning("schedule lookup failed for team %d on %s: %s", team_id, check_date, e)
            continue

        for g in games:
            if g.get("status") in ("Final", "Game Over", "Completed Early"):
                return g["game_id"]

    return None


def fetch_projected_lineup(
    away_team_id: int,
    home_team_id: int,
    today: date,
) -> dict[str, list[dict]]:
    """
    Build projected lineups from each team's most recent prior game.

    Args:
        away_team_id: MLB Stats API team ID for the away team.
        home_team_id: MLB Stats API team ID for the home team.
        today: The date of the game being projected (to search backwards from).

    Returns:
        {"away": [...], "home": [...]} — may be empty lists if no prior game found.
    """
    result = {"away": [], "home": []}

    for side, team_id in (("away", away_team_id), ("home", home_team_id)):
        last_pk = _find_last_game_pk(team_id, today)
        if not last_pk:
            logger.warning("No recent game found for team_id=%d — projected lineup empty", team_id)
            continue

        try:
            boxscore = statsapi.boxscore_data(last_pk)
        except Exception as e:
            logger.warning("boxscore_data(%d) failed for projection: %s", last_pk, e)
            continue

        # The team could be either side in their last game — check both
        for check_side in ("away", "home"):
            team_info = boxscore.get(check_side, {}).get("team", {})
            if team_info.get("id") == team_id:
                lineup = _extract_lineup_from_boxscore(boxscore, check_side)
                if lineup:
                    result[side] = lineup
                break

    return result


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

    today = date.today()
    date_str = today.strftime("%Y-%m-%d")
    print(f"\nFetching today's schedule ({date_str}) to find a test game...\n")

    games = statsapi.schedule(date=date_str)
    if not games:
        print("No games today.")
        sys.exit(0)

    # Test with the first game of the day
    g = games[0]
    game_pk   = g["game_id"]
    away_name = g.get("away_name", "Away")
    home_name = g.get("home_name", "Home")
    away_id   = g.get("away_id")
    home_id   = g.get("home_id")

    print(f"Game: {away_name} @ {home_name}  (game_pk={game_pk})\n")

    # Try confirmed
    print("--- Confirmed lineup ---")
    confirmed = fetch_confirmed_lineup(game_pk)
    if confirmed:
        for side in ("away", "home"):
            label = away_name if side == "away" else home_name
            print(f"\n  {label}:")
            for b in confirmed[side]:
                print(f"    {b['order']}. {b['name']:30s} id={b['mlbam_id']}")
    else:
        print("  Not yet posted.\n")

    # Try projected
    print("\n--- Projected lineup (from last game) ---")
    projected = fetch_projected_lineup(away_id, home_id, today)
    for side in ("away", "home"):
        label = away_name if side == "away" else home_name
        lineup = projected[side]
        if lineup:
            print(f"\n  {label} (projected):")
            for b in lineup:
                print(f"    {b['order']}. {b['name']:30s} id={b['mlbam_id']}")
        else:
            print(f"\n  {label}: no projection available")
