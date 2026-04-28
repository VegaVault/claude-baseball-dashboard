"""
Streamlit dashboard: MLB daily matchup viewer.

Reads JSON files from data/ — no live API calls.
All times displayed in ET (converted from UTC stored in JSON).
"""

import json
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import streamlit as st
import sys

sys.path.insert(0, str(Path(__file__).parents[2]))
from src.fetch.labels        import rank_to_grade, rank_to_score, score_to_grade, grade_to_num
from src.fetch.park_factors  import park_factor_label
from src.build.picks_tracker import get_stats as _get_pick_stats, BANKROLL_START, BET_SIZE

# ── Paths ─────────────────────────────────────────────────────────────────────
DATA_DIR = Path(__file__).parents[2] / "data"
ET       = ZoneInfo("America/New_York")   # kept for UTC conversion reference
LOCAL_TZ = ZoneInfo("America/Denver")     # display timezone (MT)

# ── Conditional formatting color maps ─────────────────────────────────────────
# Red (hot/good) → Yellow → Blue (cold/poor)
GRADE_COLORS: dict[str, tuple[str, str]] = {
    "A+": ("#FF1744", "white"),   # neon red
    "A":  ("#FF5722", "white"),   # vivid orange-red
    "A-": ("#FF9800", "black"),   # vivid orange
    "B+": ("#FFD700", "black"),   # gold yellow
    "B":  ("#FFEE58", "black"),   # bright highlighter yellow
    "B-": ("#FFFDE7", "black"),   # near-white warm (neutral high)
    "C+": ("#E1F5FE", "black"),   # near-white cool (neutral low)
    "C":  ("#29B6F6", "black"),   # vivid sky blue
    "C-": ("#0288D1", "white"),   # medium vivid blue
    "D+": ("#0040FF", "white"),   # electric blue
    "D":  ("#1565C0", "white"),   # deep blue
    "D-": ("#0D47A1", "white"),   # dark navy
    "F":  ("#0A1045", "white"),   # near-black navy
    "—":  ("#eeeeee", "#888888"),
}

GRADE_STYLE: dict[str, str] = {
    g: f"background-color:{bg};color:{fg}"
    for g, (bg, fg) in GRADE_COLORS.items()
}

LABEL_STYLE: dict[str, str] = {
    "Elite":      "background-color:#FF1744;color:white",
    "Dominant":   "background-color:#FF5722;color:white",
    "Strong":     "background-color:#FF9800;color:black",
    "Solid":      "background-color:#FFD700;color:black",
    "Decent":     "background-color:#FFEE58;color:black",
    "Mediocre":   "background-color:#FFFDE7;color:black",
    "Shaky":      "background-color:#E1F5FE;color:black",
    "Weak":       "background-color:#29B6F6;color:black",
    "Brutal":     "background-color:#0288D1;color:white",
    "Unplayable": "background-color:#0A1045;color:white",
    "—":          "",
}


# ── Styler helpers ─────────────────────────────────────────────────────────────

def _style_grade(val: str) -> str:
    return GRADE_STYLE.get(str(val).strip(), "")


def _style_label(val: str) -> str:
    clean = str(val).replace("⚠", "").strip()
    return LABEL_STYLE.get(clean, "")


def _apply_map(styler, func, subset=None):
    """Compatibility shim: pandas 2.x uses .map(), 1.x uses .applymap()."""
    try:
        return styler.map(func, subset=subset)
    except AttributeError:
        return styler.applymap(func, subset=subset)


def _style_pitcher_table(df: pd.DataFrame) -> pd.DataFrame:
    """Row-aware styler for pitcher comparison tables."""
    styles = pd.DataFrame("", index=df.index, columns=df.columns)
    for col in df.columns:
        for idx in df.index:
            val = str(df.loc[idx, col])
            if idx == "Grade":
                styles.loc[idx, col] = _style_grade(val)
            elif idx == "Label":
                styles.loc[idx, col] = _style_label(val)
    return styles


# ── Grade badge + detail helpers ──────────────────────────────────────────────

def _grade_badge(grade: str) -> str:
    """Colored HTML badge for a letter grade."""
    bg, fg = GRADE_COLORS.get(grade, ("#eeeeee", "#888"))
    return (
        f"<div style='background:{bg};color:{fg};text-align:center;"
        f"padding:6px 4px;border-radius:6px;font-weight:700;font-size:1.1rem'>"
        f"{grade}</div>"
    )


def _rank_detail(rank: int | None) -> str:
    return "—" if rank is None else f"#{rank} of 30"


def _sp_detail(p: dict | None) -> str:
    """Compact stat + percentile string for an SP row."""
    if not p:
        return "TBD"
    cy = p.get("current_year") or {}
    parts = []
    if cy.get("ip") is not None:
        parts.append(f"IP {cy['ip']:.1f}")
    if cy.get("fip") is not None:
        fp = cy.get("fip_percentile")
        pct = f" ({fp}th pct)" if fp is not None else ""
        parts.append(f"FIP {cy['fip']:.2f}{pct}")
    if cy.get("xwoba") is not None:
        xp = cy.get("xwoba_percentile")
        pct = f" ({xp}th pct)" if xp is not None else ""
        parts.append(f"xwOBA {cy['xwoba']:.3f}{pct}")
    return " · ".join(parts) if parts else "No data"


def _stat_badge(val: str, pct: int | None) -> str:
    """Colored HTML cell for a stat value, colored by percentile."""
    if pct is None or val == "—":
        return f"<div style='padding:5px;text-align:center'>{val}</div>"
    grade = score_to_grade(pct / 100)
    bg, fg = GRADE_COLORS.get(grade, ("#eeeeee", "#888"))
    return (
        f"<div style='background:{bg};color:{fg};text-align:center;"
        f"padding:5px 6px;border-radius:4px;font-weight:600'>{val}</div>"
    )


def _era_plus_pct(era_plus: int | None) -> int | None:
    """Approximate percentile for ERA+. 100 = league avg ≈ 50th pct."""
    if era_plus is None:
        return None
    # 50 ERA+ ≈ 0th pct, 200 ERA+ ≈ 100th pct
    return min(max(int((era_plus - 50) / 1.5), 0), 100)


def _platoon_detail(lineup: list[dict], opp_throws: str | None) -> str:
    """Show how many batters have platoon advantage."""
    if not lineup or not opp_throws or opp_throws == "?":
        return "—"
    adv = sum(
        1 for b in lineup
        if (b.get("bats") or "?").upper() not in ("S", "?")
        and (b.get("bats") or "?").upper() != opp_throws.upper()
    )
    neutral = sum(1 for b in lineup if (b.get("bats") or "?").upper() in ("S", "?"))
    total = len(lineup)
    return f"{adv}/{total} batters adv." + (f" ({neutral} switch)" if neutral else "")


# ── Score helpers ──────────────────────────────────────────────────────────────

def _sp_score(pitcher: dict | None) -> float | None:
    """0–1 score for an SP: xwOBA-against 66.7% + FIP 33.3%."""
    if not pitcher:
        return None
    cy = pitcher.get("current_year") or {}
    xp = cy.get("xwoba_percentile")
    fp = cy.get("fip_percentile")
    if xp is not None and fp is not None:
        return (xp * 0.667 + fp * 0.333) / 100
    if xp is not None:
        return xp / 100
    if fp is not None:
        return fp / 100
    return None


def _platoon_score(lineup: list[dict], opp_throws: str | None) -> float | None:
    """
    0–1 platoon advantage score for a lineup vs an opposing SP's throwing hand.
    1.0 = every batter has platoon advantage, 0.0 = every batter at disadvantage.
    Switch hitters and unknowns count as neutral (0.5).
    """
    if not lineup or not opp_throws or opp_throws == "?":
        return None
    scores = []
    for b in lineup:
        bats = (b.get("bats") or "?").upper()
        if bats in ("S", "?"):
            scores.append(0.5)
        elif bats != opp_throws.upper():
            scores.append(1.0)   # platoon advantage
        else:
            scores.append(0.0)   # same hand = disadvantage
    return sum(scores) / len(scores) if scores else None


def _overall_score(
    offense_rank: int | None,
    sp_score: float | None,
    bullpen_score: float | None,
    defense_rank: int | None,
    platoon: float | None,
) -> float | None:
    """
    Weighted overall:
      Offense 50% | Pitching 30% (SP 70% + bullpen 30%) | Defense 15% | Platoon 5%
    """
    off  = rank_to_score(offense_rank)
    defn = rank_to_score(defense_rank)

    if sp_score is not None and bullpen_score is not None:
        pitch = sp_score * 0.70 + bullpen_score * 0.30
    elif sp_score is not None:
        pitch = sp_score
    elif bullpen_score is not None:
        pitch = bullpen_score
    else:
        pitch = None

    components = [(off, 0.50), (pitch, 0.30), (defn, 0.15), (platoon, 0.05)]
    valid = [(s, w) for s, w in components if s is not None]
    if not valid:
        return None
    total_w = sum(w for _, w in valid)
    return sum(s * w for s, w in valid) / total_w


def _bp_score(bp: dict) -> float | None:
    """0–1 score for a bullpen stats dict."""
    xp = bp.get("xwoba_percentile")
    fp = bp.get("fip_percentile")
    if xp is not None and fp is not None:
        return (xp * 0.667 + fp * 0.333) / 100
    if xp is not None:
        return xp / 100
    if fp is not None:
        return fp / 100
    return None


