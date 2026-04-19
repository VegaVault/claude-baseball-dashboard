"""
Discord notifier: posts game report cards as embeds via webhook.

Layout (top → bottom):
  1. Context       — weather, park factor, umpire, lineup status, L15 form
  2. Matchup table — side-by-side category comparison (code block)
  3. SP detail     — pitcher names + stats
  4. Overall grade — visually prominent box at bottom

Modes:
  Default  — posts games in the T-75 → T-45 window (pregame card)
  --all    — posts ALL games in today's snapshot (morning briefing)
  --force  — like --all but re-posts already-sent games

Run via:  python -m src.notify.discord [--date YYYY-MM-DD] [--all] [--force]
"""

import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

ROOT     = Path(__file__).parents[2]
DATA_DIR = ROOT / "data"

try:
    from src.fetch.labels       import rank_to_grade, rank_to_score, score_to_grade
    from src.fetch.park_factors import park_factor_label
except ImportError:
    sys.path.insert(0, str(ROOT))
    from src.fetch.labels       import rank_to_grade, rank_to_score, score_to_grade
    from src.fetch.park_factors import park_factor_label

WEBHOOK_URL  = os.getenv("DISCORD_WEBHOOK_URL", "")
NOTIFY_EARLY = 90   # start posting 90 min before first pitch
NOTIFY_LATE  = 20   # stop posting 20 min before first pitch
ET_OFFSET    = timedelta(hours=-4)   # EDT; change to -5 for EST


# ── Time helpers ──────────────────────────────────────────────────────────────

def _utc_now() -> datetime:
    return datetime.now(timezone.utc)

def _parse_utc(s: str) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None

def _to_et_str(utc_str: str) -> str:
    dt = _parse_utc(utc_str)
    if not dt:
        return "TBD"
    et = dt + ET_OFFSET
    h = int(et.strftime("%I"))
    m = et.strftime("%M")
    return f"{h}:{m} {et.strftime('%p')} ET"


# ── Score helpers ─────────────────────────────────────────────────────────────

def _fmt(val, d=3):
    return "—" if val is None else f"{val:.{d}f}"

def _fmt_ml(v: int | None) -> str:
    if v is None: return "—"
    return f"+{v}" if v > 0 else str(v)

def _odds_line(game: dict) -> str:
    """Single line: ML · O/U · RL"""
    odds = game.get("odds") or {}
    ml   = odds.get("moneyline") or {}
    tot  = odds.get("total")     or {}
    rl   = odds.get("runline")   or {}
    away = game["away_team"]
    home = game["home_team"]

    parts = []
    if ml.get("away_ml") is not None:
        fav     = away if ml.get("favorite") == "away" else home
        impl    = ml.get("away_impl") if ml.get("favorite") == "away" else ml.get("home_impl")
        fav_ml  = ml["away_ml"] if ml.get("favorite") == "away" else ml["home_ml"]
        dog_ml  = ml["home_ml"] if ml.get("favorite") == "away" else ml["away_ml"]
        parts.append(f"**{fav}** {_fmt_ml(fav_ml)}  /  {_fmt_ml(dog_ml)}  *({impl}%)*")
    if tot.get("line") is not None:
        o = _fmt_ml(tot.get("over_odds"))
        parts.append(f"O/U **{tot['line']}** ({o})")
    if rl.get("away_point") is not None:
        away_pt = f"{rl['away_point']:+.1f}"
        home_pt = f"{rl['home_point']:+.1f}"
        parts.append(f"RL: {away} {away_pt} / {home} {home_pt}")
    return "\n".join(parts) if parts else "—"

def _sp_score(p: dict | None) -> float | None:
    if not p:
        return None
    cy = p.get("current_year") or {}
    xp, fp = cy.get("xwoba_percentile"), cy.get("fip_percentile")
    if xp is not None and fp is not None:
        return (xp * 0.667 + fp * 0.333) / 100
    return (xp / 100) if xp is not None else ((fp / 100) if fp is not None else None)

def _bp_score(bp: dict | None) -> float | None:
    if not bp:
        return None
    xp, fp = bp.get("xwoba_percentile"), bp.get("fip_percentile")
    if xp is not None and fp is not None:
        return (xp * 0.667 + fp * 0.333) / 100
    return (xp / 100) if xp is not None else ((fp / 100) if fp is not None else None)

