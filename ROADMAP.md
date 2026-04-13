# Baseball Dashboard — Milestone Roadmap

## Phase 0 — Scaffolding ✓
Set up repo, requirements.txt, CLAUDE.md, folder structure, placeholder files,
basic models.py dataclasses, a .env.example for the Discord webhook.

## Phase 1 — Data fetchers (build one at a time, each with a test)
Order matters — start with the ones that have no dependencies:

1. **Schedule** (MLB Stats API) — easiest, unblocks everything
2. **Handedness** (pybaseball Chadwick)
3. **Pitcher season stats** (pybaseball: FanGraphs for xFIP/IP, Savant for xwOBA)
4. **Batter season stats** (pybaseball: Savant for xwOBA)
5. **Team stats** (Savant leaderboards: team xwOBA, xwOBA-against, OAA → compute ranks)
6. **Lineups** (MLB Stats API: projected from last game + confirmed when available)
7. **Probables** (MLB Stats API first; FanGraphs scrape as enhancement for early-announce)

Each fetcher = a function that returns a dataclass, plus a CLI entrypoint so
you can run it standalone and eyeball the output.

## Phase 2 — Snapshot builder
`snapshot.py` orchestrates all fetchers into the JSON schema. Writes to
`data/YYYY-MM-DD.json`. Idempotent — re-running replaces the file safely.
Handles partial failures gracefully (if FanGraphs scrape dies, log it and
continue).

## Phase 3 — Lineup updater
`lineup_update.py` loads today's JSON, finds games where
`lineup_status == "projected"` and `first_pitch - now` is between 15 min and
2 hours, checks MLB Stats API for confirmed lineup, updates that game's lineup
+ recomputes lineup xwOBA, rewrites JSON. Freezes any game where
`first_pitch - now < 0`.

## Phase 4 — Streamlit dashboard
- Sidebar: date picker (today + yesterday only for v1), game dropdown
- Main area per game:
  - Header: teams, first pitch, status, score if final
  - Reconciliation table (3×2: hitting xwOBA rank, pitching xwOBA-against rank, defense OAA rank)
  - Two pitcher cards side by side (name, hand, IP, xFIP, xwOBA — current and prior year)
  - Two lineup tables side by side (order, name, hand, xwOBA current + prior, PA)
  - Badge showing `lineup_status`
- Reads JSON files from `data/` in the repo — no live API calls from Streamlit

## Phase 5 — GitHub Actions
- `morning-snapshot.yml`: cron `0 13 * * *` (9am ET = 13:00 UTC, adjust for DST),
  runs `snapshot.py`, commits `data/YYYY-MM-DD.json`
- `lineup-updates.yml`: cron `*/15 15-4 * * *` during game hours,
  runs `lineup_update.py`, commits if changed

## Phase 6 — Discord notifier
`discord.py` runs as part of the lineup-updates workflow. For each game, if
now is within 5 min of `first_pitch - 60min` AND we haven't posted yet, post
the game card as a Discord embed. Track posted games in
`data/discord_sent_YYYY-MM-DD.json` to prevent duplicates.

## Phase 7 — Deploy to Streamlit Cloud
Connect GitHub repo → Streamlit Cloud → point at `src/dashboard/app.py`. Done.

## Phase 8 (stretch) — Nice-to-haves
Ballpark, weather, umpire K%, bullpen xFIP, L15 form. Only after v1 is stable.
