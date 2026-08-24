"""Filename metadata parsing.

Filename format (broker research reports):
    YYYYMMDD - Broker - [TICKER - ] Title... - N pages.pdf
Pitfalls observed in practice:
- Page counts in filenames are systematically wrong (12→6, 18→8, 40→32) — stored
  only as claimed_page_count; the true count comes from opening with PyMuPDF.
- 4 official NVIDIA decks don't follow this format → broker="NVIDIA" (the issuer),
  date left empty; the first-page content check at ingest time is the fallback.
- The Title segment may itself contain " - " (e.g. "Revision - U S Semiconductors"),
  so the ticker is only taken from the first segment and must match a symbol in the
  alias dictionary.
"""

import re
from dataclasses import dataclass
from datetime import date, datetime

from .tickers import TICKER_ALIASES

_MONTHS = ("January|February|March|April|May|June|July|"
           "August|September|October|November|December")
# Common first-page date styles: "8 July 2025" / "June 11, 2025" / "August 28, 2025"
_TEXT_DATE_RES = [
    (re.compile(rf"\b(\d{{1,2}})\s+({_MONTHS})\s+(20\d{{2}})\b"), "%d %B %Y", (1, 2, 3)),
    (re.compile(rf"\b({_MONTHS})\s+(\d{{1,2}}),?\s+(20\d{{2}})\b"), "%B %d %Y", (2, 1, 3)),
]


def date_from_text(text: str) -> date | None:
    """First parseable date in the first-page text (content-side fallback)."""
    for pat, fmt, order in _TEXT_DATE_RES:
        m = pat.search(text)
        if m:
            day, month, year = (m.group(i) for i in order)
            try:
                return datetime.strptime(f"{day} {month} {year}", "%d %B %Y").date()
            except ValueError:
                continue
    return None

_BROKER_RE = re.compile(
    r"^(?P<date>\d{8}) - (?P<broker>.+?) - (?P<rest>.+?)(?: - (?P<pages>\d+) pages)?$"
)


@dataclass
class FileMeta:
    broker: str
    published_date: date | None
    ticker: str | None  # Primary ticker declared in the filename (first in tickers[])
    title: str
    claimed_page_count: int | None  # Known unreliable; kept for the record only


def parse_filename(stem: str) -> FileMeta:
    m = _BROKER_RE.match(stem)
    if not m:
        # Official NVIDIA decks: GTC-Paris-2025-Keynote / NVDA-F1Q26-... / NVIDIA-2025-NDR-...
        return FileMeta(
            broker="NVIDIA",
            published_date=None,
            ticker="NVDA" if re.search(r"\b(NVDA|NVIDIA|GTC)\b", stem, re.I) else None,
            title=stem,
            claimed_page_count=None,
        )

    try:
        pub = datetime.strptime(m["date"], "%Y%m%d").date()
    except ValueError:
        pub = None

    rest = m["rest"].strip()
    ticker = None
    first, _, remainder = rest.partition(" - ")
    if first.strip() in TICKER_ALIASES:
        ticker = first.strip()
        rest = remainder.strip() or rest

    return FileMeta(
        broker=m["broker"].strip(),
        published_date=pub,
        ticker=ticker,
        title=rest,
        claimed_page_count=int(m["pages"]) if m["pages"] else None,
    )