def _platoon_score(lineup: list, opp_throws: str | None) -> float | None:
    if not lineup or not opp_throws or opp_throws == "?":
        return None
    scores = []
    for b in lineup:
        bats = (b.get("bats") or "?").upper()
        scores.append(0.5 if bats in ("S","?") else (1.0 if bats != opp_throws.upper() else 0.0))
    return sum(scores) / len(scores) if scores else None

def _platoon_str(lineup: list, opp_throws: str | None) -> str:
    if not lineup or not opp_throws or opp_throws == "?":
        return "—"
    adv = sum(1 for b in lineup
              if (b.get("bats") or "?").upper() not in ("S","?")
              and (b.get("bats") or "?").upper() != opp_throws.upper())
    return f"{adv}/{len(lineup)}"

def _ou_model(game: dict) -> dict:
    """O/U expected-total model (mirrors app.py logic)."""
    tf     = game.get("team_form", {})
    a_f    = tf.get("away") or {}
    h_f    = tf.get("home") or {}
    a_rpg  = a_f.get("l15_rpg")  or a_f.get("season_rpg")
    h_rpg  = h_f.get("l15_rpg")  or h_f.get("season_rpg")
    a_rapg = a_f.get("l15_rapg") or a_f.get("season_rapg")
    h_rapg = h_f.get("l15_rapg") or h_f.get("season_rapg")
    empty  = {"model_total": None, "diff": None, "lean": "—", "conf": "—", "notes": []}
    if a_rpg is None and h_rpg is None:
        return empty
    away_exp = (0.6*a_rpg + 0.4*h_rapg) if (a_rpg and h_rapg) else a_rpg
    home_exp = (0.6*h_rpg + 0.4*a_rapg) if (h_rpg and a_rapg) else h_rpg
    if away_exp is None or home_exp is None:
        return empty
    total = away_exp + home_exp
    notes = []
    pf = game.get("park_factor")
    if pf and abs(pf - 1.0) > 0.02:
        total *= pf
        if pf >= 1.08:   notes.append(f"🏟 HitterPark({pf:.2f})")
        elif pf <= 0.93: notes.append(f"🏟 PitcherPark({pf:.2f})")
    wx   = game.get("weather") or {}
    adj  = 0.0
    temp = wx.get("temp_f"); wind = wx.get("wind_mph") or 0
    wdir = (wx.get("wind_dir") or "").lower(); cond = (wx.get("condition") or "").lower()
    if temp and temp < 50:     adj -= 0.5; notes.append(f"🌡{int(temp)}°F")
    if wind >= 12:
        if "out" in wdir:      adj += 0.6; notes.append(f"💨out{int(wind)}mph")
        elif "in" in wdir:     adj -= 0.6; notes.append(f"💨in{int(wind)}mph")
    if any(w in cond for w in ("rain","storm","shower","drizzle")):
        adj -= 0.5; notes.append("🌧rain")
    total = round(total + adj, 1)
    posted = (game.get("odds") or {}).get("total", {})
    line   = posted.get("line") if posted else None
    if line is None:
        return {"model_total": total, "diff": None, "lean": "—", "conf": "—", "notes": notes}
    diff = round(total - line, 1)
    if diff >= 1.0:      lean, conf = "OVER",  "HIGH"
    elif diff >= 0.5:    lean, conf = "OVER",  "MED"
    elif diff <= -1.0:   lean, conf = "UNDER", "HIGH"
    elif diff <= -0.5:   lean, conf = "UNDER", "MED"
    else:                lean, conf = "PUSH",  "LOW"
    return {"model_total": total, "posted_line": line, "diff": diff,
            "lean": lean, "conf": conf, "notes": notes}


def _game_ov_grade(game: dict, side: str) -> str:
    """Compute overall letter grade for one side of a game dict."""
    pitchers   = game.get("pitchers", {})
    tr         = (game.get("team_ranks", {}) or {}).get(side) or {}
    bp         = (game.get("bullpen",    {}) or {}).get(side) or {}
    lineup     = (game.get("lineups",    {}) or {}).get(side, [])
    opp        = "home" if side == "away" else "away"
    opp_throws = (pitchers.get(opp) or {}).get("throws")
    sp_sc      = _sp_score(pitchers.get(side))
    bp_sc      = _bp_score(bp)
    plat       = _platoon_score(lineup, opp_throws)
    sc = _overall_score(
        tr.get("hitting_xwoba_rank"), sp_sc, bp_sc,
        tr.get("defense_oaa_rank"), plat,
    )
    return score_to_grade(sc) if sc is not None else "—"


