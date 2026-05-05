"""
Picks tracker: records daily bets and resolves results as games go final.

Persists to data/picks_YYYY.json.
Schema:
  {
    "bankroll_start": 10000,
    "bet_size":       100,
    "picks": [
      {
        "game_pk":     int,
        "date":        "YYYY-MM-DD",
        "away_team":   str,
        "home_team":   str,
        "pick_team":   str,
        "signal":      "🔥 STRONG" | "⭐⭐ LEAN" | "⭐ SLIGHT" | "= TOSS-UP",
        "gap":         int,
        "ml":          int | null,
        "ev_pct":      float | null,
        "away_grade":  str,
        "home_grade":  str,
        "result":      "pending" | "win" | "loss",
        "pnl":         float | null,
        "away_score":  int | null,
        "home_score":  int | null,
        "recorded_at": str (UTC ISO),
        "resolved_at": str | null
      }
    ]
  }

P&L: flat $100 bet.
  Win  → +(decimal_odds - 1) × 100
  Loss → -100
"""

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

try:
    from src.fetch.labels import rank_to_score, score_to_grade
except ModuleNotFoundError:
    import sys, os
    sys.path.insert(0, str(Path(__file__).parents[2]))
    from src.fetch.labels import rank_to_score, score_to_grade

logger = logging.getLogger(__name__)

DATA_DIR       = Path(__file__).parents[2] / "data"
BET_SIZE       = 100.0
BANKROLL_START = 10_000.0
_COMPRESS      = 0.55   # win-prob compression factor (mirrors app.py / discord.py)

_SIGNAL_ORDER = ["🔥 STRONG", "⭐⭐ LEAN", "⭐ SLIGHT", "= TOSS-UP"]
_GRADE_ORDER  = ["F","D-","D","D+","C-","C","C+","B-","B","B+","A-","A","A+"]


# ── Internal grade / score helpers (mirrors discord.py) ───────────────────────

def _grade_num(g: str):
    try:   return _GRADE_ORDER.index(g)
    except ValueError: return None


def _sp_score(p: dict | None) -> float | None:
    if not p: return None
    cy = p.get("current_year") or {}
    xp, fp = cy.get("xwoba_percentile"), cy.get("fip_percentile")
    if xp is not None and fp is not None:
        return (xp * 0.667 + fp * 0.333) / 100
    return (xp / 100) if xp is not None else ((fp / 100) if fp is not None else None)


def _bp_score(bp: dict | None) -> float | None:
    if not bp: return None
    xp, fp = bp.get("xwoba_percentile"), bp.get("fip_percentile")
    if xp is not None and fp is not None:
        return (xp * 0.667 + fp * 0.333) / 100
    return (xp / 100) if xp is not None else ((fp / 100) if fp is not None else None)


def _platoon_score(lineup: list, opp_throws: str | None) -> float | None:
    if not lineup or not opp_throws or opp_throws == "?":
        return None
    scores = [
        0.5 if (b.get("bats") or "?").upper() in ("S", "?")
        else (1.0 if (b.get("bats") or "?").upper() != opp_throws.upper() else 0.0)
        for b in lineup
    ]
    return sum(scores) / len(scores) if scores else None


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
            scores.append(val * w)
            weights.append(w)
    return sum(scores) / sum(weights) if scores else None


def _side_score(game: dict, side: str) -> float | None:
    pitchers  = game.get("pitchers", {})
    tr        = (game.get("team_ranks", {}) or {}).get(side) or {}
    bp        = (game.get("bullpen",    {}) or {}).get(side) or {}
    lineup    = (game.get("lineups",    {}) or {}).get(side, [])
    opp       = "home" if side == "away" else "away"
    opp_throws = (pitchers.get(opp) or {}).get("throws")
    return _overall_score(
        tr.get("hitting_xwoba_rank"),
        _sp_score(pitchers.get(side)),
        _bp_score(bp),
        tr.get("defense_oaa_rank"),
        _platoon_score(lineup, opp_throws),
    )


# ── Pick computation ──────────────────────────────────────────────────────────

