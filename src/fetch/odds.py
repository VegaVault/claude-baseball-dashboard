"""
Fetcher: MLB betting odds from The Odds API (the-odds-api.com).

Free tier: 500 requests/month.
Returns consensus odds (averaged across bookmakers) per game:
  - Moneyline (h2h): favorite team + american odds for both sides
  - Run line (spreads): typically ±1.5
  - Over/Under (totals)

Matches games to our schedule by full team name → abbreviation.

Requires env var: ODDS_API_KEY
"""

import logging
import os

import requests

logger = logging.getLogger(__name__)

_BASE_URL = "https://api.the-odds-api.com/v4/sports/baseball_mlb/odds/"

# Full team name → our 3-letter abbreviation
_NAME_TO_ABBR: dict[str, str] = {
    "Arizona Diamondbacks":    "ARI",
    "Atlanta Braves":          "ATL",
    "Baltimore Orioles":       "BAL",
    "Boston Red Sox":          "BOS",
    "Chicago Cubs":            "CHC",
    "Chicago White Sox":       "CWS",
    "Cincinnati Reds":         "CIN",
    "Cleveland Guardians":     "CLE",
    "Colorado Rockies":        "COL",
    "Detroit Tigers":          "DET",
    "Houston Astros":          "HOU",
    "Kansas City Royals":      "KC",
    "Los Angeles Angels":      "LAA",
    "Los Angeles Dodgers":     "LAD",
    "Miami Marlins":           "MIA",
    "Milwaukee Brewers":       "MIL",
    "Minnesota Twins":         "MIN",
    "New York Mets":           "NYM",
    "New York Yankees":        "NYY",
    "Oakland Athletics":       "ATH",
    "Athletics":               "ATH",
    "Philadelphia Phillies":   "PHI",
    "Pittsburgh Pirates":      "PIT",
    "San Diego Padres":        "SD",
    "San Francisco Giants":    "SF",
    "Seattle Mariners":        "SEA",
    "St. Louis Cardinals":     "STL",
    "Tampa Bay Rays":          "TB",
    "Texas Rangers":           "TEX",
    "Toronto Blue Jays":       "TOR",
    "Washington Nationals":    "WSH",
}

# Preferred bookmakers in priority order for display
_PREFERRED_BOOKS = ["draftkings", "fanduel", "betmgm", "caesars", "bovada"]


def _abbr(name: str) -> str | None:
    return _NAME_TO_ABBR.get(name)


def _american_to_implied(odds: int) -> float:
    """Convert american odds to implied probability (0-1)."""
    if odds > 0:
        return 100 / (odds + 100)
    else:
        return abs(odds) / (abs(odds) + 100)


def _consensus_moneyline(bookmakers: list[dict], away_name: str, home_name: str) -> dict | None:
    """Average implied probabilities across books, convert back to american odds."""
    away_probs, home_probs = [], []

    for book in bookmakers:
        for market in book.get("markets", []):
            if market["key"] != "h2h":
                continue
            for outcome in market.get("outcomes", []):
                price = outcome.get("price")
                if price is None:
                    continue
                if outcome["name"] == away_name:
                    away_probs.append(_american_to_implied(price))
                elif outcome["name"] == home_name:
                    home_probs.append(_american_to_implied(price))

    if not away_probs or not home_probs:
        return None

    away_impl = sum(away_probs) / len(away_probs)
    home_impl = sum(home_probs) / len(home_probs)

    # Normalize to remove vig
    total = away_impl + home_impl
    away_fair = away_impl / total
    home_fair = home_impl / total

    def _to_american(p: float) -> int:
        if p >= 0.5:
            return round(-(p / (1 - p)) * 100)
        else:
            return round(((1 - p) / p) * 100)

    away_odds = _to_american(away_fair)
    home_odds = _to_american(home_fair)

    favorite = "away" if away_fair > home_fair else "home"
    return {
        "away_ml":   away_odds,
        "home_ml":   home_odds,
        "favorite":  favorite,
        "away_impl": round(away_fair * 100, 1),
        "home_impl": round(home_fair * 100, 1),
    }


def _consensus_runline(bookmakers: list[dict], away_name: str, home_name: str) -> dict | None:
    """Get most common run line point + averaged odds."""
    away_data, home_data = [], []

    for book in bookmakers:
        for market in book.get("markets", []):
            if market["key"] != "spreads":
                continue
            for outcome in market.get("outcomes", []):
                price = outcome.get("price")
                point = outcome.get("point")
                if price is None or point is None:
                    continue
                if outcome["name"] == away_name:
                    away_data.append((point, price))
                elif outcome["name"] == home_name:
                    home_data.append((point, price))

    if not away_data or not home_data:
        return None

    away_point = away_data[0][0]  # typically +1.5 or -1.5
    home_point = home_data[0][0]
    away_odds  = round(sum(p for _, p in away_data) / len(away_data))
    home_odds  = round(sum(p for _, p in home_data) / len(home_data))

    return {
        "away_point": away_point,
        "away_odds":  away_odds,
        "home_point": home_point,
        "home_odds":  home_odds,
    }


