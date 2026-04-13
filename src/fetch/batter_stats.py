"""
Fetcher: batter season stats.

- xwOBA and PA: pybaseball.statcast_batter_expected_stats() (Baseball Savant)

Returns a dict keyed by MLBAM ID string.
"""

import logging

import pandas as pd
import pybaseball

try:
    from src.models import BatterSeasonStats
except ModuleNotFoundError:
    import sys, os
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
    from src.models import BatterSeasonStats

logger = logging.getLogger(__name__)

pybaseball.cache.enable()


def fetch_batter_stats(year: int) -> dict[str, BatterSeasonStats]:
    """
    Fetch batter season stats for the given year.

    Args:
        year: Season year (e.g. 2025).

    Returns:
        Dict mapping mlbam_id -> BatterSeasonStats.
    """
    df = pybaseball.statcast_batter_expected_stats(year, minPA=10)
    df.columns = [c.strip() for c in df.columns]

    id_col = next((c for c in df.columns if c.lower() in ("player_id", "playerid")), None)
    xwoba_col = next(
        (c for c in df.columns if c.lower() in ("est_woba", "xwoba", "est_woba_used")),
        None,
    )
    pa_col = next((c for c in df.columns if c.lower() in ("pa", "attempts")), None)

    if not id_col:
        logger.error("Cannot find player_id column. Available: %s", df.columns.tolist())
        return {}
    if not xwoba_col:
        logger.error("Cannot find xwOBA column. Available: %s", df.columns.tolist())
        return {}
    if not pa_col:
        logger.warning("Cannot find PA column. Available: %s", df.columns.tolist())

    result = {}
    for _, row in df.iterrows():
        mlbam = str(int(row[id_col]))
        xwoba = float(row[xwoba_col]) if pd.notna(row[xwoba_col]) else None
        pa = int(row[pa_col]) if pa_col and pd.notna(row[pa_col]) else None
        result[mlbam] = BatterSeasonStats(pa=pa, xwoba=xwoba)

    logger.info("Savant batter stats: %d batters for %d", len(result), year)
    return result


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    year = int(sys.argv[1]) if len(sys.argv) > 1 else 2025

    print(f"\nFetching batter stats for {year}...\n")
    stats = fetch_batter_stats(year)
    print(f"Total batters: {len(stats)}\n")

    # Spot check: Judge=592450, Freeman=518692, Ohtani=660271, Betts=605141
    spot_check = {
        "592450": "Aaron Judge",
        "518692": "Freddie Freeman",
        "660271": "Shohei Ohtani",
        "605141": "Mookie Betts",
    }
    for mlbam, name in spot_check.items():
        s = stats.get(mlbam)
        if s:
            print(f"  {name:20s}  PA={s.pa}  xwOBA={s.xwoba}")
        else:
            print(f"  {name:20s}  NOT FOUND")
