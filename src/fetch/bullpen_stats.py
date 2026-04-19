"""
Fetcher: team bullpen stats (FIP, xwOBA against) for a given year.

Sources:
  Reliever list + IP : MLB Stats API  (reliable — same source as schedule/lineups)
  FIP                : bref standard pitching page (best-effort, may fail in CI)
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

# Full team names as returned by MLB Stats API → our abbreviations
_FULL_NAME_TO_OURS: dict[str, str] = {
    "Arizona Diamondbacks":  "ARI", "Atlanta Braves":        "ATL",
    "Baltimore Orioles":     "BAL", "Boston Red Sox":        "BOS",
    "Chicago Cubs":          "CHC", "Chicago White Sox":     "CWS",
    "Cincinnati Reds":       "CIN", "Cleveland Guardians":   "CLE",
    "Colorado Rockies":      "COL", "Detroit Tigers":        "DET",
    "Houston Astros":        "HOU", "Kansas City Royals":    "KC",
    "Los Angeles Angels":    "LAA", "Los Angeles Dodgers":   "LAD",
    "Miami Marlins":         "MIA", "Milwaukee Brewers":     "MIL",
    "Minnesota Twins":       "MIN", "New York Mets":         "NYM",
    "New York Yankees":      "NYY", "Oakland Athletics":     "ATH",
    "Sacramento Athletics":  "ATH", "Athletics":             "ATH",
    "Philadelphia Phillies": "PHI",
    "Pittsburgh Pirates":    "PIT", "San Diego Padres":      "SD",
    "San Francisco Giants":  "SF",  "Seattle Mariners":      "SEA",
    "St. Louis Cardinals":   "STL", "Tampa Bay Rays":        "TB",
    "Texas Rangers":         "TEX", "Toronto Blue Jays":     "TOR",
    "Washington Nationals":  "WSH",
}

# bref-style 3-letter abbreviations → our abbreviations (kept for FIP matching)
_ABBR_TO_OURS: dict[str, str] = {
    "CHW": "CWS", "KCR": "KC",  "SDP": "SD",
    "SFG": "SF",  "TBR": "TB",  "WSN": "WSH", "OAK": "ATH",
    "ANA": "LAA", "FLA": "MIA",
}

def _our_abbr(s: str) -> str:
    s = str(s).strip()
    # Try full name first (MLB Stats API returns full names)
    if s in _FULL_NAME_TO_OURS:
        return _FULL_NAME_TO_OURS[s]
    # Fall back to abbreviation remapping
    return _ABBR_TO_OURS.get(s, s)

def _clean_name(name: str) -> str:
    return re.sub(r"[*#]", "", str(name)).strip().lower()

def _to_float(v) -> float | None:
    try:
        return float(v)
    except (ValueError, TypeError):
        return None

def _parse_ip(ip_str) -> float:
    """
    Convert MLB Stats API innings-pitched string to a float.
    Format: whole.thirds  e.g. "45.1" = 45 + 1/3 innings = 45.333...
    """
    try:
        s     = str(ip_str).strip()
        parts = s.split(".")
        whole = int(parts[0])
        thirds = int(parts[1][0]) if len(parts) > 1 and parts[1] else 0
        return whole + thirds / 3.0
    except Exception:
        return 0.0


# ── MLB Stats API: reliever list + IP (very reliable) ────────────────────────

def _fetch_mlb_relievers(year: int) -> pd.DataFrame:
    """
    Pull all pitchers' season stats from the MLB Stats API.
    Filter to relievers: gamesStarted <= 3 and IP >= 1.
    Returns DataFrame: mlbam, team, ip, name.
    """
    url = (
        "https://statsapi.mlb.com/api/v1/stats"
        f"?stats=season&group=pitching&gameType=R&season={year}"
        "&playerPool=All&limit=3000"
    )
    resp = requests.get(url, timeout=30)
    resp.raise_for_status()
    data = resp.json()

    rows = []
    for stat_group in data.get("stats", []):
        for split in stat_group.get("splits", []):
            stat   = split.get("stat",   {})
            player = split.get("player", {})
            team   = split.get("team",   {})

            gs = int(stat.get("gamesStarted") or 0)
            if gs > 3:          # skip clear starters (allow openers ≤ 3 GS)
                continue

            ip = _parse_ip(stat.get("inningsPitched", "0"))
            if ip < 1.0:        # skip tiny samples
                continue

            mlbam   = str(player.get("id", "")).strip()
            tm_name = (team.get("name") or "").strip()   # e.g. "San Diego Padres"
            our_tm  = _our_abbr(tm_name)
            name    = player.get("fullName", "")

            if not mlbam or not our_tm:
                continue

            rows.append({"mlbam": mlbam, "team": our_tm, "ip": ip, "name": name})

    df = pd.DataFrame(rows) if rows else pd.DataFrame(columns=["mlbam","team","ip","name"])
    logger.info("MLB API relievers: %d rows for %d", len(df), year)
    return df


# ── bref direct scrape: FIP (best-effort) ────────────────────────────────────

def _fetch_bref_fip(year: int) -> dict[str, float]:
    """
    Scrape bref standard pitching for FIP by player name.
    Best-effort — may be blocked on GitHub Actions IPs.
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
    out: dict[str, float] = {}
    for _, row in df.iterrows():
        fip = _to_float(row.get("FIP"))
        if fip is not None:
            out[_clean_name(row["Player"])] = fip
    return out


