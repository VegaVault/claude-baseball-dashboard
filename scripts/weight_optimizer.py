"""
Weight optimizer: find which component weights best predict game outcomes.

For each resolved pick in picks_2026.json, re-loads the snapshot for that
date, computes raw component scores (off, sp, bp, def, plat) for both sides,
then runs a grid search over weight combinations to see what maximizes ROI.

Also shows a correlation table — which single component is most predictive.

Run:
    python scripts/weight_optimizer.py
    python scripts/weight_optimizer.py --year 2026
"""

import json
import sys
import itertools
from pathlib import Path

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8")

from src.fetch.labels import rank_to_score, score_to_grade
from src.build.picks_tracker import (
    _sp_score, _bp_score, _platoon_score, _grade_num, BET_SIZE
)

DATA_DIR = ROOT / "data"

# ── Component extractor ───────────────────────────────────────────────────────

def _component_scores(game: dict, side: str) -> dict:
    """Return raw 0-1 scores for each component, None if unavailable."""
    pitchers = game.get("pitchers", {})
    tr       = (game.get("team_ranks", {}) or {}).get(side) or {}
    bp       = (game.get("bullpen",    {}) or {}).get(side) or {}
    lineup   = (game.get("lineups",    {}) or {}).get(side, [])
    opp      = "home" if side == "away" else "away"
    opp_throws = (pitchers.get(opp) or {}).get("throws")

    sp_sc = _sp_score(pitchers.get(side))
    bp_sc = _bp_score(bp)
    pl_sc = _platoon_score(lineup, opp_throws)

    off_r = tr.get("hitting_xwoba_rank")
    def_r = tr.get("defense_oaa_rank")

    return {
        "off":  rank_to_score(off_r),   # 0-1, 1=best offense
        "sp":   sp_sc,                   # 0-1, 1=best SP
        "bp":   bp_sc,                   # 0-1, 1=best BP
        "def":  rank_to_score(def_r),    # 0-1, 1=best defense
        "plat": pl_sc,                   # 0-1, 1=best platoon adv
    }


def _weighted_score(comp: dict, w: dict) -> float | None:
    """Compute weighted score given component dict and weight dict."""
    vals, weights = [], []
    # Combine SP+BP into pitching
    sp = comp.get("sp"); bp_s = comp.get("bp")
    if sp is not None and bp_s is not None:
        pitch = sp * 0.70 + bp_s * 0.30
    else:
        pitch = sp or bp_s

    for key, val, weight in [
        ("off",   comp.get("off"), w["off"]),
        ("pitch", pitch,           w["pitch"]),
        ("def",   comp.get("def"), w["def"]),
        ("plat",  comp.get("plat"),w["plat"]),
    ]:
        if val is not None:
            vals.append(val * weight)
            weights.append(weight)
    return sum(vals) / sum(weights) if weights else None


def _pnl(ml, result):
    if ml is None or result not in ("win","loss"):
        return None
    decimal = (ml / 100 + 1) if ml > 0 else (100 / abs(ml) + 1)
    return round((decimal - 1) * BET_SIZE, 2) if result == "win" else -BET_SIZE


# ── Load resolved picks with component data ───────────────────────────────────

