"""确定性数字校验：规范化多重集差。

诚实边界（写进交付文档，基准中已实测到一例）：
- 同值碰撞：错误值恰与同页另一 token 同值时不可检。
- 零计数中性移位：空格填 0 且同一行丢一个 0，零的总数不变，不可检。
以上由原图回传 + 答案侧溯源徽章补偿。
"""

import re
from collections import Counter

_NUM_RE = re.compile(r"\(?-?\$?\d[\d,]*\.?\d*%?\)?")
# 年份白名单：只豁免"裸 4 位、无逗号/小数/单位、且在合理年份区间"的 token。
# "2,080" 带逗号、"2025.5" 带小数 → 都参与比对；裸 "2025" → 视为年份跳过。
_YEAR_RE = re.compile(r"(19|20)\d{2}")


def canon(token: str) -> str | None:
    """货币符/千分位/%/会计括号负数 → 规范数值串；尾零归一。"""
    t = token.strip()
    if _YEAR_RE.fullmatch(t):
        return None  # 裸年份不参与比对（页面日期、财年标签大量存在）
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
    """转录中出现、但文本层不存在（计数感知）的数字。

    raw_text 为空（keynote 类）时无比对依据，返回空——该类页面靠原图回传兜底。
    复述容差：文本层里存在的值可在转录中任意重复（图表描述会合理复述数字），
    只有文本层完全不存在的值才标可疑。
    """
    if not raw_text.strip():
        return []
    md, raw = numbers_in(markdown), numbers_in(raw_text)
    return sorted(k for k in md if k not in raw)
