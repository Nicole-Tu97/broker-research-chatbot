"""Ticker 别名字典与确定性提取。

为什么是字典而不是符号匹配或模型输出：
- 字面符号匹配漏标——keynote、NVIDIA 官方 deck、BofA 多行业报告全篇只写
  "NVIDIA" 不写 "NVDA"（实测 21/30 份才有字面 NVDA）。
- ticker 全集字典过标——大写 "AI" 是真实 ticker，出现在 29/30 份文档里。
- 模型输出 MENTIONED_TICKERS 会重新引入抽取错误风险面。

规则：符号大小写敏感（防 "AI"/"ON" 类大写普通词），公司名大小写不敏感。
语义是 mentioned tickers（提及即标），配合 ticker_pages 让读到原文的模型
自行判断相关性。千文档量级的迁移点是接入 security master（DESIGN.md §9）。
"""

import re

# {ticker: [别名...]}；全大写别名按符号规则（大小写敏感）匹配，其余按名称规则
TICKER_ALIASES: dict[str, list[str]] = {
    # 语料主角与直接同业
    "NVDA": ["NVDA", "NVDA.O", "Nvidia", "NVIDIA"],
    "AMD": ["AMD", "Advanced Micro Devices"],
    "INTC": ["INTC", "Intel"],
    "AVGO": ["AVGO", "Broadcom"],
    "QCOM": ["QCOM", "Qualcomm"],
    "MRVL": ["MRVL", "Marvell"],
    "MU": ["MU", "Micron"],
    "TSM": ["TSM", "TSMC", "Taiwan Semiconductor"],
    "ASML": ["ASML"],
    "ARM": ["Arm Holdings"],  # 刻意不含大写符号 "ARM"：与架构词 ARM-based 冲突
    # Hyperscalers / 大客户
    "MSFT": ["MSFT", "Microsoft"],
    "GOOG": ["GOOG", "GOOGL", "Google", "Alphabet"],
    "AMZN": ["AMZN", "Amazon", "AWS"],
    "META": ["META", "Meta Platforms"],  # 刻意不含裸词 "Meta"：普通词误标风险
    "AAPL": ["AAPL", "Apple"],
    "ORCL": ["ORCL", "Oracle"],
    "TSLA": ["TSLA", "Tesla"],
    "CRWV": ["CRWV", "CoreWeave"],
    # 服务器 / 电力与数据中心供应链（多行业报告涉及）
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
        if _a.isupper():  # 符号：大小写敏感，词边界
            _SYMBOL_RES.setdefault(_t, []).append(re.compile(rf"\b{pat}\b"))
        else:  # 公司名：大小写不敏感
            _NAME_RES.setdefault(_t, []).append(re.compile(rf"\b{pat}\b", re.I))


def tickers_in(text: str) -> set[str]:
    """一段文本中提及的全部 ticker。"""
    found = set()
    for t, res in _SYMBOL_RES.items():
        if any(r.search(text) for r in res):
            found.add(t)
    for t, res in _NAME_RES.items():
        if t not in found and any(r.search(text) for r in res):
            found.add(t)
    return found


def extract_ticker_pages(page_texts: list[str]) -> dict[str, list[int]]:
    """逐页提取 → {ticker: [命中页码(1-indexed)]}。索引时对 raw_text 与
    markdown 的拼接调用，保证无文本层的页也能靠转录命中。"""
    hits: dict[str, list[int]] = {}
    for i, text in enumerate(page_texts, start=1):
        if not text:
            continue
        for t in tickers_in(text):
            hits.setdefault(t, []).append(i)
    return hits
