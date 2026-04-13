"""
Fetcher: batter season stats.

- xwOBA and PA : Baseball Savant via pybaseball
- Percentiles + decile labels computed across the full player pool
- Qualified flag: PA >= QUALIFY_PA (default 50)

Returns a dict keyed by MLBAM ID string.
"""

import logging

import pandas as pd
import pybaseball

try:
    from src.models import BatterSeasonStats
    from src.fetch.labels import percentile_to_label, compute_percentiles
except ModuleNotFoundError:
    import sys, os
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
    from src.models import BatterSeasonStats
    from src.fetch.labels import percentile_to_label, compute_percentiles

logger = logging.getLogger(__name__)

pybaseball.cache.enable()

# PA threshold for "qualified" flag — prorated: 3.1 PA/game × ~16 games ≈ 50
QUALIFY_PA = 50


def fetch_batter_stats(year: int) -> dict[str, BatterSeasonStats]:
    """
    Fetch batter season stats for the given year.

    Args:
        year: Season year (e.g. 2025).

    Returns:
        Dict mapping mlbam_id -> BatterSeasonStats (with percentiles + labels).
    """
    df = pybaseball.statcast_batter_expected_stats(year, minPA=10)
    df.columns = [c.strip() for c in df.columns]

    id_col    = next((c for c in df.columns if c.lower() in ("player_id", "playerid")), None)
    xwoba_col = next((c for c in df.columns if c.lower() in ("est_woba", "xwoba", "est_woba_used")), None)
    pa_col    = next((c for c in df.columns if c.lower() in ("pa", "attempts")), None)

    if not id_col or not xwoba_col:
        logger.error("Missing columns in Savant batter data. Available: %s", df.columns.tolist())
        return {}

    df = df.dropna(subset=[id_col, xwoba_col])

    # Compute percentiles across full pool (higher xwOBA = better)
    pcts = compute_percentiles(df[xwoba_col].tolist(), higher_is_better=True)

    result = {}
    for i, (_, row) in enumerate(df.iterrows()):
        mlbam     = str(int(row[id_col]))
        xwoba     = float(row[xwoba_col])
        pa        = int(row[pa_col]) if pa_col and pd.notna(row[pa_col]) else None
        pct       = pcts[i]
        qualified = (pa is not None and pa >= QUALIFY_PA)

        result[mlbam] = BatterSeasonStats(
            pa=pa,
            xwoba=xwoba,
            xwoba_percentile=pct,
            xwoba_label=percentile_to_label(pct),
            qualified=qualified,
        )

    logger.info("Savant batter stats: %d batters for %d", len(result), year)
    return result


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    year = int(sys.argv[1]) if len(sys.argv) > 1 else 2025

    print(f"\nFetching batter stats for {year}...\n")
    stats = fetch_batter_stats(year)
    print(f"Total batters: {len(stats)}\n")

    spot_check = {
        "592450": "Aaron Judge",
        "518692": "Freddie Freeman",
        "660271": "Shohei Ohtani",
        "605141": "Mookie Betts",
    }
    for mlbam, name in spot_check.items():
        s = stats.get(mlbam)
        if s:
            flag = "" if s.qualified else " ⚠"
            print(f"  {name:20s}  PA={s.pa}  xwOBA={s.xwoba}  "
                  f"pct={s.xwoba_percentile}  label={s.xwoba_label}{flag}")
        else:
            print(f"  {name:20s}  NOT FOUND")
