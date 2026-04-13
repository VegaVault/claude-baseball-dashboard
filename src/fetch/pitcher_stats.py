"""
Fetcher: pitcher season stats.

- IP, FIP, ERA+ : Baseball Reference standard pitching page (scraped directly)
                  + pybaseball.pitching_stats_bref() for mlbID crosswalk
- xwOBA allowed : pybaseball.statcast_pitcher_expected_stats() (Savant)

Returns a dict keyed by MLBAM ID string.
"""

import logging
import re
from io import StringIO

import requests
import pandas as pd
import pybaseball

try:
    from src.models import PitcherSeasonStats
    from src.fetch.labels import percentile_to_label, compute_percentiles
except ModuleNotFoundError:
    import sys, os
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
    from src.models import PitcherSeasonStats
    from src.fetch.labels import percentile_to_label, compute_percentiles

logger = logging.getLogger(__name__)

pybaseball.cache.enable()

_BREF_HEADERS = {"User-Agent": "Mozilla/5.0"}


def _clean_name(name: str) -> str:
    """Strip switch/lefty markers and whitespace for fuzzy joining."""
    return re.sub(r"[*#]", "", str(name)).strip().lower()


# ---------------------------------------------------------------------------
# IP + FIP + ERA+ — Baseball Reference standard pitching page
# ---------------------------------------------------------------------------

def _fetch_bref_standard(year: int) -> dict[str, dict]:
    """
    Scrape bref standard pitching page for IP, FIP, ERA+.
    Returns dict: cleaned_name -> {ip, fip, era_plus}
    """
    url = f"https://www.baseball-reference.com/leagues/majors/{year}-standard-pitching.shtml"
    resp = requests.get(url, headers=_BREF_HEADERS, timeout=30)
    resp.raise_for_status()

    # bref hides some tables in HTML comments — uncomment them
    html = re.sub(r"<!--\s*((<table)[\s\S]*?(</table>))\s*-->", r"\1", resp.text)
    tables = pd.read_html(StringIO(html))

    # Table 1 is the player-level standard pitching table
    df = tables[1]
    df = df[df["Player"] != "Player"].dropna(subset=["Player"])  # drop header rows

    out = {}
    for _, row in df.iterrows():
        name = _clean_name(row["Player"])
        try:
            ip = float(row["IP"]) if pd.notna(row.get("IP")) else None
        except (ValueError, TypeError):
            ip = None
        try:
            fip = float(row["FIP"]) if pd.notna(row.get("FIP")) else None
        except (ValueError, TypeError):
            fip = None
        try:
            era_plus = int(float(row["ERA+"])) if pd.notna(row.get("ERA+")) else None
        except (ValueError, TypeError):
            era_plus = None
        out[name] = {"ip": ip, "fip": fip, "era_plus": era_plus}
    return out


def _fetch_bref_mlbid(year: int) -> dict[str, str]:
    """
    Use pybaseball.pitching_stats_bref() to get cleaned_name -> mlbam_id mapping.
    """
    df = pybaseball.pitching_stats_bref(year)
    df = df.dropna(subset=["mlbID"])
    df["mlbID"] = df["mlbID"].astype(int).astype(str)
    return {_clean_name(row["Name"]): row["mlbID"] for _, row in df.iterrows()}


# ---------------------------------------------------------------------------
# xwOBA — Baseball Savant
# ---------------------------------------------------------------------------

