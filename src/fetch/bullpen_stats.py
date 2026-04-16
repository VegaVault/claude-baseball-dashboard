"""
Fetcher: team bullpen stats (FIP, xwOBA against) for a given year.

Sources:
  Reliever list + IP : pybaseball.pitching_stats_bref() (GS==0 filter, has mlbID)
  FIP                : bref standard pitching page (direct scrape, best-effort)
  xwOBA against      : Savant statcast_pitcher_expected_stats()

Returns {team_abbr: dict} keyed by our 3-letter abbreviations.
"""

import logging
import math
import re
from io import StringIO

import pandas as pd
import pybaseball
import requests

try:
    from src.fetch.labels import compute_percentiles, percentile_to_label, score_to_grade
except ModuleNotFoundError:
    import sys, os
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
    from src.fetch.labels import compute_percentiles, percentile_to_label, score_to_grade

logger = logging.getLogger(__name__)
pybaseball.cache.enable()

_BREF_HEADERS = {"User-Agent": "Mozilla/5.0"}

# pybaseball bref returns full city/team names in Tm column
_CITY_TO_OURS: dict[str, str] = {
    "Arizona":       "ARI", "Atlanta":       "ATL", "Baltimore":    "BAL",
    "Boston":        "BOS", "Chicago":       "CHC", "Chicago":      "CHC",
    "Cincinnati":    "CIN", "Cleveland":     "CLE", "Colorado":     "COL",
    "Detroit":       "DET", "Houston":       "HOU", "Kansas City":  "KC",
    "Los Angeles":   "LAD", "Miami":         "MIA", "Milwaukee":    "MIL",
    "Minnesota":     "MIN", "New York":      "NYY", "Oakland":      "ATH",
    "Athletics":     "ATH", "Philadelphia":  "PHI", "Pittsburgh":   "PIT",
    "San Diego":     "SD",  "San Francisco": "SF",  "Seattle":      "SEA",
    "St. Louis":     "STL", "Tampa Bay":     "TB",  "Texas":        "TEX",
    "Toronto":       "TOR", "Washington":    "WSH", "Angels":       "LAA",
    "White Sox":     "CWS", "Cubs":          "CHC", "Mets":         "NYM",
    "Yankees":       "NYY", "Dodgers":       "LAD", "Padres":       "SD",
    "Giants":        "SF",  "Mariners":      "SEA", "Cardinals":    "STL",
    "Rays":          "TB",  "Rangers":       "TEX", "Blue Jays":    "TOR",
    "Nationals":     "WSH", "Braves":        "ATL", "Orioles":      "BAL",
    "Red Sox":       "BOS", "Reds":          "CIN", "Guardians":    "CLE",
    "Rockies":       "COL", "Tigers":        "DET", "Astros":       "HOU",
    "Royals":        "KC",  "Marlins":       "MIA", "Brewers":      "MIL",
    "Twins":         "MIN", "Pirates":       "PIT", "Phillies":     "PHI",
}

# Also handle 3-letter bref abbreviations that differ from ours
_ABBR_TO_OURS: dict[str, str] = {
    "CHW": "CWS", "KCR": "KC", "SDP": "SD",
    "SFG": "SF",  "TBR": "TB", "WSN": "WSH", "OAK": "ATH",
}

def _our_abbr(t: str) -> str:
    t = str(t).strip()
    # Try city/name lookup first
    if t in _CITY_TO_OURS:
        return _CITY_TO_OURS[t]
    # Try 3-letter abbr remapping
    if t in _ABBR_TO_OURS:
        return _ABBR_TO_OURS[t]
    return t

def _clean_name(name: str) -> str:
    return re.sub(r"[*#]", "", str(name)).strip().lower()

def _to_float(v) -> float | None:
    try:
        return float(v)
    except (ValueError, TypeError):
        return None


# ── pybaseball bref: reliever list + IP (reliable in CI) ─────────────────────

