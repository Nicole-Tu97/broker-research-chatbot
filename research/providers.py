"""全部外部调用集中在此文件（ARCHITECTURE.md §7）。

不是抽象层——就是"外部调用都在这个文件里"。三个调用点：
transcribe_page / embed / chat。API 形态锁定 OpenAI Responses API
（/v1/responses）：function_call_output 携带图像仅它支持（§4.4，已实测）。
零第三方 SDK：urllib 足够，依赖面最小。
"""

import base64
import http.client
import json
import re
import time
import urllib.error
import urllib.request

from django.conf import settings

_BASE = "https://api.openai.com/v1"

# 转录 prompt v3（唯一真源；与 bench/run_bench.py 逐字一致，基准实测记录见 §8.1.1）
TRANSCRIBE_SYSTEM = """你是金融文档转录引擎。你的输出会成为券商研报检索系统中该页的唯一文本表示。

绝对约束：
1. 不得输出页面上不存在的任何信息。宁可遗漏，不可编造。
2. 转录中的每一个数字都必须能在页面上找到。你不做任何计算、推断或换算。
3. 数字保留页面上的原始写法：千分位逗号、货币符号、百分号、尾零一律照抄，不得重新格式化。
4. 若某字符/数字辨认不清，写 [?]，不要猜测。

理由：本系统会带页码引用地向分析师呈现你的转录内容。一个错误的数字会被自信地引用，
且无任何纠错路径；而一处遗漏可以由原始页面图兜底。"""

TRANSCRIBE_USER = """以下是一页券商研报/演示文稿。

<raw_text>
{raw_text}
</raw_text>

<说明>
raw_text 是从 PDF 精确抽取的原生文本层，逐字准确。
若其为空或极短，说明该页内容以图像形式存在，此时完全依据页面图转录。
</说明>

请产出该页的 markdown 转录，遵循：

【正文】
- raw_text 非空时，正文直接采用其内容，仅整理段落与标题层级。
- 不要从图像中重新读取已存在于 raw_text 的文字。

【表格】
- 用 markdown 表格重建，保留表头层级与行标签。
- 单元格数值必须落在正确的行列位置。
- 每行的单元格数必须与表头列数一致；原文的空单元格保留为空——不得填 0、不得左右移位补齐。
- 合并单元格用重复值或空单元格表示，勿丢弃结构。

【图表】
每个图表输出一个区块：标题、图表类型、坐标轴（名称与单位）、
数据系列（名称 + 可读出的关键数值：首尾端点、极值、有数据标签者）、趋势的一句话描述。
读不出具体数值时，描述形状与相对关系，不要编造数字。

【忽略】
- 页眉页脚、页码、法律免责声明
- 水印（常见形式：斜向或竖排的邮箱、机构名、时间戳）

【输出格式】
先输出一行元数据，再输出转录正文：

HAS_VISUAL: true|false
（true 表示本页含图表、图片或表格，需要在检索时向模型回传原始页面图）

---

（转录正文）"""


class ProviderError(RuntimeError):
    pass