def _compute_pick(game: dict) -> dict | None:
    """
    Determine the pick for a game.
    Returns a dict with pick details, or None if grades are unavailable.
    """
    away = game["away_team"]
    home = game["home_team"]

    a_sc = _side_score(game, "away")
    h_sc = _side_score(game, "home")

    away_g = score_to_grade(a_sc) if a_sc is not None else "—"
    home_g = score_to_grade(h_sc) if h_sc is not None else "—"
    an, hn = _grade_num(away_g), _grade_num(home_g)

    if an is None or hn is None:
        return None

    ml_data  = (game.get("odds") or {}).get("moneyline") or {}
    away_ml  = ml_data.get("away_ml")
    home_ml  = ml_data.get("home_ml")

    gap = abs(an - hn)
    if gap >= 3:   signal = "🔥 STRONG"
    elif gap == 2: signal = "⭐⭐ LEAN"
    elif gap == 1: signal = "⭐ SLIGHT"
    else:          signal = "= TOSS-UP"

    # ── Pick team ─────────────────────────────────────────────────────────────
    if gap == 0:
        # TOSS-UP: bet the dog (higher ML = better payout)
        if away_ml is not None and home_ml is not None:
            pick_team = away if away_ml >= home_ml else home
            pick_ml   = max(away_ml, home_ml)
        else:
            return None  # no odds — can't determine dog
    else:
        pick_team = away if an > hn else home
        pick_ml   = away_ml if pick_team == away else home_ml

    # ── EV calculation (compressed win prob) ─────────────────────────────────
    ev_pct = None
    if pick_ml is not None and a_sc is not None and h_sc is not None:
        total   = a_sc + h_sc
        if total > 0:
            pick_side = "away" if pick_team == away else "home"
            raw_p     = (a_sc / total) if pick_side == "away" else (h_sc / total)
            model_p   = 0.5 + (raw_p - 0.5) * _COMPRESS
            pick_impl = ml_data.get(f"{pick_side}_impl")
            if pick_impl is not None:
                decimal = (pick_ml / 100 + 1) if pick_ml > 0 else (100 / abs(pick_ml) + 1)
                ev_pct  = round((model_p * decimal - 1) * 100, 2)

    return {
        "pick_team":  pick_team,
        "signal":     signal,
        "gap":        gap,
        "ml":         pick_ml,
        "ev_pct":     ev_pct,
        "away_grade": away_g,
        "home_grade": home_g,
    }


# ── P&L ───────────────────────────────────────────────────────────────────────

def _pnl(ml: int | None, result: str) -> float | None:
    """$100 flat bet P&L."""
    if result not in ("win", "loss") or ml is None:
        return None
    decimal = (ml / 100 + 1) if ml > 0 else (100 / abs(ml) + 1)
    return round((decimal - 1) * BET_SIZE, 2) if result == "win" else -BET_SIZE


# ── Persistence ───────────────────────────────────────────────────────────────

def _now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def load_picks(year: int) -> dict:
    path = DATA_DIR / f"picks_{year}.json"
    if path.exists():
        return json.loads(path.read_text())
    return {"bankroll_start": BANKROLL_START, "bet_size": BET_SIZE, "picks": []}


def save_picks(data: dict, year: int) -> None:
    DATA_DIR.mkdir(exist_ok=True)
    (DATA_DIR / f"picks_{year}.json").write_text(json.dumps(data, indent=2))


# ── Public API ────────────────────────────────────────────────────────────────

def record_and_resolve(games: list[dict], date_str: str) -> None:
    """
    Called after each snapshot build.
    1. Resolve result for any pending picks whose game is now final.
    2. Record new picks for upcoming (non-final, non-in-progress) games.
    """
    year  = int(date_str[:4])
    data  = load_picks(year)
    picks = data["picks"]
    existing_pks = {p["game_pk"] for p in picks}
    changed = False

    for game in games:
        pk     = game["game_pk"]
        status = game.get("status", "")

        # ── 1. Resolve ────────────────────────────────────────────────────────
        if status == "final":
            for pick in picks:
                if pick["game_pk"] == pk and pick["result"] == "pending":
                    # final_score may be a dict {"away":5,"home":3} OR a
                    # legacy string "5-3" from schedule.py — handle both
                    raw_score = game.get("final_score")
                    if isinstance(raw_score, dict):
                        a_s = raw_score.get("away")
                        h_s = raw_score.get("home")
                    elif isinstance(raw_score, str) and "-" in str(raw_score):
                        try:
                            parts = str(raw_score).split("-")
                            a_s, h_s = int(parts[0]), int(parts[1])
                        except (ValueError, IndexError):
                            a_s = h_s = None
                    else:
                        a_s = h_s = None
                    if a_s is None or h_s is None:
                        continue  # score not recorded yet
                    winner = game["away_team"] if a_s > h_s else game["home_team"]
                    result = "win" if pick["pick_team"] == winner else "loss"
                    pick.update({
                        "result":      result,
                        "pnl":         _pnl(pick["ml"], result),
                        "away_score":  a_s,
                        "home_score":  h_s,
                        "resolved_at": _now_utc(),
                    })
                    changed = True
                    logger.info(
                        "Resolved: %s @ %s → pick=%s result=%s",
                        game["away_team"], game["home_team"],
                        pick["pick_team"], result,
                    )

        # ── 2. Record new pick ────────────────────────────────────────────────
        if pk not in existing_pks and status not in ("final", "in_progress"):
            pd_ = _compute_pick(game)
            if pd_ is None:
                logger.debug("No pick for %s @ %s (grades unavailable)", game["away_team"], game["home_team"])
                continue
            picks.append({
                "game_pk":     pk,
                "date":        date_str,
                "away_team":   game["away_team"],
                "home_team":   game["home_team"],
                "pick_team":   pd_["pick_team"],
                "signal":      pd_["signal"],
                "gap":         pd_["gap"],
                "ml":          pd_["ml"],
                "ev_pct":      pd_["ev_pct"],
                "away_grade":  pd_["away_grade"],
                "home_grade":  pd_["home_grade"],
                "result":      "pending",
                "pnl":         None,
                "away_score":  None,
                "home_score":  None,
                "recorded_at": _now_utc(),
                "resolved_at": None,
            })
            changed = True
            logger.info(
                "Recorded: %s @ %s → %s (%s)  ML=%s  EV=%s%%",
                game["away_team"], game["home_team"],
                pd_["pick_team"], pd_["signal"],
                pd_["ml"], pd_["ev_pct"],
            )

    if changed:
        save_picks(data, year)
        logger.info("picks_%d.json updated — %d total picks", year, len(picks))


