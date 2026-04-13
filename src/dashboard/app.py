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
from src.fetch.labels import rank_to_grade, rank_to_score, score_to_grade
from src.fetch.park_factors import park_factor_label

# ── Paths ─────────────────────────────────────────────────────────────────────
DATA_DIR = Path(__file__).parents[2] / "data"
ET = ZoneInfo("America/New_York")

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


# ── General helpers ────────────────────────────────────────────────────────────

def utc_to_et(utc_str: str) -> datetime:
    dt = datetime.fromisoformat(utc_str.replace("Z", "+00:00"))
    return dt.astimezone(ET)


def fmt_time(utc_str: str) -> str:
    if not utc_str:
        return "TBD"
    dt = utc_to_et(utc_str)
    return dt.strftime("%I:%M %p ET").lstrip("0")


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

    # Bullpen grade (same formula as SP)
    def _bp_score(bp: dict) -> float | None:
        xp = bp.get("xwoba_percentile")
        fp = bp.get("fip_percentile")
        if xp is not None and fp is not None:
            return (xp * 0.667 + fp * 0.333) / 100
        if xp is not None:
            return xp / 100
        if fp is not None:
            return fp / 100
        return None

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

        game_options = {
            g["game_pk"]: f"{g['away_team']} @ {g['home_team']}  {fmt_time(g.get('first_pitch_utc',''))}"
            for g in games
        }

        selected_pk = st.selectbox(
            "Game",
            options=list(game_options.keys()),
            format_func=lambda pk: game_options[pk],
        )

        st.divider()
        st.caption(f"Updated: {snapshot.get('last_updated', '?')}")
        errors = snapshot.get("fetch_errors", [])
        if errors:
            with st.expander(f"⚠️ {len(errors)} fetch error(s)"):
                for e in errors:
                    st.caption(e)

    game = next(g for g in games if g["game_pk"] == selected_pk)

    render_header(game)

    # Matchup summary with inline lineup status subtitle
    lineup_status = game.get("lineup_status", "projected")
    fp_utc = game.get("first_pitch_utc", "")

    if lineup_status == "confirmed":
        status_note = "🟢 Lineups confirmed"
    elif lineup_status == "frozen":
        status_note = "🔵 Lineups locked — game in progress"
    else:
        # Projected: compute T-60 time
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

    st.markdown(f"### Matchup Summary")
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
