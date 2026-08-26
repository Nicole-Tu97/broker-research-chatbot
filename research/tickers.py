"""Ticker alias dictionary and deterministic extraction.

Why a dictionary instead of symbol matching or model output:
- Literal symbol matching under-tags — keynotes, official NVIDIA decks, and BofA
  multi-sector reports write only "NVIDIA", never "NVDA" (measured: literal NVDA
  appears in just 21/30 documents).
- A full-universe ticker dictionary over-tags — uppercase "AI" is a real ticker
  and appears in 29/30 documents.
- Model-emitted MENTIONED_TICKERS would reintroduce extraction-error risk.

Rules: symbols match case-sensitively (guards against uppercased common words like
"AI"/"ON"); company names match case-insensitively. Semantics are mentioned tickers
(tag on any mention); combined with ticker_pages, the model reading the source text
judges relevance itself. At thousand-document scale, the migration point is wiring
in a security master (DESIGN.md §8).
"""

import re

# {ticker: [aliases...]}; all-uppercase aliases match under the symbol rule
# (case-sensitive), the rest under the name rule
TICKER_ALIASES: dict[str, list[str]] = {
    # Corpus protagonist and direct peers
    "NVDA": ["NVDA", "NVDA.O", "Nvidia", "NVIDIA"],
    "AMD": ["AMD", "Advanced Micro Devices"],
    "INTC": ["INTC", "Intel"],
    "AVGO": ["AVGO", "Broadcom"],
    "QCOM": ["QCOM", "Qualcomm"],
    "MRVL": ["MRVL", "Marvell"],
    "MU": ["MU", "Micron"],
    "TSM": ["TSM", "TSMC", "Taiwan Semiconductor"],
    "ASML": ["ASML"],
    "ARM": ["Arm Holdings"],  # Deliberately omits symbol "ARM": clashes with "ARM-based"
    # Hyperscalers / major customers
    "MSFT": ["MSFT", "Microsoft"],
    "GOOG": ["GOOG", "GOOGL", "Google", "Alphabet"],
    "AMZN": ["AMZN", "Amazon", "AWS"],
    "META": ["META", "Meta Platforms"],  # Deliberately omits bare "Meta": common-word risk
    "AAPL": ["AAPL", "Apple"],
    "ORCL": ["ORCL", "Oracle"],
    "TSLA": ["TSLA", "Tesla"],
    "CRWV": ["CRWV", "CoreWeave"],
    # Server / power and data-center supply chain (covered by multi-sector reports)
    "SMCI": ["SMCI", "Super Micro"],
    "DELL": ["DELL", "Dell"],
    "HPE": ["HPE", "Hewlett Packard Enterprise"],
    "VRT": ["VRT", "Vertiv"],
    "ETN": ["ETN", "Eaton"],
    "EMR": ["EMR", "Emerson"],
    "PWR": ["PWR", "Quanta Services"],
    "GEV": ["GEV", "GE Vernova"],
    "CEG": ["CEG", "Constellation Energy"],
    "JCI": ["JCI", "Johnson Controls"],
    "CARR": ["CARR", "Carrier"],
    "ROK": ["ROK", "Rockwell Automation"],
}

_SYMBOL_RES: dict[str, list[re.Pattern]] = {}
_NAME_RES: dict[str, list[re.Pattern]] = {}
for _t, _aliases in TICKER_ALIASES.items():
    for _a in _aliases:
        pat = re.escape(_a).replace(r"\.", r"\.")
        if _a.isupper():  # Symbol: case-sensitive, word boundaries
            _SYMBOL_RES.setdefault(_t, []).append(re.compile(rf"\b{pat}\b"))
        else:  # Company name: case-insensitive
            _NAME_RES.setdefault(_t, []).append(re.compile(rf"\b{pat}\b", re.I))


def tickers_in(text: str) -> set[str]:
    """All tickers mentioned in a piece of text."""
    found = set()
    for t, res in _SYMBOL_RES.items():
        if any(r.search(text) for r in res):
            found.add(t)
    for t, res in _NAME_RES.items():
        if t not in found and any(r.search(text) for r in res):
            found.add(t)
    return found


def extract_ticker_pages(page_texts: list[str]) -> dict[str, list[int]]:
    """Per-page extraction → {ticker: [matching page numbers (1-indexed)]}. At index
    time this is called on raw_text concatenated with markdown, so pages without a
    text layer can still match via the transcription."""
    hits: dict[str, list[int]] = {}
    for i, text in enumerate(page_texts, start=1):
        if not text:
            continue
        for t in tickers_in(text):
            hits.setdefault(t, []).append(i)
    return hits