def _game_grades(game: dict) -> tuple[str, str]:
    """Return (away_overall_grade, home_overall_grade) for a game dict."""
    pitchers = game.get("pitchers", {})
    tr       = game.get("team_ranks", {})
    bullpen  = game.get("bullpen", {})
    lineups  = game.get("lineups", {})

    def _ov(side: str) -> str:
        t          = (tr.get(side) or {})
        opp        = "home" if side == "away" else "away"
        opp_throws = (pitchers.get(opp) or {}).get("throws")
        lineup     = lineups.get(side, [])
        sp_sc      = _sp_score(pitchers.get(side))
        bp_sc      = _bp_score(bullpen.get(side) or {})
        plat       = _platoon_score(lineup, opp_throws)
        sc         = _overall_score(
            t.get("hitting_xwoba_rank"), sp_sc, bp_sc,
            t.get("defense_oaa_rank"), plat,
        )
        return score_to_grade(sc) if sc is not None else "—"

    return _ov("away"), _ov("home")


def _bet_rec(away: str, home: str, away_g: str, home_g: str, game: dict) -> dict:
    """
    Compute betting recommendation from overall grades + moneyline.

    Signal tiers (grade gap only — VALUE badge added separately via EV):
      🔥 STRONG  = 3+ grade levels apart
      ⭐⭐ LEAN   = 2 grade levels apart
      ⭐ SLIGHT  = 1 grade level apart
      =  TOSS-UP = tied grades → bet the dog (better risk/reward)

    Returns dict: team, label, signal, conf, ml, gap
    """
    an = grade_to_num(away_g)
    hn = grade_to_num(home_g)

    odds    = game.get("odds") or {}
    ml_data = odds.get("moneyline") or {}
    away_ml = ml_data.get("away_ml")
    home_ml = ml_data.get("home_ml")

    if an is None or hn is None:
        return {"team": "—", "label": "NO DATA", "signal": "❓",
                "conf": "NO DATA", "ml": None, "gap": None}

    gap = abs(an - hn)

    if gap >= 3:   conf, signal = "STRONG",  "🔥"
    elif gap == 2: conf, signal = "LEAN",    "⭐⭐"
    elif gap == 1: conf, signal = "SLIGHT",  "⭐"
    else:          conf, signal = "TOSS-UP", "="

    if gap == 0:
        # TOSS-UP: bet the underdog (higher payout = better risk/reward at 50/50)
        if away_ml is not None and home_ml is not None:
            team, team_ml = (away, away_ml) if away_ml >= home_ml else (home, home_ml)
        elif away_ml is not None:
            team, team_ml = away, away_ml
        elif home_ml is not None:
            team, team_ml = home, home_ml
        else:
            team, team_ml = "—", None
        label = f"{team} (dog)" if team != "—" else "—"
    else:
        team, team_ml = (away, away_ml) if an > hn else (home, home_ml)
        label = team

    return {"team": team, "label": label, "signal": signal,
            "conf": conf, "ml": team_ml, "gap": gap}


def _raw_scores(game: dict) -> tuple[float | None, float | None]:
    """Return (away_overall_score, home_overall_score) as 0–1 floats (pre-grade)."""
    pitchers = game.get("pitchers", {})
    tr       = game.get("team_ranks", {})
    bullpen  = game.get("bullpen", {})
    lineups  = game.get("lineups", {})

    def _sc(side: str) -> float | None:
        t          = (tr.get(side) or {})
        opp        = "home" if side == "away" else "away"
        opp_throws = (pitchers.get(opp) or {}).get("throws")
        lineup     = lineups.get(side, [])
        sp_sc      = _sp_score(pitchers.get(side))
        bp_sc      = _bp_score(bullpen.get(side) or {})
        plat       = _platoon_score(lineup, opp_throws)
        return _overall_score(
            t.get("hitting_xwoba_rank"), sp_sc, bp_sc,
            t.get("defense_oaa_rank"), plat,
        )

    return _sc("away"), _sc("home")


def _ev_data(game: dict) -> dict | None:
    """
    Expected-Value calculation for both sides of a game.

    Model win probability = head-to-head normalised score.
    EV% = (our_prob × decimal_payout − 1) × 100.

    Returns dict keyed "away"/"home", each with:
      our_prob, market_prob, edge, ev_pct, ml
    Returns None if scores unavailable.
    """
    away_sc, home_sc = _raw_scores(game)
    if away_sc is None or home_sc is None or (away_sc + home_sc) == 0:
        return None

    total_sc     = away_sc + home_sc
    raw_away     = away_sc / total_sc
    raw_home     = home_sc / total_sc

    # Compress toward 50% — baseball games rarely exceed ~60% for even the
    # best team. Raw grade scores are not calibrated win probabilities.
    # Factor 0.55 limits max model edge to ~±27.5 pp from 50%.
    _COMPRESS = 0.55
    away_model_p = 0.5 + (raw_away - 0.5) * _COMPRESS
    home_model_p = 0.5 + (raw_home - 0.5) * _COMPRESS

    odds    = game.get("odds") or {}
    ml_data = odds.get("moneyline") or {}

    def _side(ml, model_p, impl_pct):
        result = {"our_prob": model_p, "market_prob": None,
                  "edge": None, "ev_pct": None, "ml": ml}
        if ml is None or impl_pct is None:
            return result
        market_p = impl_pct / 100.0
        edge     = model_p - market_p
        decimal  = (ml / 100 + 1) if ml > 0 else (100 / abs(ml) + 1)
        ev_pct   = (model_p * decimal - 1) * 100
        result.update(market_prob=market_p, edge=edge, ev_pct=ev_pct)
        return result

    return {
        "away": _side(ml_data.get("away_ml"), away_model_p, ml_data.get("away_impl")),
        "home": _side(ml_data.get("home_ml"), home_model_p, ml_data.get("home_impl")),
    }


def _ou_model(game: dict) -> dict:
    """
    O/U expected-total model.

    Expected scoring per team = 60% own L15 RPG + 40% opponent L15 RAPG.
    Adjusted for park factor then weather.
    Compared to the posted total line.

    Returns: model_total, posted_line, diff, lean, conf, notes
    """
    tf      = game.get("team_form", {})
    away_f  = (tf.get("away") or {})
    home_f  = (tf.get("home") or {})

    # Prefer L15, fall back to season
    a_rpg  = away_f.get("l15_rpg")  or away_f.get("season_rpg")
    h_rpg  = home_f.get("l15_rpg")  or home_f.get("season_rpg")
    a_rapg = away_f.get("l15_rapg") or away_f.get("season_rapg")
    h_rapg = home_f.get("l15_rapg") or home_f.get("season_rapg")

    empty = {"model_total": None, "posted_line": None, "diff": None,
             "lean": "—", "conf": "—", "notes": []}

    if a_rpg is None and h_rpg is None:
        return empty

    # Expected runs each team scores
    away_exp = (0.6 * a_rpg  + 0.4 * h_rapg) if (a_rpg  is not None and h_rapg is not None) else a_rpg
    home_exp = (0.6 * h_rpg  + 0.4 * a_rapg) if (h_rpg  is not None and a_rapg is not None) else h_rpg

    if away_exp is None or home_exp is None:
        return empty

    model_total = away_exp + home_exp
    notes: list[str] = []

    # Park factor — small LINEAR nudge only, capped at ±0.3 runs.
    # L15 RPG already reflects ~50% of park effects (half of games played there),
    # so a full multiplier would double-count. We add a residual adjustment for
    # the remaining half-game-worth of park influence.
    pf = game.get("park_factor")
    if pf and abs(pf - 1.0) > 0.02:
        park_adj = max(-0.3, min(0.3, (pf - 1.0) * 1.5))
        model_total += park_adj
        if park_adj > 0:
            notes.append(f"🏟 Hitter park (+{park_adj:.1f})")
        else:
            notes.append(f"🏟 Pitcher park ({park_adj:.1f})")

    # Weather
    wx      = game.get("weather") or {}
    wx_adj  = 0.0
    temp    = wx.get("temp_f")
    wind    = wx.get("wind_mph") or 0
    wdir    = (wx.get("wind_dir") or "").lower()
    cond    = (wx.get("condition") or "").lower()

    if temp is not None and temp < 50:
        wx_adj -= 0.5
        notes.append(f"🌡 Cold ({int(temp)}°F)")
    if wind >= 12:
        if "out" in wdir:
            wx_adj += 0.6
            notes.append(f"💨 Wind out {int(wind)} mph")
        elif "in" in wdir:
            wx_adj -= 0.6
            notes.append(f"💨 Wind in {int(wind)} mph")
    if any(w in cond for w in ("rain", "storm", "shower", "drizzle")):
        wx_adj -= 0.5
        notes.append("🌧 Rain")

    model_total = round(model_total + wx_adj, 1)

    # Compare to posted line
    tot         = (game.get("odds") or {}).get("total") or {}
    posted_line = tot.get("line")

    if posted_line is None:
        return {"model_total": model_total, "posted_line": None, "diff": None,
                "lean": "—", "conf": "—", "notes": notes}

    diff = round(model_total - posted_line, 1)

    if diff >= 1.0:      lean, conf = "OVER",  "HIGH"
    elif diff >= 0.5:    lean, conf = "OVER",  "MED"
    elif diff <= -1.0:   lean, conf = "UNDER", "HIGH"
    elif diff <= -0.5:   lean, conf = "UNDER", "MED"
    else:                lean, conf = "PUSH",  "LOW"

    return {"model_total": model_total, "posted_line": posted_line, "diff": diff,
            "lean": lean, "conf": conf, "notes": notes}


