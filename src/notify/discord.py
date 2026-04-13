"""
Discord notifier: posts game cards as embeds via webhook.

For each game, if now falls inside the T-75 → T-45 window (i.e. 45–75 minutes
before first pitch) AND we haven't posted yet today, post a game card.
Tracks sent game_pks in data/discord_sent_YYYY-MM-DD.json.

Run via:  python -m src.notify.discord
"""

import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

# ── Path setup so this runs standalone ────────────────────────────────────────
ROOT = Path(__file__).parents[2]
DATA_DIR = ROOT / "data"

try:
    from src.fetch.labels import rank_to_grade, overall_grade
except ImportError:
    sys.path.insert(0, str(ROOT))
    from src.fetch.labels import rank_to_grade, overall_grade

# ── Constants ─────────────────────────────────────────────────────────────────
WEBHOOK_URL  = os.getenv("DISCORD_WEBHOOK_URL", "")
NOTIFY_EARLY = 75   # minutes before first pitch — start of window
NOTIFY_LATE  = 45   # minutes before first pitch — end of window (miss if past this)
ET_OFFSET    = timedelta(hours=-4)   # EDT; -5 during EST (good enough for game times)


# ── Helpers ───────────────────────────────────────────────────────────────────

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
    h = int(et.strftime("%I"))        # 01-12
    m = et.strftime("%M")
    ampm = et.strftime("%p")
    return f"{h}:{m} {ampm} ET"


def _fmt(val, decimals: int = 3) -> str:
    return "—" if val is None else f"{val:.{decimals}f}"


def _fmt_int(val) -> str:
    return "—" if val is None else str(int(val))


def _pitcher_line(pitcher: dict | None) -> str:
    """One-liner summary for a starting pitcher."""
    if not pitcher or not pitcher.get("name"):
        return "TBD"
    cy = pitcher.get("current_year") or {}
    name   = pitcher["name"]
    throws = pitcher.get("throws") or "?"
    ip     = _fmt(cy.get("ip"), 1)
    fip    = _fmt(cy.get("fip"), 2)
    xwoba  = _fmt(cy.get("xwoba"), 3)
    label  = cy.get("xwoba_label") or ""
    flag   = " ⚠" if label and not cy.get("qualified", True) else ""
    return f"**{name}** ({throws})  IP {ip} · FIP {fip} · xwOBA {xwoba} *{label}{flag}*"


def _grade_line(team_ranks: dict | None) -> str:
    """Compact grade string: Off A · Pit B+ · Def C · Overall B"""
    if not team_ranks:
        return "—"
    h = team_ranks.get("hitting_xwoba_rank")
    p = team_ranks.get("pitching_xwoba_against_rank")
    d = team_ranks.get("defense_oaa_rank")
    ov = overall_grade(h, p, d)
    return (
        f"Off **{rank_to_grade(h)}** · "
        f"Pit **{rank_to_grade(p)}** · "
        f"Def **{rank_to_grade(d)}** · "
        f"Overall **{ov}**"
    )


def _lineup_badge(status: str) -> str:
    return {"confirmed": "🟢 Confirmed", "projected": "🟡 Projected", "frozen": "🔵 Frozen"}.get(
        status, f"⚪ {status.title()}"
    )


def _build_embed(game: dict) -> dict:
    """Build a Discord embed dict for one game."""
    away  = game["away_team"]
    home  = game["home_team"]
    time  = _to_et_str(game.get("first_pitch_utc", ""))
    pitchers = game.get("pitchers", {})
    tr       = game.get("team_ranks", {})
    lineup_status = game.get("lineup_status", "projected")

    # Colour: green if confirmed lineups, otherwise blue
    colour = 0x2ECC71 if lineup_status == "confirmed" else 0x3498DB

    fields = [
        {
            "name": f"⚾ {away} SP",
            "value": _pitcher_line(pitchers.get("away")),
            "inline": False,
        },
        {
            "name": f"⚾ {home} SP",
            "value": _pitcher_line(pitchers.get("home")),
            "inline": False,
        },
        {
            "name": f"📊 {away}",
            "value": _grade_line(tr.get("away")),
            "inline": False,
        },
        {
            "name": f"📊 {home}",
            "value": _grade_line(tr.get("home")),
            "inline": False,
        },
        {
            "name": "📋 Lineup",
            "value": _lineup_badge(lineup_status),
            "inline": True,
        },
    ]

    return {
        "title": f"{away} @ {home}  —  {time}",
        "color": colour,
        "fields": fields,
        "footer": {"text": "MLB Daily Dashboard · T-60"},
    }


def _post_embed(embed: dict) -> bool:
    """POST a single embed to the webhook. Returns True on success."""
    if not WEBHOOK_URL:
        print("  DISCORD_WEBHOOK_URL not set — skipping post.")
        return False
    try:
        resp = requests.post(
            WEBHOOK_URL,
            json={"embeds": [embed]},
            timeout=10,
        )
        if resp.status_code in (200, 204):
            return True
        print(f"  Discord returned {resp.status_code}: {resp.text[:200]}")
        return False
    except Exception as exc:
        print(f"  Discord post failed: {exc}")
        return False


# ── Main logic ────────────────────────────────────────────────────────────────

def notify_upcoming_games(date: str | None = None) -> None:
    """
    Post Discord embeds for games in the T-75 → T-45 window.

    Args:
        date: YYYY-MM-DD string. Defaults to today (UTC).
    """
    if date is None:
        date = _utc_now().strftime("%Y-%m-%d")

    snapshot_path = DATA_DIR / f"{date}.json"
    if not snapshot_path.exists():
        print(f"No snapshot for {date} — nothing to post.")
        return

    snapshot = json.loads(snapshot_path.read_text())
    games    = snapshot.get("games", [])

    # Load (or create) the sent-tracker for today
    sent_path = DATA_DIR / f"discord_sent_{date}.json"
    already_sent: set[int] = set()
    if sent_path.exists():
        already_sent = set(json.loads(sent_path.read_text()))

    now  = _utc_now()
    posted: list[int] = []

    for game in games:
        pk = game["game_pk"]
        if pk in already_sent:
            continue

        fp = _parse_utc(game.get("first_pitch_utc", ""))
        if fp is None:
            continue

        minutes_until = (fp - now).total_seconds() / 60
        if not (NOTIFY_LATE <= minutes_until <= NOTIFY_EARLY):
            continue

        away = game["away_team"]
        home = game["home_team"]
        print(f"  Posting: {away} @ {home} ({minutes_until:.0f} min to first pitch)")

        embed = _build_embed(game)
        if _post_embed(embed):
            posted.append(pk)
            already_sent.add(pk)
        else:
            print(f"  Failed to post game {pk}")

    # Persist updated sent list
    if posted:
        sent_path.write_text(json.dumps(sorted(already_sent), indent=2))
        print(f"  Marked {len(posted)} game(s) as sent.")
    else:
        print("  No games in T-60 window (or already sent).")


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Send Discord game cards.")
    parser.add_argument("--date", default=None, help="YYYY-MM-DD (default: today)")
    args = parser.parse_args()

    print(f"Discord notifier — {args.date or 'today'}")
    notify_upcoming_games(args.date)
