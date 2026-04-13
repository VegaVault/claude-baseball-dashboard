"""
Fetcher: team bullpen stats (FIP, ERA+, xwOBA against) for a given year.

Strategy:
  - Reliever filter: GS == 0 from Baseball Reference standard pitching page
  - Aggregate to team level using IP-weighted averages
  - xwOBA against: Savant statcast_pitcher_expected_stats (same as SP fetcher)
  - Ranks + grades computed across all 30 teams

Returns {team_abbr: dict} keyed by our 3-letter abbreviations.
"""

import logging
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

# Bref team abbr → our abbr (only entries that differ)
_BREF_TO_OURS: dict[str, str] = {
    "CHW": "CWS",
    "KCR": "KC",
    "SDP": "SD",
    "SFG": "SF",
    "TBR": "TB",
    "WSN": "WSH",
    "OAK": "ATH",
}


def _our_abbr(bref: str) -> str:
    return _BREF_TO_OURS.get(str(bref).strip(), str(bref).strip())


def _clean_name(name: str) -> str:
    return re.sub(r"[*#]", "", str(name)).strip().lower()


def _to_float(v) -> float | None:
    try:
        return float(v)
    except (ValueError, TypeError):
        return None


def _to_int(v) -> int | None:
    try:
        return int(float(v))
    except (ValueError, TypeError):
        return None


# ── Bref scrape ────────────────────────────────────────────────────────────────

def _fetch_bref_relievers(year: int) -> pd.DataFrame:
    """
    Scrape bref standard pitching page and return only relievers (GS == 0).
    Columns retained: clean_name, team, ip, fip, era_plus
    """
    url = f"https://www.baseball-reference.com/leagues/majors/{year}-standard-pitching.shtml"
    resp = requests.get(url, headers=_BREF_HEADERS, timeout=30)
    resp.raise_for_status()

    html = re.sub(r"<!--\s*((<table)[\s\S]*?(</table>))\s*-->", r"\1", resp.text)
    tables = pd.read_html(StringIO(html))

    # Find the player-level table — bref uses "Team" (not "Tm") on this page
    df = None
    team_col = None
    for i, t in enumerate(tables):
        if "Player" not in t.columns:
            continue
        tc = "Team" if "Team" in t.columns else ("Tm" if "Tm" in t.columns else None)
        if tc and "GS" in t.columns:
            df = t
            team_col = tc
            logger.info("Found player table at index %d, team col=%r, columns: %s",
                        i, tc, list(t.columns))
            break

    if df is None:
        cols = [list(t.columns[:10]) for t in tables]
        raise ValueError(f"No player table with Team/Tm + GS found. Samples: {cols}")

    df = df.rename(columns={team_col: "Tm"})
    df = df[df["Player"] != "Player"].dropna(subset=["Player"])

    df["gs_int"]    = df["GS"].apply(_to_int)
    df["ip_f"]      = df["IP"].apply(_to_float)
    df["fip_f"]     = df["FIP"].apply(_to_float)
    df["era_plus_i"]= df["ERA+"].apply(_to_int)

    # Relievers: GS == 0, at least 2 IP, not a "TOT" row (traded player total)
    rel = df[
        (df["gs_int"] == 0) &
        (df["ip_f"] >= 2) &
        (df["Tm"] != "TOT")
    ].copy()

    rel["clean_name"] = rel["Player"].apply(_clean_name)
    rel["team"]       = rel["Tm"].apply(_our_abbr)
    return rel[["clean_name", "team", "ip_f", "fip_f", "era_plus_i"]]


# ── Savant xwOBA ──────────────────────────────────────────────────────────────

