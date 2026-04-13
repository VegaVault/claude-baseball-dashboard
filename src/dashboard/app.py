"""
Streamlit dashboard: MLB daily matchup viewer.

Reads JSON files from data/ — no live API calls.
All times displayed in ET (converted from UTC stored in JSON).
"""

import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import streamlit as st

# ── Paths ─────────────────────────────────────────────────────────────────────
DATA_DIR = Path(__file__).parents[2] / "data"
ET = ZoneInfo("America/New_York")


# ── Helpers ───────────────────────────────────────────────────────────────────

def utc_to_et(utc_str: str) -> datetime:
    dt = datetime.fromisoformat(utc_str.replace("Z", "+00:00"))
    return dt.astimezone(ET)


def fmt_time(utc_str: str) -> str:
    if not utc_str:
        return "TBD"
    dt = utc_to_et(utc_str)
    return dt.strftime("%I:%M %p ET").lstrip("0")


def fmt_stat(val, decimals=3) -> str:
    return "—" if val is None else f"{val:.{decimals}f}"


def fmt_int(val) -> str:
    return "—" if val is None else str(int(val))


def rank_label(val: int | None) -> str:
    return "—" if val is None else f"#{val}"


def load_snapshot(date_str: str) -> dict | None:
    path = DATA_DIR / f"{date_str}.json"
    return json.loads(path.read_text()) if path.exists() else None


def available_dates() -> list[str]:
    return [f.stem for f in sorted(DATA_DIR.glob("????-??-??.json"), reverse=True)]


# ── Section renderers ─────────────────────────────────────────────────────────

def render_header(game: dict) -> None:
    away, home = game["away_team"], game["home_team"]
    status = game["status"].replace("_", " ").title()
    time_str = fmt_time(game.get("first_pitch_utc", ""))
    score = game.get("final_score")

    col_a, col_mid, col_h = st.columns([2, 1, 2])
    with col_a:
        st.markdown(f"## {away}")
        st.caption("Away")
    with col_mid:
        st.markdown("<div style='text-align:center;padding-top:14px;font-size:1.4rem;color:#888'>@</div>",
                    unsafe_allow_html=True)
        if score:
            st.markdown(f"<div style='text-align:center;font-weight:700'>{score}</div>",
                        unsafe_allow_html=True)
    with col_h:
        st.markdown(f"## {home}")
        st.caption("Home")

    st.caption(f"🕐 {time_str}  ·  {status}")
    st.divider()


def render_team_ranks(game: dict) -> None:
    away, home = game["away_team"], game["home_team"]
    tr_a = game["team_ranks"].get("away") or {}
    tr_h = game["team_ranks"].get("home") or {}

    st.markdown("**Team Rankings** &nbsp;*(1 = best out of 30)*", unsafe_allow_html=True)

    metrics = [
        ("⚔️ Hitting xwOBA",      tr_a.get("hitting_xwoba_rank"),             tr_h.get("hitting_xwoba_rank")),
        ("🛡 Pitching xwOBA-Against", tr_a.get("pitching_xwoba_against_rank"), tr_h.get("pitching_xwoba_against_rank")),
        ("🧤 Defense OAA",          tr_a.get("defense_oaa_rank"),              tr_h.get("defense_oaa_rank")),
    ]

    col_label, col_away, col_home = st.columns([3, 1, 1])
    col_away.markdown(f"**{away}**")
    col_home.markdown(f"**{home}**")

    for label, av, hv in metrics:
        col_label, col_away, col_home = st.columns([3, 1, 1])
        col_label.write(label)
        col_away.metric(label=" ", value=rank_label(av), label_visibility="collapsed")
        col_home.metric(label=" ", value=rank_label(hv), label_visibility="collapsed")