def _post(path: str, payload: dict, timeout: int = 300, retries: int = 3) -> dict:
    """429/5xx 指数退避重试（全量摄取是小时级长跑，瞬时限流不该变成页失败）。"""
    body = json.dumps(payload).encode()
    for attempt in range(retries + 1):
        req = urllib.request.Request(
            f"{_BASE}{path}",
            data=body,
            headers={
                "Authorization": f"Bearer {settings.OPENAI_API_KEY}",
                "Content-Type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.load(r)
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode()[:300]
            if exc.code in (429, 500, 502, 503, 504) and attempt < retries:
                time.sleep(2 ** attempt * 5)  # 5s, 10s, 20s
                continue
            raise ProviderError(f"HTTP {exc.code}: {detail}") from exc
        # OSError 覆盖 URLError/TimeoutError/ConnectionResetError/SSLError——
        # 200 之后读 body 时的连接中断不会被包成 URLError（审查确认的空档）。
        # HTTPError 是 OSError 子类，必须保持在上面的分支先接。
        except (OSError, http.client.HTTPException) as exc:
            if attempt < retries:
                time.sleep(2 ** attempt * 5)
                continue
            raise ProviderError(f"网络错误: {exc}") from exc


def _check(res: dict):
    """未完成或拒答的响应不得作为权威转录静默入库（评审修正）。"""
    status = res.get("status")
    if status and status != "completed":
        detail = res.get("incomplete_details") or {}
        raise ProviderError(f"响应未完成: status={status} {detail}")
    for o in res.get("output", []):
        for c in o.get("content", []) if o.get("type") == "message" else []:
            if c.get("type") == "refusal":
                raise ProviderError(f"模型拒答: {c.get('refusal', '')[:200]}")


def _output_text(res: dict) -> str:
    _check(res)
    return " ".join(
        c.get("text", "")
        for o in res.get("output", [])
        if o.get("type") == "message"
        for c in o.get("content", [])
    )


_HAS_VISUAL_RE = re.compile(r"HAS_VISUAL:\s*(true|false)", re.I)


def transcribe_page(png_bytes: bytes, raw_text: str) -> tuple[str, bool, dict]:
    """单次多模态调用（§4.2）→ (markdown, has_visual, usage)。"""
    res = _post("/responses", {
        "model": settings.OPENAI_VISION_MODEL,
        "instructions": TRANSCRIBE_SYSTEM,
        "input": [{"role": "user", "content": [
            {"type": "input_text", "text": TRANSCRIBE_USER.format(raw_text=raw_text or "(空)")},
            {"type": "input_image", "detail": settings.OPENAI_IMAGE_DETAIL,
             "image_url": "data:image/png;base64," + base64.b64encode(png_bytes).decode()},
        ]}],
    })
    text = _output_text(res)
    m = _HAS_VISUAL_RE.search(text[:300])
    # 元数据行缺失时取安全方向 True（多回传一张原图的代价远小于图页失去兜底）
    has_visual = m.group(1).lower() == "true" if m else True
    body = _HAS_VISUAL_RE.sub("", text, count=1)
    if "---" in body[:80]:
        body = body.split("---", 1)[1]
    return body.strip(), has_visual, res.get("usage", {})


FIGURE_BBOX_PROMPT = """A user asked: {question}

Below is a page from a financial document that was cited in the answer. Decide whether
any VISUAL element on this page (a chart, graph, diagram, or table) would materially
help the user BEYOND the answer text — and if so, locate it.

Return STRICT JSON only, one of:
- {{"x0": <int>, "y0": <int>, "x1": <int>, "y1": <int>}} — percentages (0-100) of page
  width/height, top-left origin, a tight box around the ONE most question-relevant
  chart/graph/diagram/table, INCLUDING its own axis labels, data labels, legend and
  units, EXCLUDING unrelated text panels, other figures, and page furniture. Include
  the element's own title only if it sits directly above it; never slice a line of
  text in half — if a title spans the whole page, exclude it.
- {{"whole_page": true}} — the page AS A WHOLE is the relevant visual (e.g. a slide
  that is one big chart).
- {{"no_figure": true}} — the page's contribution to the answer is textual (prose,
  a rating paragraph, plain body text); no visual element adds value beyond the text.
  When in doubt, choose this."""


def figure_bbox(png_bytes: bytes, question: str) -> tuple[dict | None, dict]:
    """定位被引页上与问题最相关的视觉元素（§十八/§二十一）→ (原始判定, usage)。

    判定三选一：{坐标} / {"whole_page": true} / {"no_figure": true}；
    None 仅表示解析失败。语义解释与坐标校验在调用方（chat._figure_crops）。"""
    res = _post("/responses", {
        "model": settings.OPENAI_VISION_MODEL,
        "input": [{"role": "user", "content": [
            {"type": "input_text",
             "text": FIGURE_BBOX_PROMPT.format(question=question[:500])},
            {"type": "input_image", "detail": settings.OPENAI_IMAGE_DETAIL,
             "image_url": "data:image/png;base64," + base64.b64encode(png_bytes).decode()},
        ]}],
    })
    usage = res.get("usage", {})
    text = _output_text(res)
    try:
        obj = json.loads(text[text.index("{"): text.rindex("}") + 1])
    except ValueError:
        return None, usage
    return obj, usage


def embed(texts: list[str]) -> list[list[float]]:
    res = _post("/embeddings", {
        "model": settings.OPENAI_EMBED_MODEL,
        "input": texts,
        "dimensions": settings.EMBED_DIMENSIONS,
    })
    if "data" not in res:
        raise ProviderError(f"embeddings 响应异常: {str(res)[:200]}")
    return [d["embedding"] for d in sorted(res["data"], key=lambda d: d["index"])]


def chat(input_items: list[dict], instructions: str, tools: list[dict] | None = None,
         on_delta=None) -> dict:
    """对话循环的单次往返（§6.3）。返回完整 response 对象，循环逻辑在调用方。

    on_delta 提供时走 SSE 流式：每个 output_text 增量调用 on_delta(str)，
    返回值仍是完整 response 对象（取自 response.completed 事件）——
    调用方的循环逻辑对流式/非流式完全无感。evaluate 等离线路径不传，零开销。"""
    payload = {
        "model": settings.OPENAI_VISION_MODEL,
        "instructions": instructions,
        "input": input_items,
    }
    if tools:
        payload["tools"] = tools
    if on_delta is None:
        return _post("/responses", payload)
    payload["stream"] = True
    return _post_stream("/responses", payload, on_delta)


def _sse_data(fp):
    """SSE 帧解析：按空行分帧，yield 每帧 data: 行的 JSON。纯函数，可测。

    容错：跨多行的 data、非 JSON 数据（如 [DONE] 哨兵）直接跳过。"""
    data_lines: list[str] = []
    for raw in fp:
        line = raw.decode("utf-8").rstrip("\r\n")
        if line.startswith("data:"):
            data_lines.append(line[5:].lstrip())
        elif line == "" and data_lines:
            joined = "\n".join(data_lines)
            data_lines = []
            try:
                yield json.loads(joined)
            except ValueError:
                continue
    if data_lines:  # 无结尾空行的最后一帧
        try:
            yield json.loads("\n".join(data_lines))
        except ValueError:
            pass


def _post_stream(path: str, payload: dict, on_delta, timeout: int = 300,
                 retries: int = 3) -> dict:
    """流式 /responses：增量转发给 on_delta，返回 response.completed 里的完整对象。

    重试语义与 _post 一致，但仅当尚未向客户端转发过任何增量时才重试——
    已转发后重试会在 UI 里产生重复文本，此时宁可让这一轮显式失败。"""
    body = json.dumps(payload).encode()
    for attempt in range(retries + 1):
        req = urllib.request.Request(
            f"{_BASE}{path}",
            data=body,
            headers={
                "Authorization": f"Bearer {settings.OPENAI_API_KEY}",
                "Content-Type": "application/json",
            },
        )
        emitted = False
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                final = None
                for data in _sse_data(r):
                    t = data.get("type", "")
                    if t == "response.output_text.delta":
                        on_delta(data.get("delta", ""))
                        emitted = True
                    elif t in ("response.completed", "response.incomplete"):
                        # incomplete（长度/内容过滤截停）与非流式语义对齐：
                        # 照样返回 response 对象，调用方消费其中的部分输出
                        final = data.get("response")
                    elif t == "error" or t.startswith("response.failed"):
                        raise ProviderError(f"流式响应错误: {str(data)[:300]}")
                if final is None:
                    raise ProviderError("流式响应未收到 response.completed")
                return final
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode()[:300]
            if exc.code in (429, 500, 502, 503, 504) and attempt < retries:
                time.sleep(2 ** attempt * 5)
                continue
            raise ProviderError(f"HTTP {exc.code}: {detail}") from exc
        except ProviderError:
            raise
        # 同 _post：OSError 才能接住流中途的连接重置（URLError 接不住）
        except (OSError, http.client.HTTPException) as exc:
            if attempt < retries and not emitted:
                time.sleep(2 ** attempt * 5)
                continue
            raise ProviderError(f"网络错误: {exc}") from exc
