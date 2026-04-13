"""
Fetcher: home plate umpire + K% tendency for each game.

Sources:
  - HP umpire name/ID : MLB Stats API (game officials, hydrate='officials')
  - K% stats          : UmpScorecards API (umpscorecards.com)

UmpScorecards tracks called-strike tendency vs league average, expressed as
a K% above/below average and an overall accuracy score.
"""

import logging
import sys
from pathlib import Path

import requests
import statsapi

logger = logging.getLogger(__name__)

_UC_HEADERS = {
    "User-Agent": "Mozilla/5.0",
    "Accept": "application/json",
}
_UC_API = "https://umpscorecards.com/api/umpires"


# ── UmpScorecards stats ────────────────────────────────────────────────────────

def fetch_umpire_stats() -> dict[str, dict]:
    """
    Fetch per-umpire stats from UmpScorecards.
    Returns {umpire_name_lower: {k_pct, k_pct_vs_avg, accuracy, games, name}}
    """
    try:
        resp = requests.get(_UC_API, headers=_UC_HEADERS, timeout=15)
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        logger.warning("UmpScorecards fetch failed: %s", exc)
        return {}

    result: dict[str, dict] = {}

    # API returns {"rows": [...]}
    umpires = data.get("rows") or (data if isinstance(data, list) else [])

    for u in umpires:
        name = u.get("umpire") or u.get("name") or u.get("fullName") or ""
        if not name:
            continue

        # Compute accuracy from raw counts
        called   = u.get("called_pitches_sum") or 0
        correct  = u.get("called_correct_sum") or 0
        accuracy = round(correct / called * 100, 1) if called > 0 else None

        # Run impact: total_run_impact_mean
        # Positive = incorrect calls added runs (hitter-friendly zone)
        # Negative = incorrect calls removed runs (pitcher-friendly zone)
        run_impact = u.get("total_run_impact_mean")
        if run_impact is not None:
            run_impact = round(float(run_impact), 2)

        # Above-expected accuracy
        above_x = u.get("correct_calls_above_x_sum")
        if above_x is not None:
            above_x = round(float(above_x), 1)

        games = u.get("n")

        result[name.lower()] = {
            "name":       name,
            "accuracy":   accuracy,
            "run_impact": run_impact,   # + = hitter-friendly, - = pitcher-friendly
            "above_x":    above_x,      # calls above expected accuracy
            "games":      int(games) if games is not None else None,
        }

    logger.info("UmpScorecards: %d umpires loaded", len(result))
    return result


# ── HP umpire from MLB API ─────────────────────────────────────────────────────

def fetch_game_hp_umpire(game_pk: int) -> dict | None:
    """
    Return HP umpire info for a game from the MLB Stats API.

    Returns:
        {"id": str, "name": str} or None if not available.
    """
    try:
        data = statsapi.get("game", {"gamePk": game_pk, "hydrate": "officials"})
        officials = (
            data.get("liveData", {})
                .get("boxscore", {})
                .get("officials", [])
        )
        for o in officials:
            if o.get("officialType", "").lower() == "home plate":
                official = o.get("official", {})
                return {
                    "id":   str(official.get("id", "")),
                    "name": official.get("fullName", ""),
                }
    except Exception as exc:
        logger.warning("fetch_game_hp_umpire(%s): %s", game_pk, exc)
    return None


# ── Combined: umpire + stats for a game ───────────────────────────────────────

def fetch_game_umpire(game_pk: int, umpire_stats: dict[str, dict]) -> dict | None:
    """
    Get HP umpire + K% stats for a specific game.

    Args:
        game_pk:       MLB game PK.
        umpire_stats:  Pre-fetched dict from fetch_umpire_stats().

    Returns:
        {
          "name": str,
          "k_pct": float | None,
          "k_vs_avg": float | None,   # positive = more Ks than avg, negative = fewer
          "accuracy": float | None,
          "games": int | None,
        }
    """
    ump = fetch_game_hp_umpire(game_pk)
    if not ump or not ump.get("name"):
        return None

    name = ump["name"]
    stats = umpire_stats.get(name.lower(), {})

    return {
        "name":       name,
        "accuracy":   stats.get("accuracy"),
        "run_impact": stats.get("run_impact"),
        "above_x":    stats.get("above_x"),
        "games":      stats.get("games"),
    }


# ── Standalone test ────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import json
    from datetime import date

    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

    try:
        from src.fetch.schedule import fetch_schedule
    except ImportError:
        sys.path.insert(0, str(Path(__file__).parents[2]))
        from src.fetch.schedule import fetch_schedule

    today = date.today().strftime("%Y-%m-%d")
    # ── Debug: check raw UmpScorecards response ──────────────────────────────
    print(f"\nProbing UmpScorecards endpoints...")
    endpoints = [
        "https://umpscorecards.com/api/umpires",
        "https://umpscorecards.com/api/umpires/2026",
        "https://umpscorecards.com/api/leaderboard",
        "https://umpscorecards.com/api/v1/umpires",
    ]
    for url in endpoints:
        try:
            r = requests.get(url, headers=_UC_HEADERS, timeout=10)
            print(f"  {url}")
            print(f"    status={r.status_code}  content-type={r.headers.get('content-type','?')}")
            if r.status_code == 200:
                text = r.text[:300]
                print(f"    body preview: {text}")
            break
        except Exception as e:
            print(f"  {url}  ERROR: {e}")

    print()
    ump_stats = fetch_umpire_stats()
    print(f"ump_stats entries: {len(ump_stats)}")
    if ump_stats:
        sample = list(ump_stats.values())[:5]
        for u in sample:
            print(f"  {u['name']:<25}  accuracy={u['accuracy']}%  "
                  f"run_impact={u['run_impact']}  games={u['games']}")
    else:
        print("  No umpire stats returned.")

    print(f"\nFetching HP umpires for today's games ({today})...")
    games = fetch_schedule(today)
    for g in games[:3]:
        pk = g["game_pk"]
        ump = fetch_game_umpire(pk, ump_stats)
        if ump:
            print(f"  {g['away_team']} @ {g['home_team']:4s}  |  "
                  f"HP: {ump['name']:<25}  accuracy={ump['accuracy']}%  "
                  f"run_impact={ump['run_impact']}")
        else:
            print(f"  {g['away_team']} @ {g['home_team']:4s}  |  HP umpire: not available")