def get_stats(year: int) -> dict:
    """
    Load picks for year and return aggregated stats dict:
      {by_signal, signal_order, total_bets, total_wins, total_losses,
       total_pending, total_pnl, win_pct, bankroll, picks}
    """
    data  = load_picks(year)
    picks = data.get("picks", [])

    by_signal: dict[str, dict] = {
        s: {"bets": 0, "wins": 0, "losses": 0, "pending": 0, "pnl": 0.0}
        for s in _SIGNAL_ORDER
    }

    for p in picks:
        sig = p.get("signal", "")
        if sig not in by_signal:
            continue
        d = by_signal[sig]
        if p["result"] == "pending":
            d["pending"] += 1
        elif p["result"] == "win":
            d["bets"] += 1; d["wins"] += 1
            d["pnl"] += p.get("pnl") or 0
        elif p["result"] == "loss":
            d["bets"] += 1; d["losses"] += 1
            d["pnl"] += p.get("pnl") or 0

    for d in by_signal.values():
        d["win_pct"] = (d["wins"] / d["bets"] * 100) if d["bets"] > 0 else None

    total_bets    = sum(d["bets"]    for d in by_signal.values())
    total_wins    = sum(d["wins"]    for d in by_signal.values())
    total_losses  = sum(d["losses"]  for d in by_signal.values())
    total_pending = sum(d["pending"] for d in by_signal.values())
    total_pnl     = sum(d["pnl"]     for d in by_signal.values())

    return {
        "by_signal":     by_signal,
        "signal_order":  _SIGNAL_ORDER,
        "total_bets":    total_bets,
        "total_wins":    total_wins,
        "total_losses":  total_losses,
        "total_pending": total_pending,
        "total_pnl":     round(total_pnl, 2),
        "win_pct":       round(total_wins / total_bets * 100, 1) if total_bets > 0 else None,
        "bankroll":      round(BANKROLL_START + total_pnl, 2),
        "picks":         picks,
    }


if __name__ == "__main__":
    import sys
    import logging
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    year = int(sys.argv[1]) if len(sys.argv) > 1 else 2025
    stats = get_stats(year)
    print(f"\nPick Tracker — {year}")
    print(f"Bankroll: ${stats['bankroll']:,.2f}  (P&L: ${stats['total_pnl']:+,.2f})")
    print(f"Record:   {stats['total_wins']}-{stats['total_losses']}  "
          f"({stats['win_pct']}%)  {stats['total_pending']} pending\n")
    print(f"{'Signal':<14} {'Bets':>4} {'W':>3} {'L':>3} {'Pend':>4} {'Win%':>6} {'P&L':>9}")
    print("─" * 50)
    for sig in stats["signal_order"]:
        d = stats["by_signal"][sig]
        wp = f"{d['win_pct']:.1f}%" if d["win_pct"] is not None else "—"
        print(f"{sig:<14} {d['bets']:>4} {d['wins']:>3} {d['losses']:>3} "
              f"{d['pending']:>4} {wp:>6} {d['pnl']:>+9.2f}")
