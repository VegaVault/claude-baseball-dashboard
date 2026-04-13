"""
Dataclasses mirroring the JSON schema in docs/schema.md.
All timestamps are UTC ISO 8601 strings.
"""

from __future__ import annotations

import dataclasses
import json
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class PitcherSeasonStats:
    ip: Optional[float]
    fip: Optional[float]
    era_plus: Optional[int]
    xwoba: Optional[float]


@dataclass
class Pitcher:
    name: str
    mlbam_id: str
    throws: str  # "R" | "L"
    current_year: PitcherSeasonStats
    prior_year: Optional[PitcherSeasonStats]


@dataclass
class BatterSeasonStats:
    pa: int
    xwoba: Optional[float]


@dataclass
class Batter:
    order: int
    name: str
    mlbam_id: str
    bats: str  # "R" | "L" | "S"
    current_year: BatterSeasonStats
    prior_year: Optional[BatterSeasonStats]


@dataclass
class TeamRanks:
    hitting_xwoba_rank: Optional[int]
    pitching_xwoba_against_rank: Optional[int]
    defense_oaa_rank: Optional[int]


@dataclass
class Game:
    game_pk: int
    status: str  # "scheduled" | "in_progress" | "final"
    first_pitch_utc: str
    away_team: str
    home_team: str
    final_score: Optional[str]
    lineup_status: str  # "projected" | "confirmed" | "frozen"
    lineup_last_checked: Optional[str]
    pitchers: dict  # {"away": Pitcher, "home": Pitcher}
    lineups: dict   # {"away": list[Batter], "home": list[Batter]}
    team_ranks: dict  # {"away": TeamRanks, "home": TeamRanks}


@dataclass
class DailySnapshot:
    date: str
    last_updated: str
    games: list[Game] = field(default_factory=list)
    fetch_errors: list[str] = field(default_factory=list)

    def to_json(self) -> str:
        return json.dumps(dataclasses.asdict(self), indent=2)

    @staticmethod
    def from_json(text: str) -> DailySnapshot:
        raw = json.loads(text)
        games = []
        for g in raw.get("games", []):
            pitchers = {}
            for side in ("away", "home"):
                p = g["pitchers"].get(side)
                if p:
                    cy = p.get("current_year")
                    py = p.get("prior_year")
                    pitchers[side] = Pitcher(
                        name=p["name"],
                        mlbam_id=p["mlbam_id"],
                        throws=p["throws"],
                        current_year=PitcherSeasonStats(**cy) if cy else None,
                        prior_year=PitcherSeasonStats(**py) if py else None,
                    )
            lineups = {}
            for side in ("away", "home"):
                batters = []
                for b in g["lineups"].get(side, []):
                    cy = b.get("current_year")
                    py = b.get("prior_year")
                    batters.append(Batter(
                        order=b["order"],
                        name=b["name"],
                        mlbam_id=b["mlbam_id"],
                        bats=b["bats"],
                        current_year=BatterSeasonStats(**cy) if cy else None,
                        prior_year=BatterSeasonStats(**py) if py else None,
                    ))
                lineups[side] = batters
            team_ranks = {}
            for side in ("away", "home"):
                tr = g["team_ranks"].get(side)
                team_ranks[side] = TeamRanks(**tr) if tr else None

            games.append(Game(
                game_pk=g["game_pk"],
                status=g["status"],
                first_pitch_utc=g["first_pitch_utc"],
                away_team=g["away_team"],
                home_team=g["home_team"],
                final_score=g.get("final_score"),
                lineup_status=g["lineup_status"],
                lineup_last_checked=g.get("lineup_last_checked"),
                pitchers=pitchers,
                lineups=lineups,
                team_ranks=team_ranks,
            ))
        return DailySnapshot(
            date=raw["date"],
            last_updated=raw["last_updated"],
            games=games,
            fetch_errors=raw.get("fetch_errors", []),
        )
