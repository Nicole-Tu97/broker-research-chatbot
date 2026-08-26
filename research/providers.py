"""All external calls are concentrated in this file.

Not an abstraction layer -- simply "every external call lives in this file".
Three call sites: transcribe_page / embed / chat. The API shape is locked to
the OpenAI Responses API (/v1/responses): it is the only one that supports
images inside function_call_output (verified empirically).
Zero third-party SDKs: urllib is enough, keeping the dependency surface minimal.
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

# Transcription prompt (v3, benchmark-locked, in Chinese). It was benchmarked and used to
# build the shipped index; translating it would invalidate the benchmark. Rules it enforces:
# never output information absent from the page; every number must exist on the page and
# keep its original surface form; unclear glyphs become [?]; tables rebuilt with exact cell
# placement; each chart described with axes and readable values; headers/footers/watermarks/
# disclaimers ignored; first line reports HAS_VISUAL: true|false.
# Single source of truth; bench/run_bench.py imports these constants rather than copying them.
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
    """Exponential-backoff retry on 429/5xx (a full ingest is an hours-long run;
    transient rate limits should not turn into per-page failures)."""
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
        # OSError covers URLError/TimeoutError/ConnectionResetError/SSLError --
        # a connection dropped while reading the body after a 200 is NOT wrapped
        # as URLError (a gap confirmed in review).
        # HTTPError is an OSError subclass; the branch above must catch it first.
        except (OSError, http.client.HTTPException) as exc:
            if attempt < retries:
                time.sleep(2 ** attempt * 5)
                continue
            raise ProviderError(f"Network error: {exc}") from exc


def _check(res: dict):
    """An incomplete or refused response must never be silently stored as an
    authoritative transcription (review fix)."""
    status = res.get("status")
    if status and status != "completed":
        detail = res.get("incomplete_details") or {}
        raise ProviderError(f"Response not completed: status={status} {detail}")
    for o in res.get("output", []):
        for c in o.get("content", []) if o.get("type") == "message" else []:
            if c.get("type") == "refusal":
                raise ProviderError(f"Model refused: {c.get('refusal', '')[:200]}")


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
    """Single multimodal call -> (markdown, has_visual, usage)."""
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
    # If the metadata line is missing, default to True as the safe direction (sending one
    # extra page image back costs far less than a visual page losing its fallback).
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
    """Locate the visual element on a cited page most relevant to the question
    -> (raw verdict, usage).

    The verdict is one of three: {coordinates} / {"whole_page": true} /
    {"no_figure": true}; None only means parsing failed. Semantic interpretation
    and coordinate validation happen in the caller (chat._figure_crops)."""
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
        raise ProviderError(f"Unexpected embeddings response: {str(res)[:200]}")
    return [d["embedding"] for d in sorted(res["data"], key=lambda d: d["index"])]


def chat(input_items: list[dict], instructions: str, tools: list[dict] | None = None,
         on_delta=None) -> dict:
    """One round trip of the conversation loop. Returns the full response
    object; the loop logic lives in the caller.

    When on_delta is provided, SSE streaming is used: on_delta(str) is called
    for each output_text delta, and the return value is still the full response
    object (taken from the response.completed event) -- the caller's loop logic
    is entirely agnostic to streaming vs non-streaming. Offline paths such as
    evaluate simply omit it, at zero cost."""
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
    """SSE frame parsing: split frames on blank lines, yield the JSON from each
    frame's data: lines. Pure function, testable.

    Tolerant of multi-line data and of non-JSON data (e.g. the [DONE]
    sentinel), which is simply skipped."""
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
    if data_lines:  # final frame with no trailing blank line
        try:
            yield json.loads("\n".join(data_lines))
        except ValueError:
            pass


def _post_stream(path: str, payload: dict, on_delta, timeout: int = 300,
                 retries: int = 3) -> dict:
    """Streaming /responses: forward deltas to on_delta, return the full object
    from response.completed.

    Retry semantics match _post, but retries happen only while no delta has yet
    been forwarded to the client -- retrying after forwarding would produce
    duplicated text in the UI, so at that point we prefer an explicit failure
    for this round."""
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
                        # incomplete (cut off by length/content filter) aligns with the
                        # non-streaming semantics: still return the response object and
                        # let the caller consume the partial output it contains
                        final = data.get("response")
                    elif t == "error" or t.startswith("response.failed"):
                        raise ProviderError(f"Streaming response error: {str(data)[:300]}")
                if final is None:
                    raise ProviderError("Streaming response never received response.completed")
                return final
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode()[:300]
            if exc.code in (429, 500, 502, 503, 504) and attempt < retries:
                time.sleep(2 ** attempt * 5)
                continue
            raise ProviderError(f"HTTP {exc.code}: {detail}") from exc
        except ProviderError:
            raise
        # Same as _post: only OSError catches a connection reset mid-stream
        # (URLError does not)
        except (OSError, http.client.HTTPException) as exc:
            if attempt < retries and not emitted:
                time.sleep(2 ** attempt * 5)
                continue
            raise ProviderError(f"Network error: {exc}") from exc
