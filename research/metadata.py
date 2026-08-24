"""文件名元数据解析。

文件名格式（券商研报）：
    YYYYMMDD - Broker - [TICKER - ] Title... - N pages.pdf
已实测的坑：
- 文件名页数系统性错误（12→6、18→8、40→32）——只存为 claimed_page_count，
  真实页数以 PyMuPDF 打开后为准。
- 4 份 NVIDIA 官方 deck 无此格式 → broker="NVIDIA"（发行方），日期留空，
  由摄取时的首页内容校验兜底。
- Title 段内可能再含 " - "（如 "Revision - U S Semiconductors"），
  故 ticker 只认第一个段落且须匹配别名字典的符号。
"""

import re
from dataclasses import dataclass
from datetime import date, datetime

from .tickers import TICKER_ALIASES

_MONTHS = ("January|February|March|April|May|June|July|"
           "August|September|October|November|December")
# 首页常见日期写法："8 July 2025" / "June 11, 2025" / "August 28, 2025"
_TEXT_DATE_RES = [
    (re.compile(rf"\b(\d{{1,2}})\s+({_MONTHS})\s+(20\d{{2}})\b"), "%d %B %Y", (1, 2, 3)),
    (re.compile(rf"\b({_MONTHS})\s+(\d{{1,2}}),?\s+(20\d{{2}})\b"), "%B %d %Y", (2, 1, 3)),
]


def date_from_text(text: str) -> date | None:
    """首页文本中的第一个可解析日期（内容侧兜底）。"""
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
    ticker: str | None  # 文件名声明的主 ticker（tickers[] 首位）
    title: str
    claimed_page_count: int | None  # 已知不可信，仅存档


def parse_filename(stem: str) -> FileMeta:
    m = _BROKER_RE.match(stem)
    if not m:
        # NVIDIA 官方 deck：GTC-Paris-2025-Keynote / NVDA-F1Q26-... / NVIDIA-2025-NDR-...
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