def _fetch_xwoba_savant(year: int) -> dict[str, float]:
    """
    Fetch pitcher xwOBA-against from Baseball Savant expected stats.
    Returns dict: mlbam_id -> xwoba float.
    """
    df = pybaseball.statcast_pitcher_expected_stats(year, minPA=10)
    df.columns = [c.strip() for c in df.columns]

    id_col = next((c for c in df.columns if c.lower() in ("player_id", "playerid")), None)
    xwoba_col = next(
        (c for c in df.columns if c.lower() in ("est_woba", "xwoba", "est_woba_used")),
        None,
    )
    if not id_col or not xwoba_col:
        logger.error("Missing columns in Savant pitcher data. Available: %s", df.columns.tolist())
        return {}

    out = {}
    for _, row in df.iterrows():
        pid = str(int(row[id_col]))
        val = row[xwoba_col]
        out[pid] = float(val) if pd.notna(val) else None
    return out


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def fetch_pitcher_stats(year: int) -> dict[str, PitcherSeasonStats]:
    """
    Fetch pitcher season stats for the given year.

    Args:
        year: Season year (e.g. 2025).

    Returns:
        Dict mapping mlbam_id -> PitcherSeasonStats.
    """
    # --- bref: IP, FIP, ERA+ (by name) ---
    bref_stats: dict[str, dict] = {}
    try:
        bref_stats = _fetch_bref_standard(year)
        logger.info("bref standard: %d pitchers for %d", len(bref_stats), year)
    except Exception as e:
        logger.error("bref standard pitching failed for %d: %s", year, e)

    # --- bref: name -> mlbam_id crosswalk ---
    name_to_mlbam: dict[str, str] = {}
    try:
        name_to_mlbam = _fetch_bref_mlbid(year)
        logger.info("bref mlbID crosswalk: %d entries for %d", len(name_to_mlbam), year)
    except Exception as e:
        logger.error("bref mlbID crosswalk failed for %d: %s", year, e)

    # --- Savant: xwOBA (by mlbam_id) ---
    xwoba_data: dict[str, float] = {}
    try:
        xwoba_data = _fetch_xwoba_savant(year)
        logger.info("Savant xwOBA: %d pitchers for %d", len(xwoba_data), year)
    except Exception as e:
        logger.error("Savant xwOBA failed for %d: %s", year, e)

    # --- Merge into flat dict first ---
    merged: dict[str, dict] = {}

    for name, stats in bref_stats.items():
        mlbam = name_to_mlbam.get(name)
        if not mlbam:
            continue
        merged[mlbam] = {
            "ip":    stats.get("ip"),
            "fip":   stats.get("fip"),
            "era_plus": stats.get("era_plus"),
            "xwoba": xwoba_data.get(mlbam),
        }

    for mlbam, xwoba in xwoba_data.items():
        if mlbam not in merged:
            merged[mlbam] = {"ip": None, "fip": None, "era_plus": None, "xwoba": xwoba}

    # --- Compute percentiles across full pool ---
    # xwOBA-against: lower is better → invert
    ids      = list(merged.keys())
    xwobas   = [merged[m]["xwoba"] for m in ids]
    fips     = [merged[m]["fip"]   for m in ids]

    # Only rank pitchers that have the stat
    xwoba_valid = [(i, v) for i, v in enumerate(xwobas) if v is not None]
    fip_valid   = [(i, v) for i, v in enumerate(fips)   if v is not None]

    xwoba_pcts: dict[int, int] = {}
    if xwoba_valid:
        idxs, vals = zip(*xwoba_valid)
        for idx, pct in zip(idxs, compute_percentiles(list(vals), higher_is_better=False)):
            xwoba_pcts[idx] = pct

    fip_pcts: dict[int, int] = {}
    if fip_valid:
        idxs, vals = zip(*fip_valid)
        for idx, pct in zip(idxs, compute_percentiles(list(vals), higher_is_better=False)):
            fip_pcts[idx] = pct

    # IP threshold for "qualified" (prorated: ~1 IP/game × 16 games ≈ 20 IP)
    QUALIFY_IP = 20

    result: dict[str, PitcherSeasonStats] = {}
    for i, mlbam in enumerate(ids):
        m = merged[mlbam]
        ip = m["ip"]
        xp = xwoba_pcts.get(i)
        fp = fip_pcts.get(i)
        result[mlbam] = PitcherSeasonStats(
            ip=ip,
            fip=m["fip"],
            era_plus=m["era_plus"],
            xwoba=m["xwoba"],
            xwoba_percentile=xp,
            xwoba_label=percentile_to_label(xp) if xp is not None else None,
            fip_percentile=fp,
            fip_label=percentile_to_label(fp) if fp is not None else None,
            qualified=(ip is not None and ip >= QUALIFY_IP),
        )

    return result


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    year = int(sys.argv[1]) if len(sys.argv) > 1 else 2025

    print(f"\nFetching pitcher stats for {year}...\n")
    stats = fetch_pitcher_stats(year)
    print(f"\nTotal pitchers: {len(stats)}\n")

    spot_check = {
        "543037": "Gerrit Cole",
        "554430": "Zack Wheeler",
        "675911": "Spencer Strider",
    }
    for mlbam, name in spot_check.items():
        s = stats.get(mlbam)
        if s:
            print(f"  {name:20s}  IP={s.ip}  FIP={s.fip}  ERA+={s.era_plus}  xwOBA={s.xwoba}")
        else:
            print(f"  {name:20s}  NOT FOUND")