def load_resolved_with_components(year: int) -> list[dict]:
    picks_path = DATA_DIR / f"picks_{year}.json"
    if not picks_path.exists():
        print(f"No picks file for {year}")
        return []

    picks = json.loads(picks_path.read_text())["picks"]
    resolved = [p for p in picks if p["result"] in ("win","loss") and p.get("ml") is not None]

    # Index snapshots by date
    snap_cache: dict[str, dict] = {}

    rows = []
    missing_snap = 0

    for pick in resolved:
        date = pick["date"]
        if date not in snap_cache:
            snap_path = DATA_DIR / f"{date}.json"
            if not snap_path.exists():
                missing_snap += 1
                continue
            snap_cache[date] = json.loads(snap_path.read_text())

        snap  = snap_cache[date]
        game  = next((g for g in snap.get("games",[])
                      if g["game_pk"] == pick["game_pk"]), None)
        if game is None:
            missing_snap += 1
            continue

        a_comp = _component_scores(game, "away")
        h_comp = _component_scores(game, "home")

        # Component differentials (pick_side - opp_side, positive = pick has edge)
        pick_side = "away" if pick["pick_team"] == pick["away_team"] else "home"
        opp_side  = "home" if pick_side == "away" else "away"

        p_comp = a_comp if pick_side == "away" else h_comp
        o_comp = h_comp if pick_side == "away" else a_comp

        diffs = {}
        for key in ("off","sp","bp","def","plat"):
            pv = p_comp.get(key); ov = o_comp.get(key)
            diffs[key] = (pv - ov) if (pv is not None and ov is not None) else None

        rows.append({
            "date":      date,
            "game_pk":   pick["game_pk"],
            "away":      pick["away_team"],
            "home":      pick["home_team"],
            "pick_team": pick["pick_team"],
            "pick_side": pick_side,
            "signal":    pick["signal"],
            "gap":       pick["gap"],
            "ml":        pick["ml"],
            "result":    pick["result"],
            "won":       1 if pick["result"] == "win" else 0,
            "pnl":       pick.get("pnl") or _pnl(pick["ml"], pick["result"]),
            "a_comp":    a_comp,
            "h_comp":    h_comp,
            "p_comp":    p_comp,
            "o_comp":    o_comp,
            "diffs":     diffs,
        })

    print(f"Loaded {len(rows)} resolved picks with component data "
          f"({missing_snap} skipped — missing snapshot)")
    return rows


# ── Correlation analysis ───────────────────────────────────────────────────────

def correlation_analysis(rows: list[dict]) -> None:
    """Show win rate when pick has positive/negative edge in each component."""
    print("\n" + "="*62)
    print("COMPONENT CORRELATION  (win rate when pick has +/- edge)")
    print("="*62)
    print(f"{'Component':<10} {'n(+edge)':>9} {'WR(+edge)':>10} {'n(-edge)':>9} {'WR(-edge)':>10} {'Δ':>6}")
    print("─"*62)

    components = [
        ("off",   "Offense"),
        ("sp",    "SP"),
        ("bp",    "Bullpen"),
        ("def",   "Defense"),
        ("plat",  "Platoon"),
    ]
    for key, label in components:
        pos = [r for r in rows if (r["diffs"].get(key) or 0) >  0.01]
        neg = [r for r in rows if (r["diffs"].get(key) or 0) < -0.01]
        wr_pos = sum(r["won"] for r in pos) / len(pos) * 100 if pos else 0
        wr_neg = sum(r["won"] for r in neg) / len(neg) * 100 if neg else 0
        delta  = wr_pos - wr_neg
        print(f"{label:<10} {len(pos):>9} {wr_pos:>9.1f}% {len(neg):>9} {wr_neg:>9.1f}% {delta:>+5.1f}%")

    print()
    print("Higher Δ = component is more predictive of pick winning.")


# ── Grid search ───────────────────────────────────────────────────────────────

WEIGHT_OPTIONS = [0.0, 0.10, 0.20, 0.30, 0.40, 0.50, 0.60, 0.70]

