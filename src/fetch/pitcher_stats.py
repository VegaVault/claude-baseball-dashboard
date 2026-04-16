"""
Fetcher: pitcher season stats.

Sources:
  IP, mlbam_id : pybaseball.pitching_stats_bref()  — works in CI, has mlbID
  FIP, ERA+    : bref standard pitching page (direct scrape) — best-effort,
                 may fail on GitHub Actions (bref blocks cloud IPs)
  xwOBA        : pybaseball.statcast_pitcher_expected_stats() (Savant)

Returns a dict keyed by MLBAM ID string.
"""

import logging
import re
from io import StringIO

import pandas as pd
import pybaseball
import requests

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


# ---------------------------------------------------------------------------
# pybaseball bref: IP + mlbam_id  (reliable in CI)
# ---------------------------------------------------------------------------

def _fetch_bref_pybaseball(year: int) -> dict[str, dict]:
    """
    Use pybaseball.pitching_stats_bref() for IP and mlbam_id.
    Returns {mlbam_id: {ip, gs}}.
    """
    df = pybaseball.pitching_stats_bref(year)
    df = df.dropna(subset=["mlbID"])
    df["mlbID"] = df["mlbID"].astype(int).astype(str)

    out = {}
    for _, row in df.iterrows():
        mlbam = row["mlbID"]
        try:
            ip = float(row["IP"]) if pd.notna(row.get("IP")) else None
        except (ValueError, TypeError):
            ip = None
        try:
            gs = int(float(row["GS"])) if pd.notna(row.get("GS")) else None
        except (ValueError, TypeError):
            gs = None
        out[mlbam] = {"ip": ip, "gs": gs}

    return out


# ---------------------------------------------------------------------------
# bref direct scrape: FIP + ERA+  (best-effort — may fail on cloud runners)
# ---------------------------------------------------------------------------

def _clean_name(name: str) -> str:
    return re.sub(r"[*#]", "", str(name)).strip().lower()


def _fetch_bref_fip(year: int) -> dict[str, dict]:
    """
    Scrape bref standard pitching page for FIP and ERA+.
    Returns {cleaned_name: {fip, era_plus}}.
    Falls back to empty dict if bref blocks the request.
    """
    url = f"https://www.baseball-reference.com/leagues/majors/{year}-standard-pitching.shtml"
    resp = requests.get(url, headers=_BREF_HEADERS, timeout=30)
    resp.raise_for_status()

    html   = re.sub(r"<!--\s*((<table)[\s\S]*?(</table>))\s*-->", r"\1", resp.text)
    tables = pd.read_html(StringIO(html))
    df     = tables[1]
    df     = df[df["Player"] != "Player"].dropna(subset=["Player"])

    out = {}
    for _, row in df.iterrows():
        name = _clean_name(row["Player"])
        try:
            fip = float(row["FIP"]) if pd.notna(row.get("FIP")) else None
        except (ValueError, TypeError):
            fip = None
        try:
            era_plus = int(float(row["ERA+"])) if pd.notna(row.get("ERA+")) else None
        except (ValueError, TypeError):
            era_plus = None
        out[name] = {"fip": fip, "era_plus": era_plus}

    return out


def _fetch_bref_name_to_mlbam(year: int) -> dict[str, str]:
    """cleaned_name → mlbam_id from pybaseball bref crosswalk."""
    df = pybaseball.pitching_stats_bref(year)
    df = df.dropna(subset=["mlbID"])
    df["mlbID"] = df["mlbID"].astype(int).astype(str)
    return {_clean_name(row["Name"]): row["mlbID"] for _, row in df.iterrows()}


# ---------------------------------------------------------------------------
# Savant: xwOBA against
# ---------------------------------------------------------------------------

def _fetch_xwoba_savant(year: int) -> dict[str, float]:
    df = pybaseball.statcast_pitcher_expected_stats(year, minPA=10)
    df.columns = [c.strip() for c in df.columns]
    id_col    = next((c for c in df.columns if c.lower() in ("player_id", "playerid")), None)
    xwoba_col = next(
        (c for c in df.columns if c.lower() in ("est_woba", "xwoba", "est_woba_used")), None
    )
    if not id_col or not xwoba_col:
        logger.error("Missing columns in Savant pitcher data: %s", df.columns.tolist())
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