def render_lineup_badge(status: str) -> None:
    colors = {
        "confirmed": ("🟢", "Confirmed"),
        "projected":  ("🟡", "Projected"),
        "frozen":     ("🔵", "Frozen"),
    }
    icon, label = colors.get(status, ("⚪", status.title()))
    st.markdown(f"**Lineup:** {icon} {label}")


def render_pitcher_card(pitcher: dict | None, team: str, side: str, current_year: int) -> None:
    prior_year = current_year - 1
    st.markdown(f"**{team}** — {side}")

    if not pitcher or not pitcher.get("name"):
        st.info("Probable TBD")
        return

    throws = pitcher.get("throws") or "?"
    st.markdown(f"### {pitcher['name']}")
    st.caption(f"Throws: {throws}")

    cy = pitcher.get("current_year") or {}
    py = pitcher.get("prior_year") or {}

    rows = []
    for yr, stats in [(current_year, cy), (prior_year, py)]:
        rows.append({
            "Season": str(yr),
            "IP":     fmt_stat(stats.get("ip"), 1)    if stats else "—",
            "FIP":    fmt_stat(stats.get("fip"), 2)   if stats else "—",
            "ERA+":   fmt_int(stats.get("era_plus"))  if stats else "—",
            "xwOBA":  fmt_stat(stats.get("xwoba"), 3) if stats else "—",
        })

    st.dataframe(
        pd.DataFrame(rows).set_index("Season"),
        use_container_width=True,
    )


def render_lineup_table(lineup: list[dict], current_year: int) -> None:
    prior_year = current_year - 1
    if not lineup:
        st.caption("Not available")
        return

    rows = []
    for b in sorted(lineup, key=lambda x: x.get("order", 99)):
        cy = b.get("current_year") or {}
        py = b.get("prior_year") or {}
        rows.append({
            "#":                     b.get("order", "?"),
            "Name":                  b.get("name", "?"),
            "B":                     b.get("bats") or "?",
            f"xwOBA '{str(current_year)[-2:]}": fmt_stat(cy.get("xwoba"), 3),
            f"PA '{str(current_year)[-2:]}":    fmt_int(cy.get("pa")),
            f"xwOBA '{str(prior_year)[-2:]}":   fmt_stat(py.get("xwoba"), 3),
        })

    st.dataframe(
        pd.DataFrame(rows).set_index("#"),
        use_container_width=True,
        height=355,
    )


# ── Main app ──────────────────────────────────────────────────────────────────

def main() -> None:
    st.set_page_config(
        page_title="MLB Daily Dashboard",
        page_icon="⚾",
        layout="wide",
    )

    # ── Sidebar ───────────────────────────────────────────────────────────────
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

    # ── Main panel ────────────────────────────────────────────────────────────
    game = next(g for g in games if g["game_pk"] == selected_pk)

    render_header(game)

    # Team ranks + lineup status
    col_ranks, col_status = st.columns([3, 1])
    with col_ranks:
        render_team_ranks(game)
    with col_status:
        st.markdown("&nbsp;", unsafe_allow_html=True)
        render_lineup_badge(game.get("lineup_status", "projected"))

    st.divider()

    # ── Pitcher cards ─────────────────────────────────────────────────────────
    st.markdown("### Starting Pitchers")
    col_ap, col_hp = st.columns(2)

    with col_ap:
        render_pitcher_card(
            game["pitchers"].get("away"), game["away_team"], "Away", current_year
        )
    with col_hp:
        render_pitcher_card(
            game["pitchers"].get("home"), game["home_team"], "Home", current_year
        )

    st.divider()

    # ── Lineup tables ─────────────────────────────────────────────────────────
    st.markdown("### Lineups")
    col_al, col_hl = st.columns(2)

    with col_al:
        st.caption(f"**{game['away_team']}** — Away")
        render_lineup_table(game["lineups"].get("away", []), current_year)

    with col_hl:
        st.caption(f"**{game['home_team']}** — Home")
        render_lineup_table(game["lineups"].get("home", []), current_year)


if __name__ == "__main__":
    main()
