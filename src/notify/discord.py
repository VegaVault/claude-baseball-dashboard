"""
Discord notifier: posts game cards as embeds via webhook.

For each game, if now is within 5 minutes of T-60 (first_pitch minus 60 min)
AND we haven't posted yet, post the game card. Tracks sent games in
data/discord_sent_YYYY-MM-DD.json to prevent duplicates.
"""


def notify_upcoming_games(date: str) -> None:
    """
    Post Discord embeds for games approaching first pitch (T-60 window).

    Args:
        date: Date string YYYY-MM-DD. Defaults to today if not provided.
    """
    pass


if __name__ == "__main__":
    pass