# ── General helpers ────────────────────────────────────────────────────────────

def utc_to_et(utc_str: str) -> datetime:
    dt = datetime.fromisoformat(utc_str.replace("Z", "+00:00"))
    return dt.astimezone(ET)


def utc_to_local(utc_str: str) -> datetime:
    dt = datetime.fromisoformat(utc_str.replace("Z", "+00:00"))
    return dt.astimezone(LOCAL_TZ)


def fmt_time(utc_str: str) -> str:
    """Display game time in MT (Mountain Time)."""
    if not utc_str:
        return "TBD"
    dt  = utc_to_local(utc_str)
    tz  = "MDT" if dt.dst() else "MST"
    return dt.strftime("%I:%M %p ").lstrip("0") + tz


def fmt_stat(val, decimals: int = 3) -> str:
    return "—" if val is None else f"{val:.{decimals}f}"


def fmt_int(val) -> str:
    return "—" if val is None else str(int(val))


def load_snapshot(date_str: str) -> dict | None:
    path = DATA_DIR / f"{date_str}.json"
    return json.loads(path.read_text()) if path.exists() else None


def available_dates() -> list[str]:
    return [f.stem for f in sorted(DATA_DIR.glob("????-??-??.json"), reverse=True)]


# ── Section renderers ──────────────────────────────────────────────────────────

def render_header(game: dict) -> None:
    away, home = game["away_team"], game["home_team"]
    status   = game["status"].replace("_", " ").title()
    time_str = fmt_time(game.get("first_pitch_utc", ""))
    score    = game.get("final_score")

    # L15 form
    tf       = game.get("team_form", {})
    away_f   = tf.get("away") or {}
    home_f   = tf.get("home") or {}

    def _form_str(f: dict) -> str:
        if f.get("wins") is None:
            return ""
        parts = [f"{f['wins']}-{f['losses']} L15"]
        if f.get("streak"):
            parts.append(f.get("streak"))
        if f.get("season_rpg") is not None:
            parts.append(f"{f['season_rpg']} RPG (season)")
        if f.get("l15_rpg") is not None:
            parts.append(f"{f['l15_rpg']} RPG (L15)")
        return "  ·  ".join(parts)

    away_form = _form_str(away_f)
    home_form = _form_str(home_f)

    # Park factor
    pf     = game.get("park_factor")
    pf_lbl = park_factor_label(pf) if pf else ""
    pf_str = f"🏟️ {pf_lbl} ({pf})" if pf else ""

    # Umpire
    ump     = game.get("umpire") or {}
    ump_str = ""
    if ump.get("name"):
        acc = ump.get("accuracy")
        ri  = ump.get("run_impact")
        ump_str = f"👤 HP: {ump['name']}"
        if acc is not None:
            ump_str += f"  {acc}% acc"
        if ri is not None:
            ump_str += f"  {ri} run impact/gm"

    # Weather
    wx      = game.get("weather") or {}
    wx_str  = wx.get("display", "")

    # Odds
    odds    = game.get("odds") or {}
    ml      = odds.get("moneyline") or {}
    tot     = odds.get("total") or {}
    rl      = odds.get("runline") or {}
    odds_parts = []
    if ml.get("away_ml") is not None:
        def _fmt_ml(v): return f"+{v}" if v > 0 else str(v)
        fav = away if ml.get("favorite") == "away" else home
        odds_parts.append(f"ML: {away} {_fmt_ml(ml['away_ml'])} / {home} {_fmt_ml(ml['home_ml'])}  (fav: {fav})")
    if tot.get("line") is not None:
        o = f"+{tot['over_odds']}" if tot.get('over_odds', 0) > 0 else str(tot.get('over_odds',''))
        odds_parts.append(f"O/U {tot['line']} ({o})")
    if rl.get("away_point") is not None:
        odds_parts.append(f"RL: {away} {rl['away_point']:+.1f} / {home} {rl['home_point']:+.1f}")
    odds_str = "  ·  ".join(odds_parts)

    col_a, col_mid, col_h = st.columns([2, 1, 2])
    with col_a:
        st.markdown(f"## {away}")
        st.caption(away_form or "Away")
    with col_mid:
        st.markdown(
            "<div style='text-align:center;padding-top:14px;font-size:1.4rem;color:#888'>@</div>",
            unsafe_allow_html=True,
        )
        if score:
            st.markdown(
                f"<div style='text-align:center;font-weight:700'>{score}</div>",
                unsafe_allow_html=True,
            )
    with col_h:
        st.markdown(f"## {home}")
        st.caption(home_form or "Home")

    caption = f"🕐 {time_str}  ·  {status}"
    if wx_str:
        caption += f"  ·  🌤 {wx_str}"
    if pf_str:
        caption += f"  ·  {pf_str}"
    if ump_str:
        caption += f"  ·  {ump_str}"
    st.caption(caption)
    if odds_str:
        st.caption(f"💰 {odds_str}")
    st.divider()


def render_matchup_summary(game: dict) -> None:
    """
    6-row matchup summary.
    Layout per row:  [Away Grade] [Away detail] [Category] [Home detail] [Home Grade]
    """
    away, home  = game["away_team"], game["home_team"]
    tr          = game.get("team_ranks", {})
    tr_a        = tr.get("away") or {}
    tr_h        = tr.get("home") or {}
    pitchers    = game.get("pitchers", {})
    away_p      = pitchers.get("away")
    home_p      = pitchers.get("home")
    lineups     = game.get("lineups", {})
    away_lineup = lineups.get("away", [])
    home_lineup = lineups.get("home", [])

    h_a = tr_a.get("hitting_xwoba_rank")
    h_h = tr_h.get("hitting_xwoba_rank")
    p_a = tr_a.get("pitching_xwoba_against_rank")
    p_h = tr_h.get("pitching_xwoba_against_rank")
    d_a = tr_a.get("defense_oaa_rank")
    d_h = tr_h.get("defense_oaa_rank")

    bullpen     = game.get("bullpen", {})
    away_bp     = bullpen.get("away") or {}
    home_bp     = bullpen.get("home") or {}

    away_sp = _sp_score(away_p)
    home_sp = _sp_score(home_p)
    away_sp_g = score_to_grade(away_sp) if away_sp is not None else "—"
    home_sp_g = score_to_grade(home_sp) if home_sp is not None else "—"

    def _bp_detail(bp: dict) -> str:
        parts = []
        if bp.get("fip") is not None:
            fp = bp.get("fip_percentile")
            pct = f" ({fp}th pct)" if fp is not None else ""
            parts.append(f"FIP {bp['fip']:.2f}{pct}")
        if bp.get("xwoba") is not None:
            xp = bp.get("xwoba_percentile")
            pct = f" ({xp}th pct)" if xp is not None else ""
            parts.append(f"xwOBA {bp['xwoba']:.3f}{pct}")
        if bp.get("total_ip") is not None:
            parts.append(f"IP {bp['total_ip']:.1f}")
        return " · ".join(parts) if parts else "—"

    away_bp_sc = _bp_score(away_bp)
    home_bp_sc = _bp_score(home_bp)
    away_bp_g  = score_to_grade(away_bp_sc) if away_bp_sc is not None else "—"
    home_bp_g  = score_to_grade(home_bp_sc) if home_bp_sc is not None else "—"

    away_plat = _platoon_score(away_lineup, (home_p or {}).get("throws"))
    home_plat = _platoon_score(home_lineup, (away_p or {}).get("throws"))
    away_plat_g = score_to_grade(away_plat) if away_plat is not None else "—"
    home_plat_g = score_to_grade(home_plat) if home_plat is not None else "—"

    away_ov = _overall_score(h_a, away_sp, away_bp_sc, d_a, away_plat)
    home_ov = _overall_score(h_h, home_sp, home_bp_sc, d_h, home_plat)

    # (grade_away, detail_away, category_label, detail_home, grade_home)
    rows = [
        (rank_to_grade(h_a), _rank_detail(h_a),
         "⚔️ Offense",
         _rank_detail(h_h), rank_to_grade(h_h)),

        (away_sp_g, _sp_detail(away_p),
         "⚾ Starting Pitcher",
         _sp_detail(home_p), home_sp_g),

        (away_bp_g, _bp_detail(away_bp),
         "🔥 Bullpen",
         _bp_detail(home_bp), home_bp_g),

        (rank_to_grade(d_a), _rank_detail(d_a),
         "🧤 Defense",
         _rank_detail(d_h), rank_to_grade(d_h)),

        (away_plat_g, _platoon_detail(away_lineup, (home_p or {}).get("throws")),
         "↔️ Platoon Advantage",
         _platoon_detail(home_lineup, (away_p or {}).get("throws")), home_plat_g),

        (score_to_grade(away_ov) if away_ov is not None else "—", "",
         "📊 Overall",
         "", score_to_grade(home_ov) if home_ov is not None else "—"),
    ]

    # Header:  [Away Stat] [Away Grade] [Category] [Home Grade] [Home Stat]
    c1, c2, c3, c4, c5 = st.columns([2.5, 1.1, 2.4, 1.1, 2.5])
    c1.markdown(f"**{away}**")
    c3.markdown("<div style='text-align:center'><b>Category</b></div>", unsafe_allow_html=True)
    c5.markdown(f"**{home}**", )

    for ga, da, cat, dh, gh in rows:
        c1, c2, c3, c4, c5 = st.columns([2.5, 1.1, 2.4, 1.1, 2.5])
        c1.caption(da)
        c2.markdown(_grade_badge(ga), unsafe_allow_html=True)
        c3.markdown(
            f"<div style='text-align:center;padding-top:6px'>{cat}</div>",
            unsafe_allow_html=True,
        )
        c4.markdown(_grade_badge(gh), unsafe_allow_html=True)
        c5.caption(dh)



