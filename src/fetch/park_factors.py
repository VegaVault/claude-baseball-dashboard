"""
Park factors by team (home ballpark).

Values are 3-year regressed run park factors where:
  100 = perfectly neutral
  >100 = hitter-friendly (more runs expected)
  <100 = pitcher-friendly (fewer runs expected)

Source: FanGraphs park factors (2023-2025 average), updated manually each offseason.
"""

# Park factor by home team abbreviation
PARK_FACTORS: dict[str, int] = {
    "COL": 115,   # Coors Field — extreme hitter
    "CIN": 107,   # Great American Ball Park
    "BOS": 106,   # Fenway Park
    "MIN": 106,   # Target Field
    "PHI": 105,   # Citizens Bank Park
    "NYY": 105,   # Yankee Stadium
    "HOU": 104,   # Minute Maid Park
    "STL": 101,   # Busch Stadium
    "ATL": 100,
    "TOR": 100,   # Rogers Centre (dome)
    "CHC":  99,   # Wrigley Field (wind-dependent)
    "LAD":  99,   # Dodger Stadium
    "MIL":  98,
    "CLE":  98,
    "DET":  98,
    "KC":   97,
    "TEX":  97,   # Globe Life Field (climate-controlled)
    "TB":   97,   # Tropicana Field (dome)
    "BAL":  97,   # Camden Yards
    "ARI":  97,   # Chase Field (dome)
    "WSH":  96,   # Nationals Park
    "LAA":  96,   # Angel Stadium
    "CWS":  96,   # Guaranteed Rate Field
    "PIT":  95,   # PNC Park
    "SEA":  95,   # T-Mobile Park
    "ATH":  95,   # Oakland Coliseum / Sacramento
    "SD":   95,   # Petco Park
    "MIA":  94,   # loanDepot park (dome)
    "NYM":  94,   # Citi Field
    "SF":   93,   # Oracle Park — most pitcher-friendly
}


def get_park_factor(home_team: str) -> int | None:
    """Return the park factor for the given home team abbreviation."""
    return PARK_FACTORS.get(home_team.upper())


def park_factor_label(pf: int | None) -> str:
    """Convert a park factor to a human-readable label."""
    if pf is None:
        return "Unknown"
    if pf >= 110:
        return "Extreme Hitter"
    if pf >= 105:
        return "Hitter-Friendly"
    if pf >= 102:
        return "Slight Hitter"
    if pf >= 98:
        return "Neutral"
    if pf >= 95:
        return "Slight Pitcher"
    if pf >= 90:
        return "Pitcher-Friendly"
    return "Extreme Pitcher"


if __name__ == "__main__":
    print("Park factors for all 30 teams:\n")
    for abbr, pf in sorted(PARK_FACTORS.items(), key=lambda x: -x[1]):
        print(f"  {abbr:4s}  {pf:3d}  {park_factor_label(pf)}")