def grid_search(rows: list[dict], top_n: int = 15) -> None:
    print("\n" + "="*72)
    print("WEIGHT GRID SEARCH  (gap≥2 picks only, skip gap=1)")
    print("="*72)

    # Filter to same picks we'd take under current rules (gap≠1)
    eligible = [r for r in rows if r.get("gap") != 1]
    print(f"Eligible picks for grid search: {len(eligible)}")
    print(f"Current model  ROI: {sum(r['pnl'] for r in eligible if r['pnl'] is not None):+.2f}  "
          f"WR: {sum(r['won'] for r in eligible)/len(eligible)*100:.1f}%\n")
    print("Searching weight combos (off / pitch / def / plat)…")

    results = []

    # Generate all combos that sum to 1.0 (within tolerance)
    combos_checked = 0
    for w_off in WEIGHT_OPTIONS:
        for w_pitch in WEIGHT_OPTIONS:
            for w_def in WEIGHT_OPTIONS:
                w_plat = round(1.0 - w_off - w_pitch - w_def, 2)
                if w_plat < 0 or w_plat > 0.50:
                    continue
                w = {"off": w_off, "pitch": w_pitch, "def": w_def, "plat": w_plat}
                combos_checked += 1

                wins = losses = 0
                total_pnl = 0.0
                skipped = 0

                for r in eligible:
                    p_sc = _weighted_score(r["p_comp"], w)
                    o_sc = _weighted_score(r["o_comp"], w)
                    if p_sc is None or o_sc is None or (p_sc + o_sc) == 0:
                        skipped += 1
                        continue

                    # With new weights, does the model still pick the same team?
                    new_pick_side = "away" if (
                        (_weighted_score(r["a_comp"], w) or 0) >
                        (_weighted_score(r["h_comp"], w) or 0)
                    ) else "home"

                    # For TOSS-UP (gap=0) we always pick the dog — side doesn't change
                    if r["gap"] == 0:
                        new_pick_side = r["pick_side"]  # dog stays the dog

                    won = 1 if new_pick_side == r["pick_side"] and r["won"] == 1 else (
                          0 if new_pick_side == r["pick_side"] else
                          (1 if r["won"] == 0 else 0)  # flipped pick
                    )
                    # Actually recompute properly:
                    # If we're still picking the same team → same result
                    # If we flipped → opposite result
                    if new_pick_side == r["pick_side"]:
                        result = r["result"]
                    else:
                        result = "win" if r["result"] == "loss" else "loss"
                    pnl = _pnl(r["ml"], result)
                    if pnl is None:
                        skipped += 1; continue
                    if result == "win":  wins += 1
                    else:               losses += 1
                    total_pnl += pnl

                n = wins + losses
                if n < 50:
                    continue  # too few picks — skip
                wr  = wins / n * 100
                roi = total_pnl / (n * BET_SIZE) * 100
                results.append((roi, wr, n, total_pnl, w_off, w_pitch, w_def, w_plat))

    results.sort(reverse=True)

    print(f"Combos checked: {combos_checked}  |  Valid (n≥50): {len(results)}\n")
    print(f"{'Rank':<5} {'OFF':>5} {'PITCH':>6} {'DEF':>5} {'PLAT':>5} "
          f"{'n':>5} {'WR':>7} {'ROI':>7} {'P&L':>9}")
    print("─"*60)
    for i, (roi, wr, n, pnl, wo, wp, wd, wpl) in enumerate(results[:top_n], 1):
        print(f"{i:<5} {wo:>5.0%} {wp:>6.0%} {wd:>5.0%} {wpl:>5.0%} "
              f"{n:>5} {wr:>6.1f}% {roi:>+6.1f}% {pnl:>+9.2f}")

    # Current weights baseline
    current_w = {"off": 0.50, "pitch": 0.30, "def": 0.15, "plat": 0.05}
    current_entry = next(
        (r for r in results
         if abs(r[4]-0.50)<0.01 and abs(r[5]-0.30)<0.01 and abs(r[6]-0.15)<0.01),
        None
    )
    if current_entry:
        idx = results.index(current_entry) + 1
        print(f"\nCurrent weights (50/30/15/5) rank #{idx} of {len(results)} valid combos  "
              f"ROI: {current_entry[0]:+.1f}%  WR: {current_entry[1]:.1f}%")


# ── Main ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--year", type=int, default=2026)
    parser.add_argument("--top",  type=int, default=15, help="Top N combos to show")
    args = parser.parse_args()

    rows = load_resolved_with_components(args.year)
    if not rows:
        sys.exit(1)

    correlation_analysis(rows)
    grid_search(rows, top_n=args.top)