def render_pitcher_matchup(game: dict, current_year: int) -> None:
    """Side-by-side pitcher comparison with overall grade row + prior year expander."""
    prior_year = current_year - 1
    away, home = game["away_team"], game["home_team"]
    pitchers   = game.get("pitchers", {})
    away_p     = pitchers.get("away")
    home_p     = pitchers.get("home")

    def _col_name(p: dict | None, team: str) -> str:
        if not p or not p.get("name"):
            return f"{team} (TBD)"
        return f"{p['name']} ({p.get('throws') or '?'})"

    away_col = _col_name(away_p, away)
    home_col = _col_name(home_p, home)

    def _get_stats(p: dict | None, year: int) -> dict:
        if not p:
            return {}
        s = p.get("current_year") if year == current_year else p.get("prior_year")
        return s or {}

    def _pitcher_rows(year: int, include_grade: bool = True):
        """
        Yields (stat_label, away_html, home_html) for each row.
        FIP, ERA+, xwOBA cells are conditionally colored by percentile.
        """
        a = _get_stats(away_p, year)
        h = _get_stats(home_p, year)

        # IP — no color
        yield ("IP",
               f"<div style='padding:5px;text-align:center'>{fmt_stat(a.get('ip'),1)}</div>",
               f"<div style='padding:5px;text-align:center'>{fmt_stat(h.get('ip'),1)}</div>")

        # FIP — colored, lower=better so percentile already inverted in data
        yield ("FIP",
               _stat_badge(fmt_stat(a.get("fip"), 2), a.get("fip_percentile")),
               _stat_badge(fmt_stat(h.get("fip"), 2), h.get("fip_percentile")))

        # ERA+ — colored, higher=better → use _era_plus_pct
        yield ("ERA+",
               _stat_badge(fmt_int(a.get("era_plus")), _era_plus_pct(a.get("era_plus"))),
               _stat_badge(fmt_int(h.get("era_plus")), _era_plus_pct(h.get("era_plus"))))

        # xwOBA — colored
        yield ("xwOBA against",
               _stat_badge(fmt_stat(a.get("xwoba"), 3), a.get("xwoba_percentile")),
               _stat_badge(fmt_stat(h.get("xwoba"), 3), h.get("xwoba_percentile")))

        # Label — colored
        def _lbl(s):
            l = s.get("xwoba_label") or "—"
            if l != "—" and not s.get("qualified", True):
                l += " ⚠"
            bg_style = LABEL_STYLE.get(l.replace(" ⚠","").strip(), "")
            return f"<div style='{bg_style};padding:5px;text-align:center'>{l}</div>"
        yield ("Label", _lbl(a), _lbl(h))

        # Grade (current year only)
        if include_grade:
            ag = score_to_grade(_sp_score(away_p)) if away_p else "—"
            hg = score_to_grade(_sp_score(home_p)) if home_p else "—"
            yield ("⭐ Grade", _grade_badge(ag), _grade_badge(hg))

    def _render_pitcher_table(year: int, include_grade: bool = True) -> None:
        # Header row
        c1, c2, c3 = st.columns([2.5, 2.0, 2.5])
        c1.markdown(f"**{away_col}**")
        c2.markdown("<div style='text-align:center'><b>Stat</b></div>", unsafe_allow_html=True)
        c3.markdown(f"**{home_col}**")

        for stat_lbl, away_html, home_html in _pitcher_rows(year, include_grade):
            c1, c2, c3 = st.columns([2.5, 2.0, 2.5])
            c1.markdown(away_html, unsafe_allow_html=True)
            c2.markdown(
                f"<div style='text-align:center;padding-top:5px'>{stat_lbl}</div>",
                unsafe_allow_html=True,
            )
            c3.markdown(home_html, unsafe_allow_html=True)

    # Current year
    st.markdown(f"**{current_year} Season**")
    _render_pitcher_table(current_year, include_grade=True)

    # Prior year in expander
    with st.expander(f"{prior_year} Season"):
        _render_pitcher_table(prior_year, include_grade=False)


def render_lineup_table(lineup: list[dict], current_year: int) -> None:
    prior_year = current_year - 1
    if not lineup:
        st.info("No lineup available yet — check back closer to first pitch.")
        return

    rows = []
    for b in sorted(lineup, key=lambda x: x.get("order", 99)):
        cy = b.get("current_year") or {}
        py = b.get("prior_year") or {}

        label = cy.get("xwoba_label") or "—"
        if cy.get("xwoba_label") and not cy.get("qualified", True):
            label += " ⚠"

        rows.append({
            "#":                                b.get("order", "?"),
            "Name":                             b.get("name", "?"),
            "B":                                b.get("bats") or "?",
            f"xwOBA '{str(current_year)[-2:]}": fmt_stat(cy.get("xwoba"), 3),
            "Label":                            label,
            f"PA '{str(current_year)[-2:]}":    fmt_int(cy.get("pa")),
            f"xwOBA '{str(prior_year)[-2:]}":   fmt_stat(py.get("xwoba"), 3),
        })

    # Keep "#" as a regular column (not index) to avoid non-unique index errors
    # when multiple players have unknown order ("?")
    df = pd.DataFrame(rows)
    styled = _apply_map(df.style, _style_label, subset=["Label"])
    st.dataframe(styled, use_container_width=True, height=355)


