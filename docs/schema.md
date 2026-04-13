# Data Schema

**Authoritative reference** — per CLAUDE.md rule #5, propose any changes before modifying.

All timestamps are UTC strings in ISO 8601 format. ET conversion happens only in the Streamlit UI.

## Top-level: `data/YYYY-MM-DD.json`

```json
{
  "date": "2026-04-12",
  "last_updated": "2026-04-12T13:05:00Z",
  "fetch_errors": [],
  "games": [ /* array of Game objects */ ]
}
```

| Field | Type | Description |
|---|---|---|
| `date` | string | `YYYY-MM-DD` of the games |
| `last_updated` | string | UTC ISO 8601 timestamp of last write |
| `fetch_errors` | array of string | Names of fetchers that failed (empty = all succeeded) |
| `games` | array | Ordered list of Game objects |

---

## Game object

```json
{
  "game_pk": 12345,
  "status": "scheduled",
  "first_pitch_utc": "2026-04-12T23:05:00Z",
  "away_team": "NYY",
  "home_team": "BOS",
  "final_score": null,
  "lineup_status": "projected",
  "lineup_last_checked": "2026-04-12T21:30:00Z",
  "pitchers": { "away": { /* Pitcher */ }, "home": { /* Pitcher */ } },
  "lineups":  { "away": [ /* Batter */ ], "home": [ /* Batter */ ] },
  "team_ranks": { "away": { /* TeamRanks */ }, "home": { /* TeamRanks */ } }
}
```

| Field | Type | Values / Notes |
|---|---|---|
| `game_pk` | int | MLB Stats API primary key |
| `status` | string | `"scheduled"` \| `"in_progress"` \| `"final"` |
| `first_pitch_utc` | string | UTC ISO 8601 |
| `away_team` | string | 3-letter abbreviation |
| `home_team` | string | 3-letter abbreviation |
| `final_score` | string \| null | e.g. `"3-5"` (away-home), null until final |
| `lineup_status` | string | `"projected"` \| `"confirmed"` \| `"frozen"` |
| `lineup_last_checked` | string \| null | UTC ISO 8601 of last lineup API check |
| `pitchers` | object | Keys: `"away"`, `"home"` → Pitcher objects |
| `lineups` | object | Keys: `"away"`, `"home"` → array of Batter objects |
| `team_ranks` | object | Keys: `"away"`, `"home"` → TeamRanks objects |

---

## Pitcher object

```json
{
  "name": "Gerrit Cole",
  "mlbam_id": "543037",
  "throws": "R",
  "current_year": { "ip": 12.1, "xfip": 3.45, "xwoba": 0.310 },
  "prior_year":   { "ip": 180.0, "xfip": 3.80, "xwoba": 0.320 }
}
```

| Field | Type | Notes |
|---|---|---|
| `name` | string | Full name |
| `mlbam_id` | string | MLB Stats API player ID |
| `throws` | string | `"R"` \| `"L"` |
| `current_year` | PitcherSeasonStats | Current season stats |
| `prior_year` | PitcherSeasonStats \| null | Prior season stats; null for rookies |

### PitcherSeasonStats

| Field | Type | Notes |
|---|---|---|
| `ip` | float \| null | Innings pitched (Baseball Reference) |
| `fip` | float \| null | Fielding Independent Pitching (Baseball Reference) |
| `era_plus` | int \| null | ERA+ adjusted for park/league (Baseball Reference, 100 = avg) |
| `xwoba` | float \| null | xwOBA allowed (Baseball Savant) |

---

## Batter object

```json
{
  "order": 1,
  "name": "Aaron Judge",
  "mlbam_id": "592450",
  "bats": "R",
  "current_year": { "pa": 42, "xwoba": 0.420 },
  "prior_year":   { "pa": 610, "xwoba": 0.410 }
}
```

| Field | Type | Notes |
|---|---|---|
| `order` | int | Batting order position (1–9) |
| `name` | string | Full name |
| `mlbam_id` | string | MLB Stats API player ID |
| `bats` | string | `"R"` \| `"L"` \| `"S"` (switch) |
| `current_year` | BatterSeasonStats | Current season stats |
| `prior_year` | BatterSeasonStats \| null | Prior season stats; null for rookies |

### BatterSeasonStats

| Field | Type | Notes |
|---|---|---|
| `pa` | int | Plate appearances |
| `xwoba` | float \| null | Expected wOBA (Savant) |

---

## TeamRanks object

Ranks are 1-based (1 = best). Computed across all 30 MLB teams.

```json
{
  "hitting_xwoba_rank": 3,
  "pitching_xwoba_against_rank": 12,
  "defense_oaa_rank": 8
}
```

| Field | Type | Notes |
|---|---|---|
| `hitting_xwoba_rank` | int \| null | Team xwOBA rank (offense) |
| `pitching_xwoba_against_rank` | int \| null | Team xwOBA-against rank (pitching staff) |
| `defense_oaa_rank` | int \| null | Team OAA rank (Outs Above Average) |
