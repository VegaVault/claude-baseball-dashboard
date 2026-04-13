"""
Fetcher: probable starting pitchers.

Primary  : MLB Stats API via statsapi.schedule() — official announcements.
           Reliable but sometimes lags 12–24h behind actual announcements.

Enhancement: FanGraphs probables grid (roster-resource) — earlier announcements.
             Best-effort only; gracefully returns {} on failure (blocked, etc.)

Returns dict: game_pk -> {"away": {name, mlbam_id} | None,
                           "home": {name, mlbam_id} | None}
"""

import logging
import re

import requests
import statsapi

logger = logging.getLogger(__name__)

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Referer": "https://www.fangraphs.com/",
}


# ---------------------------------------------------------------------------
# Primary: MLB Stats API
# ---------------------------------------------------------------------------

def _lookup_pitcher_id(name: str) -> str | None:
    """Look up a pitcher's MLBAM ID by full name via MLB Stats API."""
    try:
        results = statsapi.lookup_player(name)
        if results:
            # Prefer active pitchers; take first match if multiple
            for p in results:
                if p.get("primaryPosition", {}).get("code") == "1":
                    return str(p["id"])
            return str(results[0]["id"])
    except Exception as e:
        logger.warning("lookup_player failed for '%s': %s", name, e)
    return None


def fetch_probables_mlbapi(date: str) -> dict[int, dict]:
    """
    Fetch probable pitchers from MLB Stats API for the given date.

    Args:
        date: Date string YYYY-MM-DD.

    Returns:
        Dict mapping game_pk -> {
            "away": {"name": str, "mlbam_id": str} | None,
            "home": {"name": str, "mlbam_id": str} | None,
        }
    """
    games = statsapi.schedule(date=date)
    result = {}

    for g in games:
        if g.get("game_type") not in ("R", "S", "F", "D", "L", "W"):
            continue

        def resolve(name_key: str, id_key: str) -> dict | None:
            name = g.get(name_key, "").strip()
            if not name:
                return None
            mlbam_id = str(g[id_key]) if g.get(id_key) else _lookup_pitcher_id(name)
            if not mlbam_id:
                logger.warning("Could not resolve MLBAM ID for pitcher '%s'", name)
                return {"name": name, "mlbam_id": None}
            return {"name": name, "mlbam_id": mlbam_id}

        result[g["game_id"]] = {
            "away": resolve("away_probable_pitcher", "away_pitcher_id"),
            "home": resolve("home_probable_pitcher", "home_pitcher_id"),
        }

    logger.info("MLB API probables: %d games for %s", len(result), date)
    return result


# ---------------------------------------------------------------------------
# Enhancement: FanGraphs probables grid
# ---------------------------------------------------------------------------

def fetch_probables_fangraphs(date: str) -> dict[str, dict]:
    """
    Scrape FanGraphs probables grid for early-announced starters.
    Returns empty dict on any failure — caller logs and continues.

    Args:
        date: Date string YYYY-MM-DD (used for the grid URL).

    Returns:
        Dict mapping team abbreviation -> {"name": str, "hand": str | None}.
        Empty dict if scrape fails.
    """
    # FanGraphs uses MM/DD/YYYY in the URL
    try:
        year, month, day = date.split("-")
        fg_date = f"{month}/{day}/{year}"
    except ValueError:
        logger.warning("Invalid date format for FanGraphs scrape: %s", date)
        return {}

    url = f"https://www.fangraphs.com/roster-resource/probables-grid?date={fg_date}"
    try:
        session = requests.Session()
        session.get("https://www.fangraphs.com/", headers=_HEADERS, timeout=10)
        resp = session.get(url, headers=_HEADERS, timeout=20)
        resp.raise_for_status()
    except Exception as e:
        logger.warning("FanGraphs probables grid unavailable (%s) — using MLB API only", e)
        return {}

    # Parse JSON embedded in the page: window.Probables = {...}
    match = re.search(r"window\.Probables\s*=\s*(\{.*?\});", resp.text, re.DOTALL)
    if not match:
        logger.warning("FanGraphs probables JSON not found in page — page structure may have changed")
        return {}

    import json
    try:
        data = json.loads(match.group(1))
    except json.JSONDecodeError as e:
        logger.warning("FanGraphs probables JSON parse failed: %s", e)
        return {}

    result = {}
    for team_abbr, info in data.items():
        name = info.get("Name") or info.get("name")
        hand = info.get("Hand") or info.get("hand")
        if name:
            result[team_abbr.upper()] = {"name": name, "hand": hand}

    logger.info("FanGraphs probables: %d teams found", len(result))
    return result


# ---------------------------------------------------------------------------
# Merged: MLB API + FanGraphs overlay
# ---------------------------------------------------------------------------

def fetch_probables(date: str) -> dict[int, dict]:
    """
    Get probable pitchers for all games on the given date.
    Starts with the MLB Stats API (reliable), overlays FanGraphs where it
    has data and the MLB API shows None (early announcement enhancement).

    Args:
        date: Date string YYYY-MM-DD.

    Returns:
        Dict mapping game_pk -> {"away": {...}|None, "home": {...}|None}
    """
    mlb = fetch_probables_mlbapi(date)

    fg = {}
    try:
        fg = fetch_probables_fangraphs(date)
    except Exception as e:
        logger.error("FanGraphs probables failed: %s", e)

    # FanGraphs overlay: only fill in slots where MLB API has None
    # We can't map FG team abbr → game_pk reliably here, so this is
    # handled in the snapshot builder where we have both pieces.
    # For now, attach the FG data as a side-channel under key "fg_overlay".
    return {"by_game": mlb, "fg_overlay": fg}


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

    from datetime import date
    date_str = date.today().strftime("%Y-%m-%d")
    print(f"\nFetching probable pitchers for {date_str}...\n")

    result = fetch_probables(date_str)
    games  = result["by_game"]
    fg     = result["fg_overlay"]

    print(f"MLB API: {len(games)} games\n")
    for game_pk, pitchers in games.items():
        away = pitchers["away"]
        home = pitchers["home"]
        away_str = f"{away['name']} ({away['mlbam_id']})" if away else "TBD"
        home_str = f"{home['name']} ({home['mlbam_id']})" if home else "TBD"
        print(f"  game_pk={game_pk}  {away_str}  vs  {home_str}")

    if fg:
        print(f"\nFanGraphs overlay: {len(fg)} teams")
        for abbr, info in list(fg.items())[:5]:
            print(f"  {abbr}: {info['name']} ({info.get('hand', '?')})")
    else:
        print("\nFanGraphs overlay: unavailable (expected — site blocks scrapers)")