def fetch_pitcher_stats(year: int) -> dict[str, "PitcherSeasonStats"]:
    """
    Fetch pitcher season stats for the given year.
    Returns dict mapping mlbam_id -> PitcherSeasonStats.
    """
    # --- pybaseball bref: IP + mlbam_id (always works) ---
    bref_pb: dict[str, dict] = {}
    try:
        bref_pb = _fetch_bref_pybaseball(year)
        logger.info("pybaseball bref: %d pitchers for %d", len(bref_pb), year)
    except Exception as e:
        logger.error("pybaseball bref failed for %d: %s", year, e)

    # --- bref direct scrape: FIP + ERA+ (best-effort) ---
    fip_data: dict[str, dict]  = {}   # cleaned_name → {fip, era_plus}
    name_to_mlbam: dict[str, str] = {}
    try:
        fip_data      = _fetch_bref_fip(year)
        name_to_mlbam = _fetch_bref_name_to_mlbam(year)
        logger.info("bref FIP/ERA+: %d pitchers for %d", len(fip_data), year)
    except Exception as e:
        logger.warning("bref FIP/ERA+ scrape failed for %d (will show — in dashboard): %s", year, e)

    # Build mlbam → {fip, era_plus} mapping
    fip_by_mlbam: dict[str, dict] = {}
    for name, stats in fip_data.items():
        mlbam = name_to_mlbam.get(name)
        if mlbam:
            fip_by_mlbam[mlbam] = stats

    # --- Savant: xwOBA (always works) ---
    xwoba_data: dict[str, float] = {}
    try:
        xwoba_data = _fetch_xwoba_savant(year)
        logger.info("Savant xwOBA: %d pitchers for %d", len(xwoba_data), year)
    except Exception as e:
        logger.error("Savant xwOBA failed for %d: %s", year, e)

    # --- Merge: union of all known mlbam IDs ---
    all_ids = set(bref_pb.keys()) | set(fip_by_mlbam.keys()) | set(xwoba_data.keys())
    merged: dict[str, dict] = {}
    for mlbam in all_ids:
        pb   = bref_pb.get(mlbam, {})
        fip  = fip_by_mlbam.get(mlbam, {})
        merged[mlbam] = {
            "ip":       pb.get("ip"),
            "fip":      fip.get("fip"),
            "era_plus": fip.get("era_plus"),
            "xwoba":    xwoba_data.get(mlbam),
        }

    # --- Compute percentiles ---
    ids    = list(merged.keys())
    xwobas = [merged[m]["xwoba"] for m in ids]
    fips   = [merged[m]["fip"]   for m in ids]

    def _pcts(values, higher_is_better):
        valid = [(i, v) for i, v in enumerate(values) if v is not None]
        if not valid:
            return {}
        idxs, vals = zip(*valid)
        return dict(zip(idxs, compute_percentiles(list(vals), higher_is_better=higher_is_better)))

    xwoba_pcts = _pcts(xwobas, higher_is_better=False)
    fip_pcts   = _pcts(fips,   higher_is_better=False)

    QUALIFY_IP = 20

    result: dict[str, PitcherSeasonStats] = {}
    for i, mlbam in enumerate(ids):
        m  = merged[mlbam]
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
    year = int(sys.argv[1]) if len(sys.argv) > 1 else 2026

    print(f"\nFetching pitcher stats for {year}...\n")
    stats = fetch_pitcher_stats(year)
    print(f"Total pitchers: {len(stats)}\n")

    check = {"669923": "George Kirby", "694973": "Paul Skenes",
             "676979": "Garrett Crochet", "543037": "Gerrit Cole"}
    for mlbam, name in check.items():
        s = stats.get(mlbam)
        if s:
            print(f"  {name:20s}  IP={s.ip}  FIP={s.fip}  ERA+={s.era_plus}  "
                  f"xwOBA={s.xwoba}  fip_pct={s.fip_percentile}")
        else:
            print(f"  {name:20s}  NOT FOUND")
