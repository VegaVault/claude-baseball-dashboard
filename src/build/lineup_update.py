"""
Lineup updater: lightweight job that checks for confirmed lineups.

Loads today's JSON, finds games where lineup_status == "projected" and
first_pitch is 15 min to 2 hours away, queries MLB Stats API for confirmed
lineup, updates the JSON in place. Freezes games where first_pitch has passed.
Per CLAUDE.md rule #6: never writes to a game that is already frozen.
"""


def update_lineups(date: str) -> None:
    """
    Check and update lineup statuses for today's games.

    Args:
        date: Date string YYYY-MM-DD. Defaults to today if not provided.
    """
    pass


if __name__ == "__main__":
    pass