def _fetch_pb_relievers(year: int) -> pd.DataFrame:
    """
    Use pybaseball.pitching_stats_bref() for GS, IP, Team, mlbID.
    Returns DataFrame with columns: mlbam, team, ip, clean_name.
    """
    df = pybaseball.pitching_stats_bref(year)
    df = df.dropna(subset=["mlbID"])
    df["mlbID"] = df["mlbID"].astype(int).astype(str)

    # Filter relievers: GS == 0
    df["gs_int"] = df["GS"].apply(lambda x: int(float(x)) if pd.notna(x) else None)
    rel = df[df["gs_int"] == 0].copy()

    # Team column: pybaseball bref uses "Tm"
    team_col = "Tm" if "Tm" in rel.columns else None

    rows = []
    for _, row in rel.iterrows():
        ip = _to_float(row.get("IP"))
        if not ip or ip < 2:
            continue
        team = _our_abbr(row[team_col]) if team_col else None
        if not team:
            continue
        rows.append({
            "mlbam":      row["mlbID"],
            "clean_name": _clean_name(row["Name"]),
            "team":       team,
            "ip":         ip,
        })

    return pd.DataFrame(rows)


# ── bref direct scrape: FIP (best-effort) ────────────────────────────────────

def _fetch_bref_fip(year: int) -> dict[str, float]:
    """
    Scrape bref standard pitching page for FIP.
    Returns {cleaned_name: fip}.
    May fail on GitHub Actions — falls back gracefully.
    """
    url  = f"https://www.baseball-reference.com/leagues/majors/{year}-standard-pitching.shtml"
    resp = requests.get(url, headers=_BREF_HEADERS, timeout=30)
    resp.raise_for_status()

    html   = re.sub(r"<!--\s*((<table)[\s\S]*?(</table>))\s*-->", r"\1", resp.text)
    tables = pd.read_html(StringIO(html))

    df = None
    for t in tables:
        if "Player" in t.columns and "FIP" in t.columns:
            df = t
            break

    if df is None:
        return {}

    df = df[df["Player"] != "Player"].dropna(subset=["Player"])
    out = {}
    for _, row in df.iterrows():
        name = _clean_name(row["Player"])
        fip  = _to_float(row.get("FIP"))
        if fip is not None:
            out[name] = fip
    return out


# ── Savant xwOBA ──────────────────────────────────────────────────────────────

def _fetch_xwoba_savant(year: int) -> dict[str, float]:
    df = pybaseball.statcast_pitcher_expected_stats(year, minPA=5)
    df.columns = [c.strip() for c in df.columns]
    id_col    = next((c for c in df.columns if c.lower() in ("player_id", "playerid")), None)
    xwoba_col = next((c for c in df.columns if c.lower() in ("est_woba", "xwoba", "est_woba_used")), None)
    if not id_col or not xwoba_col:
        return {}
    return {
        str(int(row[id_col])): (float(row[xwoba_col]) if pd.notna(row[xwoba_col]) else None)
        for _, row in df.iterrows()
    }


# ── Public API ────────────────────────────────────────────────────────────────

