"""
shared_utils.py — Shared utility functions used by multiple agents.

Centralises duplicated helpers so each agent imports from one place.
"""


def has_cjk(text: str) -> bool:
    """Return True if *text* contains any CJK, kana, or Hangul character.

    Covers CJK unified ideographs, hiragana/katakana, Hangul syllables,
    CJK compatibility ideographs, and CJK extension blocks B-F.
    """
    for ch in text:
        cp = ord(ch)
        if (
            0x3000 <= cp <= 0x9FFF      # CJK unified + hiragana/katakana
            or 0xAC00 <= cp <= 0xD7AF   # Hangul syllables
            or 0xF900 <= cp <= 0xFAFF   # CJK compatibility
            or 0x20000 <= cp <= 0x2FA1F # CJK extensions B-F
        ):
            return True
    return False


def cluster(vals: list, tol: float = 3.0, min_count: int = 2) -> dict:
    """Group floats that are within *tol* of each other.

    Returns {representative_value: [original_values]} for groups with
    at least *min_count* members.
    """
    if not vals:
        return {}
    sorted_vals = sorted(vals)
    groups: list[list[float]] = [[sorted_vals[0]]]
    for v in sorted_vals[1:]:
        if v - groups[-1][-1] <= tol:
            groups[-1].append(v)
        else:
            groups.append([v])
    result: dict = {}
    for grp in groups:
        if len(grp) >= min_count:
            rep = sum(grp) / len(grp)
            result[rep] = grp
    return result