def _overall_score(off_rank, sp_sc, bp_sc, def_rank, plat) -> float | None:
    off  = rank_to_score(off_rank)
    defn = rank_to_score(def_rank)
    if sp_sc is not None and bp_sc is not None:
        pitch = sp_sc * 0.70 + bp_sc * 0.30
    else:
        pitch = sp_sc or bp_sc
    scores, weights = [], []
    for val, w in [(off, 0.50), (pitch, 0.30), (defn, 0.15), (plat, 0.05)]:
        if val is not None:
            scores.append(val * w); weights.append(w)
    return sum(scores) / sum(weights) if scores else None

def _lineup_badge(status: str) -> str:
    return {"confirmed":"🟢 Confirmed","projected":"🟡 Projected","frozen":"🔵 Frozen"}.get(
        status, f"⚪ {status.title()}")


# ── Context description (top block) ──────────────────────────────────────────

def _context_description(game: dict, away: str, home: str) -> str:
    lines = []

    wx = game.get("weather") or {}
    if wx.get("display"):
        lines.append(f"🌤️  {wx['display']}")

    pf = game.get("park_factor")
    if pf:
        lines.append(f"🏟️  {home}: {park_factor_label(pf)}  ({pf})")

    ump = game.get("umpire") or {}
    if ump.get("name"):
        parts = [f"👤  HP: **{ump['name']}**"]
        if ump.get("accuracy") is not None:
            parts.append(f"{ump['accuracy']}% acc")
        if ump.get("run_impact") is not None:
            ri = ump["run_impact"]
            parts.append(f"{'+'if ri>0 else ''}{ri} run/gm")
        lines.append("  ·  ".join(parts))

    lines.append(f"📋  {_lineup_badge(game.get('lineup_status','projected'))}")

    odds_str = _odds_line(game)
    if odds_str != "—":
        lines.append(f"💰  {odds_str}")

    tf = game.get("team_form") or {}
    for side, team in (("away", away), ("home", home)):
        f = tf.get(side) or {}
        if f.get("wins") is not None:
            w, l   = f["wins"], f["losses"]
            streak = f.get("streak", "")
            l_rpg  = f.get("l15_rpg")
            s_rpg  = f.get("season_rpg")
            rpg    = f"  ·  {l_rpg} RPG (L15)" if l_rpg else ""
            if s_rpg:
                rpg += f"  /  {s_rpg} (ssn)"
            lines.append(f"📈  **{team}**  {w}-{l} L15  {streak}{rpg}")

    return "\n".join(lines)


# ── Matchup table (code block) ────────────────────────────────────────────────

def _matchup_table(away: str, home: str, game: dict) -> str:
    """
    Monospace table:
         GRADE  OFF   SP   BULL  DEF   PLAT
    NYY    B+    A    A-    B+    C    7/9
    BOS    B-   B+     B    C+   B-   5/9
    """
    pitchers = game.get("pitchers", {})
    tr       = game.get("team_ranks", {})
    bullpen  = game.get("bullpen", {})
    lineups  = game.get("lineups", {})

    def _row(side, team):
        t       = tr.get(side) or {}
        bp      = bullpen.get(side) or {}
        lineup  = lineups.get(side, [])
        opp     = "home" if side == "away" else "away"
        opp_throws = (pitchers.get(opp) or {}).get("throws")

        off_r  = t.get("hitting_xwoba_rank")
        def_r  = t.get("defense_oaa_rank")
        sp_sc  = _sp_score(pitchers.get(side))
        bp_sc  = _bp_score(bp)
        plat   = _platoon_score(lineup, opp_throws)

        off_g  = rank_to_grade(off_r)
        sp_g   = score_to_grade(sp_sc)   if sp_sc  is not None else "—"
        bp_g   = score_to_grade(bp_sc)   if bp_sc  is not None else "—"
        def_g  = rank_to_grade(def_r)
        pl_str = _platoon_str(lineup, opp_throws)

        return team, off_g, sp_g, bp_g, def_g, pl_str

    a = _row("away", away)
    h = _row("home", home)

    # Column widths: pad to fit both teams' values
    cols = ["OFF", "SP", "BULL", "DEF", "PLAT"]
    vals_a = list(a[1:])
    vals_h = list(h[1:])
    widths = [max(len(c), len(va), len(vh))
              for c, va, vh in zip(cols, vals_a, vals_h)]

    team_w = max(len(away), len(home), 4)

    def _row_str(label, vals):
        cells = [v.center(w) for v, w in zip(vals, widths)]
        return f"{label:<{team_w}}  " + "  ".join(cells)

    header_vals = [c.center(w) for c, w in zip(cols, widths)]
    header = f"{'':^{team_w}}  " + "  ".join(header_vals)
    sep    = "─" * len(header)

    lines = [
        header,
        sep,
        _row_str(away, vals_a),
        _row_str(home, vals_h),
    ]
    return "```\n" + "\n".join(lines) + "\n```"