def render_summary_tab(games: list[dict]) -> None:
    """
    Summary tab: Best Bets (EV-ranked), O/U Analysis, Full Grade Board.
    All sections update automatically as lineups are confirmed.
    """

    # ── Signal legend pills ───────────────────────────────────────────────────
    pill = "display:inline-block;padding:3px 10px;border-radius:12px;font-size:0.82rem;font-weight:600;"
    c1, c2, c3, c4, c5 = st.columns(5)
    c1.markdown(f"<span style='{pill}background:#FF5722;color:white'>🔥 STRONG</span>"
                "<span style='font-size:0.75rem;margin-left:5px'>3+ grade gap</span>",
                unsafe_allow_html=True)
    c2.markdown(f"<span style='{pill}background:#FF9800;color:black'>⭐⭐ LEAN</span>"
                "<span style='font-size:0.75rem;margin-left:5px'>2 grade gap</span>",
                unsafe_allow_html=True)
    c3.markdown(f"<span style='{pill}background:#FFD700;color:black'>⭐ SLIGHT</span>"
                "<span style='font-size:0.75rem;margin-left:5px'>1 grade gap</span>",
                unsafe_allow_html=True)
    c4.markdown(f"<span style='{pill}background:#eeeeee;color:#333'>= TOSS-UP</span>"
                "<span style='font-size:0.75rem;margin-left:5px'>Tied → bet dog</span>",
                unsafe_allow_html=True)
    c5.markdown(f"<span style='{pill}background:#4CAF50;color:white'>💎 badge</span>"
                "<span style='font-size:0.75rem;margin-left:5px'>EV &gt; 5% on top of any tier</span>",
                unsafe_allow_html=True)

    st.divider()

    # ── Best Bets (ranked by confidence then EV%) ─────────────────────────────
    st.markdown("### 🏆 Best Bets")
    st.caption(
        "Ranked by confidence tier (grade gap), then EV% within each tier.  "
        "💎 = genuine line value: model edge > 5% after calibration."
    )

    # Confidence sort order — base signal only (VALUE is a badge, not a tier)
    _CONF_ORDER = {"🔥": 0, "⭐⭐": 1, "⭐": 2, "=": 3, "❓": 9}

    # Signal cell styles — base signals + badge combos
    _SIG_STYLE: dict[str, str] = {
        "🔥 💎":  "background-color:#4CAF50;color:white;font-weight:700",
        "🔥":     "background-color:#FF5722;color:white;font-weight:700",
        "⭐⭐ 💎": "background-color:#2e7d32;color:white;font-weight:700",
        "⭐⭐":   "background-color:#FF9800;color:black;font-weight:700",
        "⭐ 💎":  "background-color:#558b2f;color:white",
        "⭐":     "background-color:#FFD700;color:black",
        "= 💎":   "background-color:#1b5e20;color:white",
        "=":      "background-color:#eeeeee;color:#444",
    }

    _EV_VALUE_THRESHOLD = 5.0   # % — badge fires above this

    bet_rows: list[dict] = []
    for game in games:
        if game.get("status") == "final":
            continue
        away, home     = game["away_team"], game["home_team"]
        away_g, home_g = _game_grades(game)
        rec            = _bet_rec(away, home, away_g, home_g, game)
        ev             = _ev_data(game)

        side    = "away" if rec["team"] == away else "home"
        ev_side = ((ev or {}).get(side)) or {}

        our_p  = ev_side.get("our_prob")
        mkt_p  = ev_side.get("market_prob")
        edge   = ev_side.get("edge")
        ev_pct = ev_side.get("ev_pct")
        ml     = rec["ml"]
        ml_str = (f"+{ml}" if ml > 0 else str(ml)) if ml is not None else "—"

        # Attach 💎 badge when EV clears threshold
        base_sig = rec["signal"]
        signal   = f"{base_sig} 💎" if (ev_pct is not None and ev_pct >= _EV_VALUE_THRESHOLD) else base_sig

        bet_rows.append({
            "Game":          f"{away} @ {home}",
            "Time":          fmt_time(game.get("first_pitch_utc", "")),
            "Signal":        signal,
            "_conf_ord":     _CONF_ORDER.get(base_sig, 9),
            "_ev_num":       ev_pct if ev_pct is not None else -999.0,
            "Bet":           rec["label"],
            "ML":            ml_str,
            "Model%":        f"{our_p*100:.1f}%" if our_p is not None else "—",
            "Mkt%":          f"{mkt_p*100:.1f}%" if mkt_p is not None else "—",
            "Edge":          (f"+{edge*100:.1f}%" if edge >= 0 else f"{edge*100:.1f}%") if edge is not None else "—",
            "EV%":           (f"+{ev_pct:.1f}%" if ev_pct >= 0 else f"{ev_pct:.1f}%") if ev_pct is not None else "—",
        })

    bet_rows.sort(key=lambda r: (r["_conf_ord"], -r["_ev_num"]))
    for i, r in enumerate(bet_rows, 1):
        r["#"] = i

    disp    = ["#", "Game", "Time", "Signal", "Bet", "ML",
               "Model%", "Mkt%", "Edge", "EV%"]
    df_bets = pd.DataFrame(bet_rows)[disp] if bet_rows else pd.DataFrame(columns=disp)

    def _style_bets(frame: pd.DataFrame) -> pd.DataFrame:
        out = pd.DataFrame("", index=frame.index, columns=frame.columns)
        for i, row in frame.iterrows():
            sig  = str(row.get("Signal", "")).strip()
            cell = _SIG_STYLE.get(sig, "")
            out.at[i, "Signal"] = cell
            # Green row highlight for anything with the VALUE badge
            if "💎" in sig:
                for col in frame.columns:
                    if col != "Signal":
                        out.at[i, col] = "background-color:#0d2b0d"
            ev_s = str(row.get("EV%", ""))
            if ev_s.startswith("+"):
                out.at[i, "EV%"] = "color:#4CAF50;font-weight:600"
            elif ev_s.startswith("-"):
                out.at[i, "EV%"] = "color:#ef5350"
            e_s = str(row.get("Edge", ""))
            if e_s.startswith("+"):
                out.at[i, "Edge"] = "color:#4CAF50"
            elif e_s.startswith("-"):
                out.at[i, "Edge"] = "color:#ef5350"
        return out

    st.dataframe(
        df_bets.style.apply(_style_bets, axis=None),
        use_container_width=True, hide_index=True,
        height=min(65 + len(bet_rows) * 38, 660),
    )
    st.caption(
        "All columns are for the **recommended team** (Bet).  "
        "**Model%** = our compressed win prob · **Mkt%** = implied prob from ML · "
        "**Edge** = Model% − Mkt% · **EV%** = (Model% × decimal payout) − 1.  "
        "**💎** fires when EV > 5%."
    )

    st.divider()

    # ── O/U Analysis ──────────────────────────────────────────────────────────
    with st.expander("📊 O/U Analysis — All Games", expanded=False):
        st.caption(
            "Model: 60% own L15 RPG + 40% opponent L15 RA/G · then park factor + weather adjustment.  "
            "HIGH = model differs from line by 1+ run · MED = 0.5+ run."
        )
        ou_rows: list[dict] = []
        for game in games:
            away, home = game["away_team"], game["home_team"]
            tf         = game.get("team_form", {})
            a_f        = tf.get("away") or {}
            h_f        = tf.get("home") or {}
            ou         = _ou_model(game)

            a_rpg  = a_f.get("l15_rpg");  h_rpg  = h_f.get("l15_rpg")
            a_rapg = a_f.get("l15_rapg"); h_rapg = h_f.get("l15_rapg")

            diff_str = "—"
            if ou.get("diff") is not None:
                diff_str = f"+{ou['diff']}" if ou["diff"] >= 0 else str(ou["diff"])

            ou_rows.append({
                "Game":        f"{away} @ {home}",
                "Time":        fmt_time(game.get("first_pitch_utc", "")),
                f"{away} RPG": f"{a_rpg:.1f}" if a_rpg else "—",
                f"{home} RPG": f"{h_rpg:.1f}" if h_rpg else "—",
                f"{away} RA":  f"{a_rapg:.1f}" if a_rapg else "—",
                f"{home} RA":  f"{h_rapg:.1f}" if h_rapg else "—",
                "Model":       str(ou["model_total"]) if ou.get("model_total") else "—",
                "Line":        str(ou["posted_line"]) if ou.get("posted_line") else "—",
                "Diff":        diff_str,
                "Lean":        ou["lean"],
                "Conf":        ou["conf"],
                "Factors":     "  ".join(ou.get("notes", [])) or "—",
            })

        if ou_rows:
            df_ou = pd.DataFrame(ou_rows)
            _LEAN_STYLE = {
                "OVER":  "background-color:#FF5722;color:white;font-weight:700",
                "UNDER": "background-color:#0288D1;color:white;font-weight:700",
                "PUSH":  "background-color:#555;color:#ccc",
                "—":     "",
            }
            _CONF_STYLE_OU = {
                "HIGH": "color:#4CAF50;font-weight:700",
                "MED":  "color:#FF9800",
                "LOW":  "color:#888",
                "—":    "",
            }

            def _style_ou(frame: pd.DataFrame) -> pd.DataFrame:
                out = pd.DataFrame("", index=frame.index, columns=frame.columns)
                out["Lean"] = frame["Lean"].map(lambda v: _LEAN_STYLE.get(str(v), ""))
                out["Conf"] = frame["Conf"].map(lambda v: _CONF_STYLE_OU.get(str(v), ""))
                for i, d in enumerate(frame["Diff"]):
                    s = str(d)
                    if s.startswith("+"):
                        out.at[i, "Diff"] = "color:#FF5722"
                    elif s.startswith("-"):
                        out.at[i, "Diff"] = "color:#0288D1"
                return out

            st.dataframe(
                df_ou.style.apply(_style_ou, axis=None),
                use_container_width=True, hide_index=True,
            )
        else:
            st.info("No O/U data available yet.")

    # ── Full Grade Board ──────────────────────────────────────────────────────
    with st.expander("📋 Full Grade Board", expanded=False):
        grade_rows: list[dict] = []
        for game in games:
            away, home     = game["away_team"], game["home_team"]
            away_g, home_g = _game_grades(game)
            rec            = _bet_rec(away, home, away_g, home_g, game)
            status         = game.get("status", "")
            lineup_icon    = {"confirmed": "🟢", "frozen": "🔵", "projected": "🟡"}.get(
                game.get("lineup_status", "projected"), "⚪"
            )
            if status == "final":
                game_lbl = f"✅ Final: {game.get('final_score','')}"
            elif status == "in_progress":
                game_lbl = f"🔴 Live: {game.get('final_score','')}"
            else:
                game_lbl = f"{lineup_icon} {game.get('lineup_status','projected').title()}"

            ml = rec["ml"]
            grade_rows.append({
                "Time":    fmt_time(game.get("first_pitch_utc", "")),
                "Matchup": f"{away} @ {home}",
                "Status":  game_lbl,
                "Away":    away_g,
                "Home":    home_g,
                "Gap":     rec["gap"] if rec["gap"] is not None else "—",
                "Bet":     rec["label"],
                "ML":      (f"+{ml}" if ml > 0 else str(ml)) if ml is not None else "—",
                "Signal":  rec["signal"],
            })

        df_gb = pd.DataFrame(grade_rows)

        def _style_gb(frame: pd.DataFrame) -> pd.DataFrame:
            out = pd.DataFrame("", index=frame.index, columns=frame.columns)
            for col in ("Away", "Home"):
                out[col] = frame[col].map(lambda v: GRADE_STYLE.get(str(v).strip(), ""))
            out["Signal"] = frame["Signal"].map(lambda v: _SIG_STYLE.get(str(v).strip(), ""))
            return out

        st.dataframe(
            df_gb.style.apply(_style_gb, axis=None),
            use_container_width=True, hide_index=True,
            height=min(50 + len(grade_rows) * 38, 620),
        )


