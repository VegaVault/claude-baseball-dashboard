"""
Shared percentile + label utilities for batter and pitcher stat fetchers.
"""

# Decile labels: index 0 = bottom 10%, index 9 = top 10%
DECILE_LABELS = [
    "Unplayable",  # 0–10th
    "Brutal",      # 10–20th
    "Weak",        # 20–30th
    "Shaky",       # 30–40th
    "Mediocre",    # 40–50th
    "Decent",      # 50–60th
    "Solid",       # 60–70th
    "Strong",      # 70–80th
    "Dominant",    # 80–90th
    "Elite",       # 90–100th
]


def percentile_to_label(pct: int) -> str:
    """Convert a 0–100 percentile to a decile label string."""
    idx = min(int(pct) // 10, 9)
    return DECILE_LABELS[idx]


def compute_percentiles(values: list[float], higher_is_better: bool = True) -> list[int]:
    """
    Compute 0–100 percentile rank for each value in the list.
    higher_is_better=True  → highest value = 100th percentile (e.g. batter xwOBA)
    higher_is_better=False → lowest value = 100th percentile (e.g. pitcher FIP)
    """
    n = len(values)
    if n == 0:
        return []

    # Rank: 1 = lowest value, n = highest value
    from pandas import Series
    ranks = Series(values).rank(method="average")

    if higher_is_better:
        pcts = ((ranks - 1) / (n - 1) * 100) if n > 1 else Series([50.0])
    else:
        pcts = ((n - ranks) / (n - 1) * 100) if n > 1 else Series([50.0])

    return [min(max(int(round(p)), 0), 100) for p in pcts]


# ── Team grade helpers ────────────────────────────────────────────────────────

def rank_to_grade(rank: int | None, n: int = 30) -> str:
    """Convert a 1-based rank out of n to a letter grade (A+ → F)."""
    if rank is None:
        return "—"
    # Normalise to 0.0 (worst) → 1.0 (best)
    score = 1.0 - (rank - 1) / (n - 1)
    if score >= 0.93: return "A+"
    if score >= 0.87: return "A"
    if score >= 0.80: return "A-"
    if score >= 0.73: return "B+"
    if score >= 0.67: return "B"
    if score >= 0.60: return "B-"
    if score >= 0.53: return "C+"
    if score >= 0.47: return "C"
    if score >= 0.40: return "C-"
    if score >= 0.33: return "D+"
    if score >= 0.27: return "D"
    if score >= 0.20: return "D-"
    return "F"


def overall_grade(hitting_rank: int | None,
                  pitching_rank: int | None,
                  defense_rank: int | None,
                  n: int = 30) -> str:
    """Average the three ranks and convert to a letter grade."""
    valid = [r for r in [hitting_rank, pitching_rank, defense_rank] if r is not None]
    if not valid:
        return "—"
    return rank_to_grade(round(sum(valid) / len(valid)), n)