# ── SP detail field ───────────────────────────────────────────────────────────

def _sp_detail_field(away: str, home: str, pitchers: dict) -> str:
    def _line(side, team):
        p = pitchers.get(side)
        if not p or not p.get("name"):
            return f"**{team}:** TBD"
        throws = p.get("throws") or "?"
        sc     = _sp_score(p)
        grade  = score_to_grade(sc) if sc is not None else "—"
        return f"**{team}  [{grade}]** — {p['name']} ({throws})"
    return _line("away", away) + "\n" + _line("home", home)


# ── Overall grade box ─────────────────────────────────────────────────────────

def _overall_box(away: str, home: str, away_grade: str, home_grade: str) -> str:
    """Monospace box — most visually distinct element in the embed."""
    inner_w = 28
    top     = "╔" + "═" * inner_w + "╗"
    bot     = "╚" + "═" * inner_w + "╝"
    mid_sep = "╠" + "═" * inner_w + "╣"
    title   = ("★  OVERALL GRADE  ★").center(inner_w)
    blank   = " " * inner_w
    grade_line = f"{away}  {away_grade}".ljust(inner_w // 2) + f"{home}  {home_grade}".rjust(inner_w // 2)
    lines = [
        top,
        f"║{title}║",
        f"║{blank}║",
        f"║{grade_line}║",
        f"║{blank}║",
        bot,
    ]
    return "```\n" + "\n".join(lines) + "\n```"


# ── Main embed builder ────────────────────────────────────────────────────────

def _build_embed(game: dict) -> dict:
    away     = game["away_team"]
    home     = game["home_team"]
    time_str = _to_et_str(game.get("first_pitch_utc", ""))
    pitchers = game.get("pitchers", {})
    tr       = game.get("team_ranks", {})
    bullpen  = game.get("bullpen", {})
    lineups  = game.get("lineups", {})
    status   = game.get("lineup_status", "projected")

    colour = 0x2ECC71 if status == "confirmed" else 0x3498DB

    def _ov_grade(side):
        t          = tr.get(side) or {}
        opp        = "home" if side == "away" else "away"
        opp_throws = (pitchers.get(opp) or {}).get("throws")
        lineup     = lineups.get(side, [])
        sp_sc      = _sp_score(pitchers.get(side))
        bp_sc      = _bp_score((bullpen.get(side) or {}))
        plat       = _platoon_score(lineup, opp_throws)
        sc = _overall_score(
            t.get("hitting_xwoba_rank"), sp_sc, bp_sc,
            t.get("defense_oaa_rank"), plat,
        )
        return score_to_grade(sc) if sc is not None else "—"

    away_ov = _ov_grade("away")
    home_ov = _ov_grade("home")

    fields = [
        {
            "name":   "📊  Matchup",
            "value":  _matchup_table(away, home, game),
            "inline": False,
        },
        {
            "name":   "⚾  Starting Pitchers",
            "value":  _sp_detail_field(away, home, pitchers),
            "inline": False,
        },
        {
            "name":   "\u200b",
            "value":  _overall_box(away, home, away_ov, home_ov),
            "inline": False,
        },
    ]

    return {
        "title":       f"{away} @ {home}  —  {time_str}",
        "description": _context_description(game, away, home),
        "color":       colour,
        "fields":      fields,
        "footer":      {"text": "MLB Daily Dashboard"},
    }


# ── Summary board ─────────────────────────────────────────────────────────────

_GRADE_ORDER = ["F", "D-", "D", "D+", "C-", "C", "C+", "B-", "B", "B+", "A-", "A", "A+"]

def _grade_num(g: str) -> int | None:
    try:   return _GRADE_ORDER.index(g)
    except ValueError: return None


def _summary_rec(game: dict) -> dict:
    """Betting recommendation for one game. Returns team/label/signal/ml/gap."""
    away    = game["away_team"]
    home    = game["home_team"]
    away_g  = _game_ov_grade(game, "away")
    home_g  = _game_ov_grade(game, "home")
    an, hn  = _grade_num(away_g), _grade_num(home_g)

    ml_data = (game.get("odds") or {}).get("moneyline") or {}
    away_ml = ml_data.get("away_ml")
    home_ml = ml_data.get("home_ml")

    if an is None or hn is None:
        return {"team": "—", "label": "—", "signal": "❓",
                "away_g": away_g, "home_g": home_g, "gap": None, "ml": None}

    gap = abs(an - hn)
    if gap >= 3:   signal = "🔥 STRONG"
    elif gap == 2: signal = "⭐⭐ LEAN"
    elif gap == 1: signal = "⭐ SLIGHT"
    else:          signal = "= TOSS-UP"

    if gap == 0:
        if away_ml is not None and home_ml is not None:
            team, team_ml = (away, away_ml) if away_ml >= home_ml else (home, home_ml)
        else:
            team, team_ml = "—", None
        label = f"{team}(dog)" if team != "—" else "—"
    else:
        team, team_ml = (away, away_ml) if an > hn else (home, home_ml)
        label = team

    if gap >= 3 and team_ml is not None and team_ml > -175:
        signal = "💎 VALUE"

    return {"team": team, "label": label, "signal": signal,
            "away_g": away_g, "home_g": home_g, "gap": gap, "ml": team_ml}


def _ev_side(game: dict, side: str) -> dict:
    """EV data for one side. Mirrors app.py logic."""
    pitchers = game.get("pitchers", {})
    tr       = game.get("team_ranks", {})
    bullpen  = game.get("bullpen", {})
    lineups  = game.get("lineups", {})

    def _sc(s):
        t   = (tr.get(s) or {})
        opp = "home" if s == "away" else "away"
        opp_t = (pitchers.get(opp) or {}).get("throws")
        sp = _sp_score(pitchers.get(s))
        bp = _bp_score((bullpen.get(s) or {}))
        pl = _platoon_score(lineups.get(s, []), opp_t)
        return _overall_score(t.get("hitting_xwoba_rank"), sp, bp, t.get("defense_oaa_rank"), pl)

    a_sc = _sc("away"); h_sc = _sc("home")
    if a_sc is None or h_sc is None or (a_sc + h_sc) == 0:
        return {}
    total = a_sc + h_sc
    model_p = (a_sc / total) if side == "away" else (h_sc / total)
    ml_data = (game.get("odds") or {}).get("moneyline") or {}
    ml      = ml_data.get(f"{side}_ml")
    impl    = ml_data.get(f"{side}_impl")
    if ml is None or impl is None:
        return {"our_prob": model_p}
    market_p = impl / 100.0
    edge     = model_p - market_p
    decimal  = (ml / 100 + 1) if ml > 0 else (100 / abs(ml) + 1)
    ev_pct   = (model_p * decimal - 1) * 100
    return {"our_prob": model_p, "market_prob": market_p,
            "edge": edge, "ev_pct": ev_pct, "ml": ml}


def _build_summary_embed(games: list[dict], date_str: str) -> dict:
    """
    Two-section embed:
      1. Best Bets board (ranked by confidence then EV)
      2. O/U leans field
    """
    from datetime import date as _date
    try:
        d = _date.fromisoformat(date_str)
        date_label = d.strftime("%a %b %-d")
    except Exception:
        date_label = date_str

    active = [g for g in games if g.get("status") not in ("final",)]

    if not active:
        return {
            "title":       f"📋 Betting Board — {date_label}",
            "description": "No upcoming games today.",
            "color":       0x2C3E50,
        }

    # ── Rank bets ────────────────────────────────────────────────────────────
    _CORD = {"💎 VALUE": 0, "🔥 STRONG": 1, "⭐⭐ LEAN": 2, "⭐ SLIGHT": 3, "= TOSS-UP": 4}
    ranked = []
    for game in active:
        rec  = _summary_rec(game)
        side = "away" if rec["team"] == game["away_team"] else "home"
        ev   = _ev_side(game, side)
        ranked.append((game, rec, ev))
    ranked.sort(key=lambda x: (_CORD.get(x[1]["signal"], 9), -(x[2].get("ev_pct") or -999)))

    # ── Best Bets board ───────────────────────────────────────────────────────
    col_t  = 6; col_m = 10; col_g = 4; col_b = 12; col_e = 7
    header = (f"{'TIME':<{col_t}} {'MATCHUP':<{col_m}} "
              f"{'AW':^{col_g}} {'HM':^{col_g}} "
              f"{'BET':<{col_b}} {'EV%':<{col_e}} SIG")
    sep    = "─" * len(header)
    rows   = [header, sep]

    for game, rec, ev in ranked:
        away = game["away_team"]; home = game["home_team"]
        ts   = _to_et_str(game.get("first_pitch_utc","")).replace(" PM","p").replace(" AM","a").replace(" ET","")
        ml   = rec["ml"]
        ml_s = (f"+{ml}" if ml > 0 else str(ml)) if ml is not None else ""
        bet  = f"{rec['label']} {ml_s}".strip()
        ev_p = ev.get("ev_pct")
        ev_s = (f"+{ev_p:.1f}%" if ev_p >= 0 else f"{ev_p:.1f}%") if ev_p is not None else "—"
        sig  = rec["signal"].replace(" STRONG","").replace(" LEAN","").replace(" SLIGHT","").replace(" TOSS-UP","")
        rows.append(
            f"{ts:<{col_t}} {f'{away}@{home}':<{col_m}} "
            f"{rec['away_g']:^{col_g}} {rec['home_g']:^{col_g}} "
            f"{bet:<{col_b}} {ev_s:<{col_e}} {sig}"
        )

    board = "```\n" + "\n".join(rows) + "\n```"

    # ── O/U leans field ───────────────────────────────────────────────────────
    ou_lines = []
    for game in active:
        away = game["away_team"]; home = game["home_team"]
        ou   = _ou_model(game)
        line = ou.get("posted_line"); mt = ou.get("model_total")
        lean = ou.get("lean","—");   conf = ou.get("conf","—")
        if lean in ("OVER","UNDER") and line is not None:
            diff  = ou.get("diff",0)
            diff_s = f"+{diff}" if diff >= 0 else str(diff)
            note_s = "  ".join(ou.get("notes",[]))
            icon   = "🔺" if lean == "OVER" else "🔻"
            conf_s = f"({conf})" if conf != "—" else ""
            ou_lines.append(
                f"`{away}@{home}` {icon} **{lean} {line}** · model {mt} ({diff_s})  {note_s} {conf_s}"
            )

    ou_text = "\n".join(ou_lines) if ou_lines else "No O/U lines available yet."

    # ── Summary note ──────────────────────────────────────────────────────────
    value  = sum(1 for _,r,_ in ranked if r["signal"] == "💎 VALUE")
    strong = sum(1 for _,r,_ in ranked if r["signal"] in ("💎 VALUE","🔥 STRONG"))
    note_p = []
    if value:  note_p.append(f"{value} 💎 value")
    if strong: note_p.append(f"{strong} 🔥 strong")
    highlights = "  ·  ".join(note_p) if note_p else "No strong plays today"

    return {
        "title":       f"📋 Betting Board — {date_label}",
        "description": board,
        "color":       0x2C3E50,
        "fields": [
            {"name": "📊 O/U Leans", "value": ou_text,    "inline": False},
            {"name": "Highlights",   "value": highlights,  "inline": False},
        ],
        "footer": {"text": "EV%=(our_prob×decimal_odds)-1  ·  💎VALUE  🔥STRONG  ⭐⭐LEAN  ⭐SLIGHT  =TOSS-UP"},
    }


def post_summary(date: str | None = None, force: bool = False) -> None:
    """Post the betting board embed to Discord."""
    if date is None:
        date = _utc_now().strftime("%Y-%m-%d")

    snapshot_path = DATA_DIR / f"{date}.json"
    if not snapshot_path.exists():
        print(f"No snapshot for {date} — run the snapshot builder first.")
        return

    snapshot = json.loads(snapshot_path.read_text())
    games    = snapshot.get("games", [])

    embed = _build_summary_embed(games, date)
    print(f"  Posting betting board for {date}…")
    if _post_embed(embed):
        print("  ✓ Betting board posted.")
    else:
        print("  ✗ Failed to post betting board.")


# ── Post + main ───────────────────────────────────────────────────────────────

def _post_embed(embed: dict) -> bool:
    if not WEBHOOK_URL:
        print("  DISCORD_WEBHOOK_URL not set — skipping.")
        return False
    try:
        resp = requests.post(WEBHOOK_URL, json={"embeds": [embed]}, timeout=10)
        if resp.status_code in (200, 204):
            return True
        print(f"  Discord returned {resp.status_code}: {resp.text[:200]}")
        return False
    except Exception as exc:
        print(f"  Discord post failed: {exc}")
        return False


def notify_upcoming_games(
    date: str | None = None,
    post_all: bool   = False,
    force: bool      = False,
    morning: bool    = False,   # morning briefing — posts all but doesn't block T-60 cards
) -> None:
    if date is None:
        date = _utc_now().strftime("%Y-%m-%d")

    snapshot_path = DATA_DIR / f"{date}.json"
    if not snapshot_path.exists():
        print(f"No snapshot for {date} — run the snapshot builder first.")
        return

    snapshot = json.loads(snapshot_path.read_text())
    games    = snapshot.get("games", [])

    # Morning briefing uses a separate tracker so T-60 cards still fire later
    if morning:
        sent_path = DATA_DIR / f"discord_morning_{date}.json"
    else:
        sent_path = DATA_DIR / f"discord_sent_{date}.json"

    already_sent: set[int] = set()
    if sent_path.exists() and not force:
        already_sent = set(json.loads(sent_path.read_text()))

    # Morning briefing: post the summary board first
    if morning:
        embed = _build_summary_embed(games, date)
        print("  Posting morning betting board…")
        _post_embed(embed)

    now    = _utc_now()
    posted: list[int] = []

    for game in games:
        pk = game["game_pk"]
        if pk in already_sent:
            print(f"  Skip (already sent): {game['away_team']} @ {game['home_team']}")
            continue
        if not post_all and not morning:
            fp = _parse_utc(game.get("first_pitch_utc",""))
            if fp is None:
                continue
            mins = (fp - now).total_seconds() / 60
            if not (NOTIFY_LATE <= mins <= NOTIFY_EARLY):
                continue

        away = game["away_team"]
        home = game["home_team"]
        print(f"  Posting: {away} @ {home}  ({_to_et_str(game.get('first_pitch_utc',''))})")

        if _post_embed(_build_embed(game)):
            posted.append(pk)
            already_sent.add(pk)
        else:
            print(f"  Failed: {away} @ {home}")

    if posted:
        sent_path.write_text(json.dumps(sorted(already_sent), indent=2))
        print(f"\n  ✓ Posted {len(posted)} game card(s).")
    else:
        print("  Nothing posted (time window miss, all sent, or no data).")


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Send Discord game report cards.")
    parser.add_argument("--date",    default=None,        help="YYYY-MM-DD (default: today)")
    parser.add_argument("--all",     action="store_true", help="Post all games (does NOT block T-60 cards)")
    parser.add_argument("--morning", action="store_true", help="Morning briefing — summary board + all games, separate tracker")
    parser.add_argument("--summary", action="store_true", help="Post only the betting board summary embed")
    parser.add_argument("--force",   action="store_true", help="Re-post already-sent games")
    args = parser.parse_args()
    print(f"Discord notifier — {args.date or 'today'}")

    if args.summary:
        post_summary(args.date, force=args.force)
    else:
        notify_upcoming_games(args.date, post_all=args.all, force=args.force, morning=args.morning)