def render_tracker_tab(year: int) -> None:
    """
    Pick Tracker — running tally for the season, organized by confidence tier.
    Bankroll starts at $10,000, flat $100/bet.
    """
    try:
        stats = _get_pick_stats(year)
    except Exception as e:
        st.error(f"Could not load picks: {e}")
        return

    picks       = stats["picks"]
    bankroll    = stats["bankroll"]
    total_pnl   = stats["total_pnl"]
    total_bets  = stats["total_bets"]
    total_wins  = stats["total_wins"]
    total_losses= stats["total_losses"]
    pending     = stats["total_pending"]
    win_pct     = stats["win_pct"]

    # ── Bankroll hero banner ─────────────────────────────────────────────────
    pnl_color = "#4CAF50" if total_pnl >= 0 else "#F44336"
    pnl_sign  = "+" if total_pnl >= 0 else ""
    pct_gain  = (total_pnl / BANKROLL_START * 100)
    st.markdown(
        f"""
        <div style="background:linear-gradient(135deg,#1a237e,#283593);
                    border-radius:12px;padding:24px 32px;margin-bottom:16px;
                    display:flex;align-items:center;gap:40px;flex-wrap:wrap;">
          <div>
            <div style="color:#90caf9;font-size:0.85rem;font-weight:600;letter-spacing:1px">BANKROLL</div>
            <div style="color:white;font-size:2.2rem;font-weight:800">${bankroll:,.2f}</div>
            <div style="color:{pnl_color};font-size:1rem;font-weight:600">
              {pnl_sign}${total_pnl:,.2f} &nbsp;·&nbsp; {pnl_sign}{pct_gain:.1f}%
            </div>
          </div>
          <div style="border-left:1px solid #3949ab;padding-left:32px">
            <div style="color:#90caf9;font-size:0.85rem;font-weight:600;letter-spacing:1px">RECORD</div>
            <div style="color:white;font-size:1.8rem;font-weight:800">{total_wins}-{total_losses}</div>
            <div style="color:#b3c5ff;font-size:0.9rem">
              {win_pct:.1f}% win rate &nbsp;·&nbsp; ${BET_SIZE:.0f}/bet flat
            </div>
          </div>
          <div style="border-left:1px solid #3949ab;padding-left:32px">
            <div style="color:#90caf9;font-size:0.85rem;font-weight:600;letter-spacing:1px">BETS</div>
            <div style="color:white;font-size:1.8rem;font-weight:800">{total_bets}</div>
            <div style="color:#b3c5ff;font-size:0.9rem">{pending} pending</div>
          </div>
        </div>
        """,
        unsafe_allow_html=True,
    ) if total_bets > 0 else st.info("No resolved picks yet — results will appear after games go final.")

    if total_bets == 0 and pending == 0:
        st.info(
            "No picks recorded yet. Picks are automatically recorded each morning "
            "when the snapshot runs. Check back after the next scheduled build."
        )
        return

    st.divider()

    # ── By confidence tier ───────────────────────────────────────────────────
    st.markdown("### 📊 By Confidence Tier")

    _TIER_STYLE = {
        "🔥 STRONG":  "background-color:#FF5722;color:white",
        "⭐⭐ LEAN":   "background-color:#FF9800;color:black",
        "⭐ SLIGHT":  "background-color:#FFD700;color:black",
        "= TOSS-UP":  "background-color:#eeeeee;color:#333",
    }

    tier_rows = []
    for sig in stats["signal_order"]:
        d    = stats["by_signal"][sig]
        wp   = f"{d['win_pct']:.1f}%" if d["win_pct"] is not None else "—"
        pnl  = f"+${d['pnl']:,.2f}" if d["pnl"] >= 0 else f"-${abs(d['pnl']):,.2f}"
        tier_rows.append({
            "Signal":  sig,
            "Bets":    d["bets"],
            "W":       d["wins"],
            "L":       d["losses"],
            "Pending": d["pending"],
            "Win %":   wp,
            "P&L":     pnl,
        })
    # Totals row
    total_pnl_str = f"+${total_pnl:,.2f}" if total_pnl >= 0 else f"-${abs(total_pnl):,.2f}"
    tier_rows.append({
        "Signal":  "TOTAL",
        "Bets":    total_bets,
        "W":       total_wins,
        "L":       total_losses,
        "Pending": pending,
        "Win %":   f"{win_pct:.1f}%" if win_pct is not None else "—",
        "P&L":     total_pnl_str,
    })

    df_tier = pd.DataFrame(tier_rows)

    def _style_tier(df):
        styles = pd.DataFrame("", index=df.index, columns=df.columns)
        for i, row in df.iterrows():
            sig = row["Signal"]
            if sig in _TIER_STYLE:
                styles.loc[i, "Signal"] = _TIER_STYLE[sig] + ";font-weight:700;border-radius:4px"
            elif sig == "TOTAL":
                styles.loc[i, :] = "font-weight:800;border-top:2px solid #555"
            # Color P&L
            pnl_val = row["P&L"]
            if str(pnl_val).startswith("+"):
                styles.loc[i, "P&L"] = "color:#4CAF50;font-weight:700"
            elif str(pnl_val).startswith("-"):
                styles.loc[i, "P&L"] = "color:#F44336;font-weight:700"
        return styles

    st.dataframe(
        df_tier.style.apply(_style_tier, axis=None),
        use_container_width=True,
        hide_index=True,
        height=min(50 + len(tier_rows) * 38, 400),
    )

    st.divider()

    # ── Recent picks log ─────────────────────────────────────────────────────
    st.markdown("### 📋 Pick Log")

    resolved_picks = [p for p in reversed(picks) if p["result"] in ("win", "loss")]
    pending_picks  = [p for p in reversed(picks) if p["result"] == "pending"]

    def _picks_df(pick_list: list[dict], show_result: bool) -> pd.DataFrame | None:
        rows = []
        for p in pick_list:
            ml_s = (f"+{p['ml']}" if p["ml"] > 0 else str(p["ml"])) if p["ml"] is not None else "—"
            ev_s = (f"+{p['ev_pct']:.1f}%" if p["ev_pct"] >= 0 else f"{p['ev_pct']:.1f}%") if p.get("ev_pct") is not None else "—"
            row = {
                "Date":    p["date"][5:],   # MM-DD
                "Matchup": f"{p['away_team']}@{p['home_team']}",
                "Pick":    p["pick_team"],
                "ML":      ml_s,
                "Signal":  p["signal"],
                "AW":      p["away_grade"],
                "HM":      p["home_grade"],
                "EV%":     ev_s,
            }
            if show_result:
                pnl = p.get("pnl")
                pnl_s = f"+${pnl:.2f}" if pnl and pnl >= 0 else (f"-${abs(pnl):.2f}" if pnl else "—")
                score_s = f"{p['away_score']}-{p['home_score']}" if p.get("away_score") is not None else "—"
                row["Result"] = "✅ W" if p["result"] == "win" else "❌ L"
                row["Score"]  = score_s
                row["P&L"]    = pnl_s
            rows.append(row)
        return pd.DataFrame(rows) if rows else None

    def _style_picks(df, show_result: bool):
        styles = pd.DataFrame("", index=df.index, columns=df.columns)
        for col in ("AW", "HM"):
            if col in df.columns:
                styles[col] = df[col].map(lambda g: GRADE_STYLE.get(str(g).strip(), ""))
        sig_map = {
            "🔥 STRONG":  "background-color:#FF5722;color:white;font-weight:700",
            "⭐⭐ LEAN":   "background-color:#FF9800;color:black;font-weight:700",
            "⭐ SLIGHT":  "background-color:#FFD700;color:black;font-weight:700",
            "= TOSS-UP":  "background-color:#eeeeee;color:#333;font-weight:700",
        }
        if "Signal" in df.columns:
            styles["Signal"] = df["Signal"].map(lambda s: sig_map.get(s, ""))
        if show_result and "Result" in df.columns:
            styles["Result"] = df["Result"].map(
                lambda r: "color:#4CAF50;font-weight:700" if "W" in str(r)
                          else ("color:#F44336;font-weight:700" if "L" in str(r) else "")
            )
        if show_result and "P&L" in df.columns:
            styles["P&L"] = df["P&L"].map(
                lambda v: "color:#4CAF50;font-weight:700" if str(v).startswith("+")
                          else ("color:#F44336;font-weight:700" if str(v).startswith("-") else "")
            )
        return styles

    if pending_picks:
        with st.expander(f"⏳ Pending ({len(pending_picks)} games today / tonight)", expanded=True):
            df_pend = _picks_df(pending_picks, show_result=False)
            if df_pend is not None:
                st.dataframe(
                    df_pend.style.apply(lambda df: _style_picks(df, False), axis=None),
                    use_container_width=True, hide_index=True,
                    height=min(50 + len(pending_picks) * 38, 500),
                )

    if resolved_picks:
        df_res = _picks_df(resolved_picks[:50], show_result=True)   # last 50
        if df_res is not None:
            st.dataframe(
                df_res.style.apply(lambda df: _style_picks(df, True), axis=None),
                use_container_width=True, hide_index=True,
                height=min(50 + min(len(resolved_picks), 50) * 38, 720),
            )
    elif total_bets == 0:
        st.info("No resolved picks yet.")


def _load_odds_history(date_str: str) -> dict:
    """Load data/odds_history_YYYY-MM-DD.json; return empty dict if missing."""
    path = DATA_DIR / f"odds_history_{date_str}.json"
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except Exception:
        return {}