def _fetch_xwoba_savant(year: int) -> dict[str, float]:
    """mlbam_id -> xwOBA for pitchers with >= 5 PA."""
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

    Returns:
        {
          team_abbr: {
            "fip":           float,   # IP-weighted FIP
            "era_plus":      int,     # IP-weighted ERA+
            "xwoba":         float,   # IP-weighted xwOBA against
            "total_ip":      float,
            "fip_percentile":   int,  # 0–100, higher=better (lower FIP)
            "xwoba_percentile": int,
            "fip_label":     str,
            "xwoba_label":   str,
          }
        }
    """
    # ── 1. Bref: reliever rows ──────────────────────────────────────────────
    rel_df = pd.DataFrame()
    try:
        rel_df = _fetch_bref_relievers(year)
        logger.info("bref relievers: %d rows for %d", len(rel_df), year)
    except Exception as e:
        logger.error("bref relievers failed for %d: %s", year, e)

    # ── 2. mlbam crosswalk ─────────────────────────────────────────────────
    name_to_mlbam: dict[str, str] = {}
    try:
        cross = pybaseball.pitching_stats_bref(year)
        cross = cross.dropna(subset=["mlbID"])
        cross["mlbID"] = cross["mlbID"].astype(int).astype(str)
        name_to_mlbam = {_clean_name(r["Name"]): r["mlbID"] for _, r in cross.iterrows()}
        logger.info("mlbID crosswalk: %d entries for %d", len(name_to_mlbam), year)
    except Exception as e:
        logger.warning("mlbID crosswalk failed for %d: %s", year, e)

    # ── 3. Savant xwOBA ────────────────────────────────────────────────────
    xwoba_data: dict[str, float] = {}
    try:
        xwoba_data = _fetch_xwoba_savant(year)
        logger.info("Savant xwOBA: %d relievers for %d", len(xwoba_data), year)
    except Exception as e:
        logger.warning("Savant xwOBA failed for bullpen %d: %s", year, e)

    # ── 4. Aggregate by team ───────────────────────────────────────────────
    team_pitchers: dict[str, list[dict]] = {}

    for _, row in rel_df.iterrows():
        team   = row["team"]
        mlbam  = name_to_mlbam.get(row["clean_name"])
        xwoba  = xwoba_data.get(mlbam) if mlbam else None

        team_pitchers.setdefault(team, []).append({
            "ip":       row["ip_f"],
            "fip":      row["fip_f"],
            "era_plus": row["era_plus_i"],
            "xwoba":    xwoba,
        })

    raw: dict[str, dict] = {}
    for team, pitchers in team_pitchers.items():
        total_ip = sum(p["ip"] for p in pitchers)
        if total_ip == 0:
            continue

        def _wavg(key):
            vals = [(p["ip"], p[key]) for p in pitchers if p[key] is not None]
            if not vals:
                return None
            return sum(ip * v for ip, v in vals) / sum(ip for ip, _ in vals)

        raw[team] = {
            "fip":      _wavg("fip"),
            "era_plus": _wavg("era_plus"),
            "xwoba":    _wavg("xwoba"),
            "total_ip": total_ip,
        }

    # ── 5. Cross-team percentile ranks ────────────────────────────────────
    teams = list(raw.keys())

    def _rank_stat(key: str, higher_is_better: bool) -> dict[str, int]:
        import math
        vals = [(t, raw[t][key]) for t in teams
                if raw[t][key] is not None and not math.isnan(raw[t][key])]
        if not vals:
            return {}
        ts, vs = zip(*vals)
        pcts = compute_percentiles(list(vs), higher_is_better=higher_is_better)
        return dict(zip(ts, pcts))

    fip_pcts   = _rank_stat("fip",   higher_is_better=False)
    xwoba_pcts = _rank_stat("xwoba", higher_is_better=False)
    era_pcts   = _rank_stat("era_plus", higher_is_better=True)

    result: dict[str, dict] = {}
    for team in teams:
        d = raw[team]
        fp  = fip_pcts.get(team)
        xp  = xwoba_pcts.get(team)
        ep  = era_pcts.get(team)

        # Bullpen grade: same weights as SP (xwOBA 66.7% + FIP 33.3%)
        score = None
        if xp is not None and fp is not None:
            score = (xp * 0.667 + fp * 0.333) / 100
        elif xp is not None:
            score = xp / 100
        elif fp is not None:
            score = fp / 100

        import math
        def _safe(v, decimals=None):
            if v is None or (isinstance(v, float) and math.isnan(v)):
                return None
            return round(v, decimals) if decimals is not None else round(v)

        result[team] = {
            "fip":              _safe(d["fip"], 2),
            "era_plus":         _safe(d["era_plus"]),
            "xwoba":            _safe(d["xwoba"], 3),
            "total_ip":         round(d["total_ip"], 1),
            "fip_percentile":   fp,
            "xwoba_percentile": xp,
            "era_plus_percentile": ep,
            "fip_label":        percentile_to_label(fp) if fp is not None else None,
            "xwoba_label":      percentile_to_label(xp) if xp is not None else None,
            "grade":            score_to_grade(score)   if score is not None else "—",
        }

    logger.info("Bullpen stats: %d teams for %d", len(result), year)
    return result


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    year = int(sys.argv[1]) if len(sys.argv) > 1 else 2026

    print(f"\nFetching bullpen stats for {year}...\n")
    stats = fetch_bullpen_stats(year)
    print(f"\n{'Team':<6} {'FIP':>5} {'ERA+':>5} {'xwOBA':>6} {'IP':>6} {'Grade':>6}  xwOBA Label")
    print("-" * 55)
    for team, s in sorted(stats.items(), key=lambda x: x[1].get("fip") or 99):
        print(
            f"  {team:<4} {str(s.get('fip') or '—'):>5}  {str(s.get('era_plus') or '—'):>4}"
            f"  {str(s.get('xwoba') or '—'):>6}  {str(s.get('total_ip') or '—'):>5}"
            f"  {s.get('grade','—'):>5}  {s.get('xwoba_label') or '—'}"
        )
