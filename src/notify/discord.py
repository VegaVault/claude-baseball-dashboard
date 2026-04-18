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
    parser.add_argument("--morning", action="store_true", help="Morning briefing — all games, separate tracker")
    parser.add_argument("--force",   action="store_true", help="Re-post already-sent games")
    args = parser.parse_args()
    print(f"Discord notifier — {args.date or 'today'}")
    notify_upcoming_games(args.date, post_all=args.all, force=args.force, morning=args.morning)