def fetch_bullpen_stats(year: int) -> dict[str, dict]:
    """
    Return team bullpen stats for the given year.
    Returns {team_abbr: {fip, xwoba, total_ip, fip_percentile, xwoba_percentile, grade, ...}}
    """
    # ── 1. Reliever rows (IP + team + mlbam) ──────────────────────────────
    rel_df = pd.DataFrame()
    try:
        rel_df = _fetch_pb_relievers(year)
        logger.info("bref relievers: %d rows for %d", len(rel_df), year)
    except Exception as e:
        logger.error("bref relievers failed for %d: %s", year, e)

    if rel_df.empty:
        return {}

    # ── 2. FIP by name (best-effort) ──────────────────────────────────────
    fip_by_name: dict[str, float] = {}
    try:
        fip_by_name = _fetch_bref_fip(year)
        logger.info("bref FIP: %d pitchers for %d", len(fip_by_name), year)
    except Exception as e:
        logger.warning("bref FIP scrape failed for %d (FIP will be —): %s", year, e)

    # ── 3. Savant xwOBA (by mlbam) ────────────────────────────────────────
    xwoba_data: dict[str, float] = {}
    try:
        xwoba_data = _fetch_xwoba_savant(year)
        logger.info("Savant xwOBA: %d pitchers for %d", len(xwoba_data), year)
    except Exception as e:
        logger.warning("Savant xwOBA failed for bullpen %d: %s", year, e)

    # ── 4. Aggregate by team ───────────────────────────────────────────────
    team_pitchers: dict[str, list[dict]] = {}
    for _, row in rel_df.iterrows():
        team  = row["team"]
        mlbam = row["mlbam"]
        fip   = fip_by_name.get(row["clean_name"])
        xwoba = xwoba_data.get(mlbam)
        team_pitchers.setdefault(team, []).append({
            "ip":    row["ip"],
            "fip":   fip,
            "xwoba": xwoba,
        })

    raw: dict[str, dict] = {}
    for team, pitchers in team_pitchers.items():
        total_ip = sum(p["ip"] for p in pitchers if p["ip"])
        if total_ip == 0:
            continue

        def _wavg(key, ps=pitchers):
            vals = [(p["ip"], p[key]) for p in ps
                    if p[key] is not None
                    and not (isinstance(p[key], float) and math.isnan(p[key]))]
            if not vals:
                return None
            ip_sum = sum(ip for ip, _ in vals)
            return sum(ip * v for ip, v in vals) / ip_sum if ip_sum else None

        raw[team] = {
            "fip":      _wavg("fip"),
            "xwoba":    _wavg("xwoba"),
            "total_ip": total_ip,
        }

    logger.info("Bullpen stats: %d teams for %d", len(raw), year)

    # ── 5. Cross-team percentile ranks ────────────────────────────────────
    teams = list(raw.keys())

    def _rank_stat(key, higher_is_better):
        vals = [(t, raw[t][key]) for t in teams
                if raw[t].get(key) is not None
                and not math.isnan(raw[t][key])]
        if not vals:
            return {}
        ts, vs = zip(*vals)
        return dict(zip(ts, compute_percentiles(list(vs), higher_is_better=higher_is_better)))

    fip_pcts   = _rank_stat("fip",   higher_is_better=False)
    xwoba_pcts = _rank_stat("xwoba", higher_is_better=False)

    def _safe(v, decimals=None):
        if v is None or (isinstance(v, float) and math.isnan(v)):
            return None
        return round(v, decimals) if decimals is not None else round(v)

    result: dict[str, dict] = {}
    for team in teams:
        d  = raw[team]
        fp = fip_pcts.get(team)
        xp = xwoba_pcts.get(team)

        score = None
        if xp is not None and fp is not None:
            score = (xp * 0.667 + fp * 0.333) / 100
        elif xp is not None:
            score = xp / 100
        elif fp is not None:
            score = fp / 100

        result[team] = {
            "fip":              _safe(d.get("fip"), 2),
            "xwoba":            _safe(d.get("xwoba"), 3),
            "total_ip":         _safe(d.get("total_ip"), 1),
            "fip_percentile":   fp,
            "xwoba_percentile": xp,
            "fip_label":        percentile_to_label(fp) if fp is not None else None,
            "xwoba_label":      percentile_to_label(xp) if xp is not None else None,
            "grade":            score_to_grade(score) if score is not None else "—",
        }

    return result


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    year = int(sys.argv[1]) if len(sys.argv) > 1 else 2026

    print(f"\nFetching bullpen stats for {year}...\n")
    stats = fetch_bullpen_stats(year)
    print(f"Teams returned: {len(stats)}\n")
    for team, d in sorted(stats.items()):
        print(f"  {team:<4}  FIP={d.get('fip')}  xwOBA={d.get('xwoba')}  "
              f"Grade={d.get('grade')}  IP={d.get('total_ip')}")