# ── Savant xwOBA ──────────────────────────────────────────────────────────────

def _fetch_xwoba_savant(year: int) -> dict[str, float]:
    """Savant expected stats → {mlbam_id: xwoba}."""
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
    Returns {team_abbr: {fip, xwoba, total_ip, fip_percentile,
                         xwoba_percentile, fip_label, xwoba_label, grade}}
    """
    # ── 1. Reliever rows (MLB API — reliable) ─────────────────────────────
    rel_df = pd.DataFrame()
    try:
        rel_df = _fetch_mlb_relievers(year)
    except Exception as e:
        logger.error("MLB API reliever fetch failed for %d: %s", year, e)

    if rel_df.empty:
        logger.warning("No reliever data for %d — bullpen stats unavailable.", year)
        return {}

    # ── 2. FIP by name (best-effort bref scrape) ──────────────────────────
    fip_by_name: dict[str, float] = {}
    try:
        fip_by_name = _fetch_bref_fip(year)
        logger.info("bref FIP: %d pitchers for %d", len(fip_by_name), year)
    except Exception as e:
        logger.warning("bref FIP scrape failed for %d (FIP will be —): %s", year, e)

    # ── 3. Savant xwOBA by mlbam ──────────────────────────────────────────
    xwoba_data: dict[str, float] = {}
    try:
        xwoba_data = _fetch_xwoba_savant(year)
        logger.info("Savant xwOBA: %d pitchers for %d", len(xwoba_data), year)
    except Exception as e:
        logger.warning("Savant xwOBA failed for bullpen %d: %s", year, e)

    # ── 4. Aggregate by team ──────────────────────────────────────────────
    team_pitchers: dict[str, list[dict]] = {}
    for _, row in rel_df.iterrows():
        team  = row["team"]
        mlbam = row["mlbam"]
        name  = _clean_name(row.get("name", ""))
        fip   = fip_by_name.get(name)
        xwoba = xwoba_data.get(mlbam)
        team_pitchers.setdefault(team, []).append({
            "ip": row["ip"], "fip": fip, "xwoba": xwoba,
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

    logger.info("Bullpen raw teams: %d for %d", len(raw), year)

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

    logger.info("Bullpen stats final: %d teams for %d", len(result), year)
    return result


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    year = int(sys.argv[1]) if len(sys.argv) > 1 else 2026
    print(f"\nFetching bullpen stats for {year}...\n")
    stats = fetch_bullpen_stats(year)
    print(f"\nTeams returned: {len(stats)}\n")
    for team, d in sorted(stats.items()):
        print(f"  {team:<4}  FIP={d.get('fip')}  xwOBA={d.get('xwoba')}  "
              f"Grade={d.get('grade')}  IP={d.get('total_ip')}")