def render_odds_tab(date_str: str, games: list[dict]) -> None:
    """
    Odds History page — Moneyline / Run Line / O/U tables.
    Shows odds at three checkpoints: Midnight MT · 8am MT · Current (latest snapshot).
    """
    history   = _load_odds_history(date_str)
    snapshots = history.get("snapshots", {})

    midnight_odds = (snapshots.get("midnight") or {}).get("odds", {})
    morning_odds  = (snapshots.get("morning")  or {}).get("odds", {})

    midnight_at = (snapshots.get("midnight") or {}).get("captured_at", "")
    morning_at  = (snapshots.get("morning")  or {}).get("captured_at", "")

    def _ts(utc_iso: str) -> str:
        if not utc_iso:
            return "—"
        try:
            dt  = utc_to_local(utc_iso)
            tz  = "MDT" if dt.dst() else "MST"
            return dt.strftime("%-I:%M %p ") + tz
        except Exception:
            return utc_iso

    # Pull current odds from each game's odds field
    current_odds: dict[str, dict] = {}
    for g in games:
        key = f"{g['away_team']}_{g['home_team']}"
        if g.get("odds"):
            current_odds[key] = g["odds"]

    # Build the ordered game list (by first pitch)
    ordered_games = sorted(
        [g for g in games if g.get("status") != "final"],
        key=lambda g: g.get("first_pitch_utc") or "",
    ) or games

    def _fmt_ml(v: int | None) -> str:
        if v is None: return "—"
        return f"+{v}" if v > 0 else str(v)

    def _ml_move(old_ml: int | None, new_ml: int | None) -> str:
        """Movement text for a moneyline (negative = shorter/more favorite)."""
        if old_ml is None or new_ml is None:
            return "—"
        d = new_ml - old_ml
        if d == 0: return "—"
        return f"{d:+d}"

    def _ou_move(old_line: float | None, new_line: float | None) -> str:
        if old_line is None or new_line is None:
            return "—"
        d = new_line - old_line
        if d == 0: return "—"
        return f"{d:+.1f}"

    # ── Header chips ─────────────────────────────────────────────────────────
    pill = ("display:inline-block;padding:4px 12px;border-radius:12px;"
            "font-size:0.8rem;font-weight:600;margin-right:8px")
    c1, c2, c3 = st.columns(3)
    c1.markdown(
        f"<span style='{pill}background:#1565C0;color:white'>🌙 Midnight MT</span>"
        f"<span style='font-size:0.75rem;color:#888'>{_ts(midnight_at)}</span>",
        unsafe_allow_html=True,
    )
    c2.markdown(
        f"<span style='{pill}background:#E65100;color:white'>☀️ 8am MT</span>"
        f"<span style='font-size:0.75rem;color:#888'>{_ts(morning_at)}</span>",
        unsafe_allow_html=True,
    )
    c3.markdown(
        f"<span style='{pill}background:#2E7D32;color:white'>🔴 Current</span>"
        f"<span style='font-size:0.75rem;color:#888'>latest snapshot</span>",
        unsafe_allow_html=True,
    )

    if not midnight_odds and not morning_odds:
        st.info(
            "No odds history captured yet for this date. "
            "Snapshots are taken automatically at midnight MT and 8am MT. "
            "You can also trigger one manually from GitHub Actions → **Odds Snapshots** → Run workflow."
        )
        return

    st.divider()

    # Helper to get one game's odds from a snapshot dict
    def _g(snap: dict, away: str, home: str) -> dict:
        key = f"{away}_{home}"
        return snap.get(key) or {}

    # ── Color helpers ─────────────────────────────────────────────────────────
    def _ml_style(old_impl: float | None, new_impl: float | None) -> str:
        """Color a ML cell green if implied prob increased ≥2pp, red if decreased."""
        if old_impl is None or new_impl is None:
            return ""
        diff = new_impl - old_impl
        if diff >= 2:   return "color:#4CAF50;font-weight:700"
        if diff <= -2:  return "color:#ef5350;font-weight:700"
        return ""

    def _ou_style(old_l: float | None, new_l: float | None) -> str:
        if old_l is None or new_l is None:
            return ""
        d = new_l - old_l
        if d > 0:  return "color:#FF9800;font-weight:700"
        if d < 0:  return "color:#29B6F6;font-weight:700"
        return ""

    # ═══════════════════════════════════════════════════════════════════════
    # TABLE 1 — MONEYLINE
    # ═══════════════════════════════════════════════════════════════════════
    st.markdown("### 💰 Moneyline")
    st.caption("Away ML / Home ML at each checkpoint · **Bold fav** · Δ = midnight → current fav ML move")

    ml_rows = []
    for game in ordered_games:
        away, home = game["away_team"], game["home_team"]
        mid = (_g(midnight_odds, away, home).get("moneyline") or {})
        mrn = (_g(morning_odds,  away, home).get("moneyline") or {})
        cur = (_g(current_odds,  away, home).get("moneyline") or {})

        def _ml_cell(ml_data: dict, label_away: str, label_home: str) -> str:
            if not ml_data:
                return "—"
            fav = ml_data.get("favorite", "away")
            a   = _fmt_ml(ml_data.get("away_ml"))
            h   = _fmt_ml(ml_data.get("home_ml"))
            if fav == "away":
                return f"**{label_away} {a}** / {label_home} {h}"
            else:
                return f"{label_away} {a} / **{label_home} {h}**"

        # Movement: track the favorite's ML from midnight to current
        fav_side = cur.get("favorite") or mid.get("favorite") or "away"
        fav_lbl  = away if fav_side == "away" else home
        mid_fav  = mid.get(f"{fav_side}_ml")
        cur_fav  = cur.get(f"{fav_side}_ml")
        move     = _ml_move(mid_fav, cur_fav)
        # Negative move = favorite getting shorter (more expensive)
        if move != "—":
            val = int(move)
            move_styled = f"📈 {move}" if val < 0 else f"📉 {move}"
        else:
            move_styled = "—"

        ml_rows.append({
            "Game":       f"{away} @ {home}",
            "Time":       fmt_time(game.get("first_pitch_utc", "")),
            "Midnight":   (f"{_fmt_ml(mid.get('away_ml'))} / {_fmt_ml(mid.get('home_ml'))}"
                           if mid else "—"),
            "8am MT":     (f"{_fmt_ml(mrn.get('away_ml'))} / {_fmt_ml(mrn.get('home_ml'))}"
                           if mrn else "—"),
            "Current":    (f"{_fmt_ml(cur.get('away_ml'))} / {_fmt_ml(cur.get('home_ml'))}"
                           if cur else "—"),
            "_fav":       fav_lbl,
            "_mid_impl":  mid.get(f"{fav_side}_impl"),
            "_cur_impl":  cur.get(f"{fav_side}_impl"),
            "Fav":        fav_lbl,
            "Δ (fav ML)": move_styled,
        })

    def _style_ml(df: pd.DataFrame) -> pd.DataFrame:
        out = pd.DataFrame("", index=df.index, columns=df.columns)
        for i, row in df.iterrows():
            mi = row.get("_mid_impl")
            ci = row.get("_cur_impl")
            st_ = _ml_style(mi, ci)
            if st_:
                out.at[i, "Current"] = st_
            move = str(row.get("Δ (fav ML)", ""))
            if "📈" in move:
                out.at[i, "Δ (fav ML)"] = "color:#4CAF50;font-weight:700"
            elif "📉" in move:
                out.at[i, "Δ (fav ML)"] = "color:#ef5350;font-weight:700"
        return out

    df_ml = pd.DataFrame(ml_rows)[["Game", "Time", "Midnight", "8am MT", "Current", "Fav", "Δ (fav ML)"]]
    st.dataframe(
        df_ml.style.apply(_style_ml, axis=None),
        use_container_width=True, hide_index=True,
        height=min(65 + len(ml_rows) * 38, 600),
    )

    st.divider()

    # ═══════════════════════════════════════════════════════════════════════
    # TABLE 2 — RUN LINE
    # ═══════════════════════════════════════════════════════════════════════
    st.markdown("### ±1.5 Run Line")
    st.caption("Away RL odds / Home RL odds · typical spread is ±1.5 · track odds movement on the RL")

    rl_rows = []
    for game in ordered_games:
        away, home = game["away_team"], game["home_team"]
        mid = (_g(midnight_odds, away, home).get("runline") or {})
        mrn = (_g(morning_odds,  away, home).get("runline") or {})
        cur = (_g(current_odds,  away, home).get("runline") or {})

        def _rl_cell(rl: dict) -> str:
            if not rl:
                return "—"
            ap = rl.get("away_point"); ao = _fmt_ml(rl.get("away_odds"))
            hp = rl.get("home_point"); ho = _fmt_ml(rl.get("home_odds"))
            a_pt = f"{ap:+.1f}" if ap is not None else "?"
            h_pt = f"{hp:+.1f}" if hp is not None else "?"
            return f"{a_pt} ({ao}) / {h_pt} ({ho})"

        # Odds move on away -1.5 (the favorite RL side)
        mid_ao = mid.get("away_odds"); cur_ao = cur.get("away_odds")
        move   = _ml_move(mid_ao, cur_ao) if (mid_ao and cur_ao) else "—"

        rl_rows.append({
            "Game":        f"{away} @ {home}",
            "Time":        fmt_time(game.get("first_pitch_utc", "")),
            "Midnight":    _rl_cell(mid),
            "8am MT":      _rl_cell(mrn),
            "Current":     _rl_cell(cur),
            "Δ (away RL)": move if move == "—" else (f"📈 {move}" if int(move) < 0 else f"📉 {move}"),
            "_mid_ao":     mid_ao,
            "_cur_ao":     cur_ao,
        })

    def _style_rl(df: pd.DataFrame) -> pd.DataFrame:
        out = pd.DataFrame("", index=df.index, columns=df.columns)
        for i, row in df.iterrows():
            mi = row.get("_mid_ao"); ci = row.get("_cur_ao")
            if mi is not None and ci is not None:
                d = ci - mi
                if d < -5:  out.at[i, "Current"] = "color:#4CAF50;font-weight:700"
                elif d > 5: out.at[i, "Current"] = "color:#ef5350;font-weight:700"
            move = str(row.get("Δ (away RL)", ""))
            if "📈" in move: out.at[i, "Δ (away RL)"] = "color:#4CAF50;font-weight:700"
            elif "📉" in move: out.at[i, "Δ (away RL)"] = "color:#ef5350;font-weight:700"
        return out

    df_rl = pd.DataFrame(rl_rows)[["Game", "Time", "Midnight", "8am MT", "Current", "Δ (away RL)"]]
    st.dataframe(
        df_rl.style.apply(_style_rl, axis=None),
        use_container_width=True, hide_index=True,
        height=min(65 + len(rl_rows) * 38, 600),
    )

    st.divider()

    # ═══════════════════════════════════════════════════════════════════════
    # TABLE 3 — OVER / UNDER
    # ═══════════════════════════════════════════════════════════════════════
    st.markdown("### 🔢 Over / Under")
    st.caption("Total line + over/under odds at each checkpoint · orange = line moved up · blue = line moved down")

    ou_rows = []
    for game in ordered_games:
        away, home = game["away_team"], game["home_team"]
        mid = (_g(midnight_odds, away, home).get("total") or {})
        mrn = (_g(morning_odds,  away, home).get("total") or {})
        cur = (_g(current_odds,  away, home).get("total") or {})

        def _ou_cell(tot: dict) -> str:
            if not tot:
                return "—"
            line = tot.get("line")
            o    = _fmt_ml(tot.get("over_odds"))
            u    = _fmt_ml(tot.get("under_odds"))
            return f"**{line}**  O {o} / U {u}" if line else "—"

        mid_l = mid.get("line"); cur_l = cur.get("line")
        line_move = _ou_move(mid_l, cur_l)

        ou_rows.append({
            "Game":       f"{away} @ {home}",
            "Time":       fmt_time(game.get("first_pitch_utc", "")),
            "Midnight":   _ou_cell(mid),
            "8am MT":     _ou_cell(mrn),
            "Current":    _ou_cell(cur),
            "Δ line":     line_move,
            "_mid_line":  mid_l,
            "_cur_line":  cur_l,
        })

    def _style_ou(df: pd.DataFrame) -> pd.DataFrame:
        out = pd.DataFrame("", index=df.index, columns=df.columns)
        for i, row in df.iterrows():
            mi = row.get("_mid_line"); ci = row.get("_cur_line")
            if mi is not None and ci is not None:
                d = ci - mi
                style = _ou_style(mi, ci)
                if style:
                    out.at[i, "Current"] = style
                    out.at[i, "Δ line"]  = style
        return out

    df_ou = pd.DataFrame(ou_rows)[["Game", "Time", "Midnight", "8am MT", "Current", "Δ line"]]
    st.dataframe(
        df_ou.style.apply(_style_ou, axis=None),
        use_container_width=True, hide_index=True,
        height=min(65 + len(ou_rows) * 38, 600),
    )

    st.caption(
        "📈 = fav getting shorter (money coming in) · "
        "📉 = fav getting longer (public fading) · "
        "🟠 O/U line up · 🔵 O/U line down"
    )