def _consensus_total(bookmakers: list[dict]) -> dict | None:
    """Average over/under line and odds."""
    over_data, under_data = [], []

    for book in bookmakers:
        for market in book.get("markets", []):
            if market["key"] != "totals":
                continue
            for outcome in market.get("outcomes", []):
                price = outcome.get("price")
                point = outcome.get("point")
                if price is None or point is None:
                    continue
                if outcome["name"] == "Over":
                    over_data.append((point, price))
                elif outcome["name"] == "Under":
                    under_data.append((point, price))

    if not over_data:
        return None

    line  = round(sum(p for p, _ in over_data) / len(over_data) * 2) / 2  # round to .5
    o_odds = round(sum(p for _, p in over_data)  / len(over_data))
    u_odds = round(sum(p for _, p in under_data) / len(under_data)) if under_data else None

    return {
        "line":        line,
        "over_odds":   o_odds,
        "under_odds":  u_odds,
    }


def fetch_odds() -> dict[str, dict]:
    """
    Fetch today's MLB odds from The Odds API.

    Returns:
        {
          "NYY_BOS": {          # key = "{away}_{home}"
            "away":  "NYY",
            "home":  "BOS",
            "moneyline": {
              "away_ml":   -145,
              "home_ml":   +125,
              "favorite":  "away",
              "away_impl": 55.2,   # % chance
              "home_impl": 44.8,
            },
            "runline": {
              "away_point": -1.5,
              "away_odds":  +155,
              "home_point": +1.5,
              "home_odds":  -175,
            },
            "total": {
              "line":       8.5,
              "over_odds":  -110,
              "under_odds": -110,
            },
            "books_used": 4,
          }
        }
    """
    api_key = os.getenv("ODDS_API_KEY")
    if not api_key:
        logger.warning("ODDS_API_KEY not set — skipping odds fetch")
        return {}

    try:
        resp = requests.get(
            _BASE_URL,
            params={
                "apiKey":      api_key,
                "regions":     "us",
                "markets":     "h2h,spreads,totals",
                "oddsFormat":  "american",
                "dateFormat":  "iso",
            },
            timeout=15,
        )
        resp.raise_for_status()
        games = resp.json()
        remaining = resp.headers.get("x-requests-remaining", "?")
        logger.info("Odds API: %d games fetched  (%s requests remaining)", len(games), remaining)
    except Exception as exc:
        logger.error("Odds API fetch failed: %s", exc)
        return {}

    result: dict[str, dict] = {}

    for game in games:
        away_name = game.get("away_team", "")
        home_name = game.get("home_team", "")
        away_abbr = _abbr(away_name)
        home_abbr = _abbr(home_name)

        if not away_abbr or not home_abbr:
            logger.warning("Unknown team name(s): %r / %r", away_name, home_name)
            continue

        bookmakers  = game.get("bookmakers", [])
        books_used  = len(bookmakers)

        moneyline = _consensus_moneyline(bookmakers, away_name, home_name)
        runline   = _consensus_runline(bookmakers, away_name, home_name)
        total     = _consensus_total(bookmakers)

        key = f"{away_abbr}_{home_abbr}"
        result[key] = {
            "away":      away_abbr,
            "home":      home_abbr,
            "moneyline": moneyline,
            "runline":   runline,
            "total":     total,
            "books_used": books_used,
        }

    logger.info("Odds: %d games processed", len(result))
    return result


def fmt_ml(odds: int | None) -> str:
    """Format american moneyline for display: +125 / -145"""
    if odds is None:
        return "—"
    return f"+{odds}" if odds > 0 else str(odds)


def fmt_ou(total: dict | None) -> str:
    """Format O/U for display: O/U 8.5"""
    if not total:
        return "—"
    line = total.get("line")
    return f"O/U {line}" if line else "—"


if __name__ == "__main__":
    import logging
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

    odds = fetch_odds()
    print(f"\n{len(odds)} games with odds:\n")
    for key, g in sorted(odds.items()):
        ml  = g.get("moneyline") or {}
        rl  = g.get("runline")   or {}
        tot = g.get("total")     or {}
        fav = g.get("away") if ml.get("favorite") == "away" else g.get("home")
        print(f"  {g['away']:>4} @ {g['home']:<4}  "
              f"ML: {fmt_ml(ml.get('away_ml'))} / {fmt_ml(ml.get('home_ml'))}  "
              f"RL: {rl.get('away_point','—')} / {rl.get('home_point','—')}  "
              f"{fmt_ou(tot)}  "
              f"({g['books_used']} books)  fav={fav}")
