"""Deterministic numeric verification: canonicalized multiset difference.

Honest limitations (documented in the deliverable; one case observed in the benchmark):
- Same-value collision: undetectable when a wrong value happens to equal another
  token on the same page.
- Zero-count-neutral shift: a blank filled with 0 while the same row drops a 0 keeps
  the total zero count unchanged — undetectable.
Both are compensated by returning the original page image plus answer-side
provenance badges.
"""

import re
from collections import Counter

_NUM_RE = re.compile(r"\(?-?\$?\d[\d,]*\.?\d*%?\)?")
# Year whitelist: exempt only tokens that are "bare 4 digits, no comma/decimal/unit,
# and within a plausible year range".
# "2,080" has a comma, "2025.5" has a decimal → both are compared; bare "2025" →
# treated as a year and skipped.
_YEAR_RE = re.compile(r"(19|20)\d{2}")


def canon(token: str) -> str | None:
    """Currency sign / thousands separators / % / accounting-parens negatives →
    canonical numeric string; trailing zeros normalized."""
    t = token.strip()
    if _YEAR_RE.fullmatch(t):
        return None  # Bare years excluded from comparison (page dates, FY labels abound)
    neg = t.startswith("(") and t.endswith(")")
    t = t.strip("()").lstrip("$").rstrip("%").replace(",", "")
    if not t or not re.fullmatch(r"-?\d+\.?\d*", t):
        return None
    if "." in t:
        t = t.rstrip("0").rstrip(".")
    if neg and not t.startswith("-"):
        t = "-" + t
    return t


def numbers_in(text: str) -> Counter:
    out: Counter = Counter()
    for tok in _NUM_RE.findall(text):
        c = canon(tok)
        if c is not None:
            out[c] += 1
    return out


def suspect_numbers(markdown: str, raw_text: str) -> list[str]:
    """Numbers present in the transcription but absent from the text layer
    (count-aware).

    When raw_text is empty (keynote-style pages) there is no basis for comparison:
    return empty — those pages fall back to returning the original page image.
    Restatement tolerance: values present in the text layer may repeat freely in the
    transcription (chart descriptions legitimately restate numbers); only values
    entirely absent from the text layer are flagged as suspect.
    """
    if not raw_text.strip():
        return []
    md, raw = numbers_in(markdown), numbers_in(raw_text)
    return sorted(k for k in md if k not in raw)