def render_legend() -> None:
    st.divider()
    with st.expander("📖 Legend & Methodology"):
        col1, col2 = st.columns(2)
        with col1:
            st.markdown("""
**Grade Scale (A+ → F)**
| Grade | Percentile |
|---|---|
| A+ | Top 7% |
| A  | Top 13% |
| A- | Top 20% |
| B+ | Top 27% |
| B  | Top 33% |
| B- | Top 40% |
| C+ | Top 47% |
| C  | Top 53% |
| C- | Top 60% |
| D+ | Top 67% |
| D  | Top 73% |
| D- | Top 80% |
| F  | Bottom 20% |

**Player Labels** *(10th-percentile bands, 2026 season)*
Unplayable · Brutal · Weak · Shaky · Mediocre
Decent · Solid · Strong · Dominant · Elite

⚠ = small sample (batters: <50 PA · pitchers: <20 IP)

🔴 Red = hot/elite &nbsp;&nbsp; 🔵 Blue = cold/poor
""")
        with col2:
            st.markdown("""
**Matchup Summary Weights**
| Component | Weight |
|---|---|
| Offense (team xwOBA rank) | 50% |
| Starting Pitcher grade | 21% |
| Bullpen proxy (team xwOBA-against) | 9% |
| Defense (OAA rank) | 15% |
| Platoon Advantage | 5% |

**Starting Pitcher Grade**
xwOBA-against percentile × 66.7% + FIP percentile × 33.3%

**Platoon Advantage**
Each batter's handedness vs opposing SP throwing hand.
Switch hitters = neutral (0.5). Grade reflects the full lineup.

**Bullpen (proxy)**
Uses team pitching xwOBA-against rank as a stand-in.
True bullpen stats (xFIP, reliever xwOBA) coming in a future update.
""")


# ── Main app ───────────────────────────────────────────────────────────────────

def main() -> None:
    st.set_page_config(
        page_title="MLB Daily Dashboard",
        page_icon="⚾",
        layout="wide",
    )

    with st.sidebar:
        st.title("⚾ MLB Dashboard")
        st.divider()

        dates = available_dates()
        if not dates:
            st.error("No snapshots in data/. Run snapshot.py first.")
            st.stop()

        selected_date = st.selectbox(
            "Date",
            options=dates,
            format_func=lambda d: datetime.strptime(d, "%Y-%m-%d").strftime("%a %b %d %Y"),
        )

        snapshot = load_snapshot(selected_date)
        if not snapshot:
            st.error(f"Could not load {selected_date}.")
            st.stop()

        games = snapshot.get("games", [])
        if not games:
            st.warning("No games on this date.")
            st.stop()

        current_year = int(selected_date[:4])

        # Summary / Tracker / Odds are sentinel keys at top of dropdown
        SUMMARY_KEY = "__summary__"
        TRACKER_KEY = "__tracker__"
        ODDS_KEY    = "__odds__"
        game_options: dict[str, str] = {
            SUMMARY_KEY: "📋 Summary — All Games",
            TRACKER_KEY: "📊 Pick Tracker",
            ODDS_KEY:    "📈 Odds History",
        }
        game_options.update({
            g["game_pk"]: f"{g['away_team']} @ {g['home_team']}  {fmt_time(g.get('first_pitch_utc',''))}"
            for g in games
        })

        selected_pk = st.selectbox(
            "Game",
            options=list(game_options.keys()),
            format_func=lambda pk: game_options[pk],
        )

        st.divider()
        raw_upd = snapshot.get("last_updated", "")
        try:
            upd_local = utc_to_local(raw_upd)
            tz_label  = "MDT" if upd_local.dst() else "MST"
            upd_str   = upd_local.strftime("%-I:%M %p ") + tz_label
        except Exception:
            upd_str = raw_upd
        st.caption(f"Updated: {upd_str}")
        errors = snapshot.get("fetch_errors", [])
        if errors:
            with st.expander(f"⚠️ {len(errors)} fetch error(s)"):
                for e in errors:
                    st.caption(e)

    # ── Main content ──────────────────────────────────────────────────────────
    if selected_pk == SUMMARY_KEY:
        render_summary_tab(games)

    elif selected_pk == TRACKER_KEY:
        render_tracker_tab(current_year)

    elif selected_pk == ODDS_KEY:
        render_odds_tab(selected_date, games)

    else:
        game = next(g for g in games if g["game_pk"] == selected_pk)

        render_header(game)

        lineup_status = game.get("lineup_status", "projected")
        fp_utc = game.get("first_pitch_utc", "")

        if lineup_status == "confirmed":
            status_note = "🟢 Lineups confirmed"
        elif lineup_status == "frozen":
            status_note = "🔵 Lineups locked — game in progress"
        else:
            if fp_utc:
                try:
                    fp_dt = datetime.fromisoformat(fp_utc.replace("Z", "+00:00"))
                    t60_et = (fp_dt - timedelta(minutes=60)).astimezone(ET)
                    t60_str = t60_et.strftime("%I:%M %p ET").lstrip("0")
                    status_note = f"🟡 Projected starters — confirmed lineups expected around {t60_str}"
                except Exception:
                    status_note = "🟡 Projected starters — lineups not yet confirmed"
            else:
                status_note = "🟡 Projected starters — lineups not yet confirmed"

        st.markdown("### Matchup Summary")
        st.caption(status_note)
        render_matchup_summary(game)

        st.divider()

        st.markdown("### Starting Pitcher Matchup")
        render_pitcher_matchup(game, current_year)

        st.divider()

        st.markdown("### Lineups")
        col_al, col_hl = st.columns(2)
        with col_al:
            st.caption(f"**{game['away_team']}** — Away")
            render_lineup_table(game["lineups"].get("away", []), current_year)
        with col_hl:
            st.caption(f"**{game['home_team']}** — Home")
            render_lineup_table(game["lineups"].get("home", []), current_year)

        render_legend()


if __name__ == "__main__":
    main()
