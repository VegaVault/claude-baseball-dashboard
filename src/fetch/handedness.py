"""
Fetcher: player handedness (bats / throws) via MLB Stats API people endpoint.

Batches up to 50 IDs per request to stay within URL length limits.
Falls back to individual lookups for any IDs that fail in the batch.
"""

import logging
from itertools import islice

import statsapi

logger = logging.getLogger(__name__)

_BATCH_SIZE = 50


def _chunks(lst: list, n: int):
    """Yield successive n-sized chunks from lst."""
    it = iter(lst)
    while chunk := list(islice(it, n)):
        yield chunk


def fetch_handedness(mlbam_ids: list[str]) -> dict[str, dict]:
    """
    Return handedness for a list of MLBAM player IDs.

    Args:
        mlbam_ids: List of MLBAM ID strings.

    Returns:
        Dict mapping mlbam_id -> {"bats": "R"|"L"|"S"|None,
                                   "throws": "R"|"L"|None}
    """
    result: dict[str, dict] = {}

    for batch in _chunks(mlbam_ids, _BATCH_SIZE):
        id_str = ",".join(str(i) for i in batch)
        try:
            data = statsapi.get(
                "people",
                {"personIds": id_str, "hydrate": "batSide,pitchHand"},
            )
            for person in data.get("people", []):
                pid = str(person["id"])
                result[pid] = {
                    "bats":   person.get("batSide", {}).get("code"),
                    "throws": person.get("pitchHand", {}).get("code"),
                }
        except Exception as e:
            logger.warning("Batch handedness lookup failed (%s): %s — trying one by one", id_str, e)
            for pid in batch:
                result[str(pid)] = _single_lookup(str(pid))

    # Fill in any IDs that came back missing from the API response
    for pid in mlbam_ids:
        if str(pid) not in result:
            logger.debug("ID %s missing from batch response — fallback lookup", pid)
            result[str(pid)] = _single_lookup(str(pid))

    return result


def _single_lookup(mlbam_id: str) -> dict:
    """Fallback: look up one player at a time."""
    try:
        data = statsapi.get("people", {"personIds": mlbam_id})
        people = data.get("people", [])
        if people:
            p = people[0]
            return {
                "bats":   p.get("batSide", {}).get("code"),
                "throws": p.get("pitchHand", {}).get("code"),
            }
    except Exception as e:
        logger.warning("Single handedness lookup failed for %s: %s", mlbam_id, e)
    return {"bats": None, "throws": None}


if __name__ == "__main__":
    # Known players for sanity check:
    # Aaron Judge (592450)  — bats R, throws R
    # Freddie Freeman (518692) — bats L, throws L
    # Shohei Ohtani (660271) — bats L, throws R
    # Ozzie Albies (645277)  — bats S (switch), throws R
    test_ids = ["592450", "518692", "660271", "645277"]

    names = {
        "592450": "Aaron Judge",
        "518692": "Freddie Freeman",
        "660271": "Shohei Ohtani",
        "645277": "Ozzie Albies",
    }

    print("Fetching handedness from MLB Stats API...\n")
    data = fetch_handedness(test_ids)
    for mlbam_id, h in data.items():
        print(f"  {names.get(mlbam_id, mlbam_id):20s}  bats={h['bats']}  throws={h['throws']}")
