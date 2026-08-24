"""阶段 1 测试：纯函数（元数据/ticker/数字校验）+ 真实语料文件名全量回归。

确定性核心的测试无需 API key 与数据库（除模型测试外），
呼应"保证来自校验"的设计（§4.3）。
"""

from datetime import date
from pathlib import Path

from django.conf import settings
from django.test import SimpleTestCase, TestCase

from .metadata import date_from_text, parse_filename
from .numeric import canon, numbers_in, suspect_numbers
from .tickers import extract_ticker_pages, tickers_in


class MetadataTests(SimpleTestCase):
    def test_standard_broker_filename(self):
        m = parse_filename(
            "20250617 - Barclays - NVDA - Target Up,Estimates Up - U S Semiconductors Semiconductor - 14 pages")
        self.assertEqual(m.broker, "Barclays")
        self.assertEqual(m.published_date, date(2025, 6, 17))
        self.assertEqual(m.ticker, "NVDA")
        self.assertEqual(m.claimed_page_count, 14)  # 已知不可信，仅存档

    def test_no_ticker_multi_industry(self):
        m = parse_filename(
            "20250617 - BofA Global Research - Industrials Multi Industry AI - 26 pages")
        self.assertEqual(m.broker, "BofA Global Research")
        self.assertIsNone(m.ticker)
        self.assertIn("Industrials", m.title)

    def test_title_containing_dash_segments(self):
        m = parse_filename(
            "20250925 - Barclays - NVDA - Revision - U S Semiconductors Semiconductor - 18 pages")
        self.assertEqual(m.ticker, "NVDA")
        self.assertTrue(m.title.startswith("Revision"))

    def test_nvidia_deck_without_pattern(self):
        m = parse_filename("GTC-Paris-2025-Keynote")
        self.assertEqual(m.broker, "NVIDIA")
        self.assertIsNone(m.published_date)
        self.assertEqual(m.ticker, "NVDA")

    def test_date_from_first_page_text(self):
        # §6.1 步骤 1 内容侧兜底：deck 无文件名日期，靠首页文本
        self.assertEqual(date_from_text("NVIDIA Corp 8 July 2025 ab"), date(2025, 7, 8))
        self.assertEqual(date_from_text("... June 11, 2025 keynote"), date(2025, 6, 11))
        self.assertEqual(date_from_text("North America Equity Research 28 August 2025"),
                         date(2025, 8, 28))
        self.assertIsNone(date_from_text("no date here, just 2025 revenue"))

    def test_whole_corpus_parses(self):
        """真实语料 30 份全量回归：每份都能解析出 broker，研报类都有日期。"""
        corpus = Path(settings.CORPUS_DIR)
        if not corpus.exists():
            self.skipTest("语料目录不存在")
        pdfs = sorted(corpus.glob("*.pdf"))
        self.assertGreaterEqual(len(pdfs), 30)
        for pdf in pdfs:
            m = parse_filename(pdf.stem)
            self.assertTrue(m.broker, pdf.name)
            if pdf.stem[:8].isdigit():
                self.assertIsNotNone(m.published_date, pdf.name)


class TickerTests(SimpleTestCase):
    def test_symbol_match_case_sensitive(self):
        self.assertIn("NVDA", tickers_in("We raise NVDA PT to $240"))
        self.assertNotIn("NVDA", tickers_in("nvda lowercase noise"))

    def test_company_name_maps_to_ticker(self):
        # 关键回归：NVIDIA 官方 deck 与多行业报告只写公司名（DECISION-LOG §七.4）
        self.assertIn("NVDA", tickers_in("NVIDIA Serving $2 Trillion Europe Automotive"))
        self.assertIn("MSFT", tickers_in("Microsoft capex is rising"))
        self.assertIn("GOOG", tickers_in("Alphabet and Google Cloud"))

    def test_uppercase_common_words_not_tagged(self):
        # "AI"/"ON"/"ARM" 这类大写普通词不得误标（29/30 份文档含大写 AI）
        found = tickers_in("AI FACTORY requires ARM-based designs ON premises")
        self.assertEqual(found, set())

    def test_ticker_pages_indexing(self):
        hits = extract_ticker_pages(["Intel and AMD compete", "", "NVIDIA wins"])
        self.assertEqual(hits["INTC"], [1])
        self.assertEqual(hits["AMD"], [1])
        self.assertEqual(hits["NVDA"], [3])


class NumericTests(SimpleTestCase):
    def test_canon_normalizations(self):
        self.assertEqual(canon("$3,539,639"), "3539639")
        self.assertEqual(canon("170.00"), "170")
        self.assertEqual(canon("45.6%"), "45.6")
        self.assertEqual(canon("(123)"), "-123")  # 会计负数
        self.assertIsNone(canon("2025"))  # 裸年份白名单
        # 白名单收窄（评审修正）：带逗号/小数/单位的 4 位值不是年份，参与比对
        self.assertEqual(canon("2,080"), "2080")
        self.assertEqual(canon("2025.5"), "2025.5")
        self.assertEqual(canon("$2025"), "2025")

    def test_reformat_not_flagged(self):
        # 37% 页面的误报来源（DECISION-LOG §七.3）：换写法不算可疑
        self.assertEqual(suspect_numbers("PT 170 with 3539639 shares",
                                         "PT $170.00 ... 3,539,639"), [])

    def test_invented_number_flagged(self):
        self.assertEqual(suspect_numbers("PT raised to 250", "PT is 240.00"), ["250"])

    def test_restatement_allowed(self):
        # 图表描述合理复述页面上存在的数字
        self.assertEqual(suspect_numbers("peak 45.6 ... again 45.6", "45.6%"), [])

    def test_empty_raw_text_returns_empty(self):
        self.assertEqual(suspect_numbers("$100T only in pixels", ""), [])

    def test_known_blind_spot_documented(self):
        # 诚实边界（§4.3/§8.1.1）：同值碰撞不可检——此测试记录该事实而非掩盖
        self.assertEqual(suspect_numbers("PT is 200.00", "240.00 ... 200.00"), [])


class ModelTests(TestCase):
    def test_document_page_roundtrip(self):
        from .models import Document, Page

        d = Document.objects.create(filename="t.pdf", content_hash="x" * 64,
                                    broker="Barclays", tickers=["NVDA"])
        Page.objects.create(document=d, page_number=1,
                            markdown="Price target raised to $240")
        p = Page.objects.get(document=d, page_number=1)
        self.assertEqual(p.page_number, 1)
        # search_vector 为生成列：写入 markdown 即有索引值
        self.assertIsNotNone(p.search_vector)


class ToolTests(TestCase):
    """检索工具（§6.2）：合成向量 + mock embed，不触外部 API。"""

    @classmethod
    def setUpTestData(cls):
        from datetime import date as d

        from .models import Document, Page

        def vec(axis):  # 1024 维单位向量，方向由 axis 决定
            v = [0.0] * 1024
            v[axis] = 1.0
            return v

        cls.barclays = Document.objects.create(
            filename="b.pdf", content_hash="b" * 64, broker="Barclays",
            published_date=d(2025, 6, 17), tickers=["NVDA"],
            ticker_pages={"NVDA": [1, 3]}, status=Document.Status.DONE, page_count=2)
        cls.ubs = Document.objects.create(
            filename="u.pdf", content_hash="u" * 64, broker="UBS Research",
            published_date=d(2025, 7, 8), tickers=["NVDA", "TSM"],
            ticker_pages={"NVDA": [1]}, status=Document.Status.DONE, page_count=1)
        Page.objects.create(document=cls.barclays, page_number=1, embedding=vec(0),
                            markdown="Price target raised to USD 200 from USD 170")
        Page.objects.create(document=cls.barclays, page_number=2, embedding=vec(1),
                            markdown="Blackwell capacity ramp discussion",
                            numeric_flags=["999"])
        Page.objects.create(document=cls.ubs, page_number=1, embedding=vec(2),
                            markdown="12-month rating Buy, price target US$175.00")
        # 无日期文档（公司自家 deck，页面上无任何可解析日期——如 NVIDIA 季报 deck）
        cls.deck = Document.objects.create(
            filename="d.pdf", content_hash="d" * 64, broker="NVIDIA",
            published_date=None, tickers=["NVDA"],
            ticker_pages={"NVDA": [1]}, status=Document.Status.DONE, page_count=1)
        Page.objects.create(document=cls.deck, page_number=1, embedding=vec(3),
                            markdown="Data Center revenue $41.1B up 56% Y/Y")

    def _patch_embed(self, axis):
        from unittest.mock import patch
        v = [0.0] * 1024
        v[axis] = 1.0
        return patch("research.tools.providers.embed", return_value=[v])

    def test_hybrid_prefers_agreement_and_traces_legs(self):
        from . import tools
        with self._patch_embed(0):  # 向量一路指向 Barclays p1；FTS 也命中它
            out = tools.search_pages("price target raised", k=2)
        top = out["results"][0]
        self.assertEqual((top["broker"], top["page_number"]), ("Barclays", 1))
        ranks = out["trace"]["per_result_ranks"][0]
        self.assertIsNotNone(ranks["vector"])
        self.assertIsNotNone(ranks["fts"])  # 双路命中，排名都在追踪里

    def test_filters_narrow_results(self):
        from . import tools
        with self._patch_embed(0):
            out = tools.search_pages("price target", brokers=["UBS"], k=5)
        self.assertTrue(all(r["broker"] == "UBS Research" for r in out["results"]))
        with self._patch_embed(0):
            out = tools.search_pages("price target", tickers=["tsm"], k=5)  # 大小写归一
        self.assertTrue(all(r["broker"] == "UBS Research" for r in out["results"]))

    def test_suspect_numbers_surface_in_payload(self):
        from . import tools
        with self._patch_embed(1):
            out = tools.search_pages("Blackwell capacity", k=1)
        self.assertEqual(out["results"][0].get("suspect_numbers"), ["999"])

    def test_list_reports_ordered_with_first_page(self):
        from . import tools
        out = tools.list_reports(tickers=["NVDA"])
        self.assertEqual(out["count"], 3)
        self.assertEqual([r["broker"] for r in out["reports"]],
                         ["Barclays", "UBS Research", "NVIDIA"])  # 日期升序，无日期排最后
        self.assertIn("USD 200", out["reports"][0]["first_page"]["markdown"])
        self.assertEqual(out["reports"][0]["ticker_hit_pages"], {"NVDA": [1, 3]})

    def test_date_filter_warns_about_undated_documents(self):
        # §十七 回归:published_date=None 的文档被日期过滤静默排除,曾让模型
        # 13 次调用找不到 NVIDIA deck($4.85 一轮)。工具必须给出恢复提示。
        from datetime import date as d
        from . import tools
        out = tools.list_reports(brokers=["NVIDIA"], date_from=d(2025, 6, 1),
                                 date_to=d(2025, 9, 30))
        self.assertEqual(out["count"], 0)          # 过滤语义不变:仍被排除
        self.assertIn("warning", out)              # 但排除必须可见
        self.assertIn("EXCLUDED", out["warning"])
        # 去掉日期过滤 → 恢复路径成立
        out2 = tools.list_reports(brokers=["NVIDIA"])
        self.assertEqual(out2["count"], 1)
        self.assertNotIn("warning", out2)
        # search_pages 同款(fts 模式避免 embed 调用)
        out3 = tools.search_pages("Data Center revenue", brokers=["NVIDIA"],
                                  date_from=d(2025, 6, 1), mode="fts")
        self.assertEqual(len(out3["results"]), 0)
        self.assertIn("warning", out3)
        # 无日期过滤时不应有提示噪音
        out4 = tools.search_pages("Data Center revenue", brokers=["NVIDIA"], mode="fts")
        self.assertTrue(out4["results"])
        self.assertNotIn("warning", out4)

    def test_dispatch_parses_dates(self):
        from . import tools
        out = tools.dispatch("list_reports", {"date_from": "2025-07-01"})
        self.assertEqual(out["count"], 1)
        self.assertEqual(out["reports"][0]["broker"], "UBS Research")
        with self.assertRaises(ValueError):
            tools.dispatch("nope", {})


class ChatPureTests(TestCase):
    """chat 层确定性后处理（§6.3）：不触任何外部 API。"""

    @classmethod
    def setUpTestData(cls):
        from datetime import date as d

        from .models import Document, Page

        cls.old = Document.objects.create(
            filename="o.pdf", content_hash="o" * 64, broker="Barclays",
            published_date=d(2025, 6, 17), tickers=["NVDA"],
            status=Document.Status.DONE)
        cls.new = Document.objects.create(
            filename="n.pdf", content_hash="n" * 64, broker="Barclays",
            published_date=d(2025, 9, 25), tickers=["NVDA"],
            status=Document.Status.DONE)
        cls.p1 = Page.objects.create(
            document=cls.old, page_number=1, png_path="1_1.png",
            raw_text="Price Target USD 200.00 raised 18% from USD 170.00",
            markdown="PT USD 200.00 (prior 170.00)")

    def test_citation_parsing_incl_compound(self):
        from .chat import _citation_fragments
        frags = list(_citation_fragments(
            "目标价上调 [Barclays, 2025-06-17, p.1]，累计 "
            "[Barclays, 2025-06-17, p.3; Barclays, 2025-09-25, p.1]"))
        self.assertEqual(len(frags), 3)  # 复合引用按 ';' 拆条
        self.assertEqual(frags[0][1].group(1), "Barclays")
        self.assertEqual(int(frags[2][1].group(3)), 1)

    def test_grounding_badge_matches_numbers(self):
        from .chat import grounding_badges
        pages = {("Barclays", "2025-06-17", 1): self.p1}
        answer = "Barclays 将 PT 从 $170 上调至 $200 [Barclays, 2025-06-17, p.1]"
        badges = grounding_badges(answer, pages)
        self.assertEqual(badges[0]["status"], "grounded")
        self.assertIn("200", badges[0]["matched_numbers"])
        # 引用了不在本轮检索结果里的页 → unknown（防伪造引用）
        badges = grounding_badges("[UBS, 2025-07-08, p.4]", pages)
        self.assertEqual(badges[0]["status"], "unknown")

    def test_citation_human_date_formats_resolve(self):
        # 实测 bug：模型写 "September 2025" 而非 ISO，旧解析器整条丢弃 →
        # 链接/徽章/Sources 全部消失。解析层必须比 prompt 措辞更宽。
        from .chat import grounding_badges
        pages = {("Barclays", "2025-06-17", 1): self.p1}
        for cite in ("[Barclays, June 2025, p.1]",
                     "[Barclays, Jun 2025, p.1]",
                     "[Barclays, June 17, 2025, p.1]",
                     "[Barclays, 2025, p.1]",
                     "[Barclays, n.d., p.1]"):
            badges = grounding_badges(f"PT $200 {cite}", pages)
            self.assertEqual(badges[0]["status"], "grounded", cite)
        # 月份对不上 → 不得张冠李戴到别的报告
        badges = grounding_badges("PT $200 [Barclays, July 2025, p.1]", pages)
        self.assertEqual(badges[0]["status"], "unknown")
        # 文档本身无日期（如 NVIDIA 自家 deck）→ 日期无从核对，不作为否决项；
        # broker+页号+本轮检索集仍是防伪造硬门
        pages_nd = {("NVIDIA", "None", 1): self.p1}
        badges = grounding_badges("$200 [NVIDIA, September 2025, p.1]", pages_nd)
        self.assertEqual(badges[0]["status"], "grounded")

    def test_badge_carries_has_visual_for_inline_figures(self):
        # 前端行内原页图（需求 "surface the original asset"）依赖此字段
        from .chat import grounding_badges
        pages = {("Barclays", "2025-06-17", 1): self.p1}
        badges = grounding_badges("PT $200 [Barclays, 2025-06-17, p.1]", pages)
        self.assertIn("has_visual", badges[0])

    def test_recency_label_flags_superseded_citation(self):
        from .chat import grounding_badges, recency_labels
        pages = {("Barclays", "2025-06-17", 1): self.p1}
        badges = grounding_badges("PT $200 [Barclays, 2025-06-17, p.1]", pages)
        labels = recency_labels(badges)
        self.assertEqual(len(labels), 1)
        self.assertEqual(labels[0]["superseded_by"], "2025-09-25")

    def test_sse_parser_frames_and_tolerance(self):
        # SSE 帧解析（§十五 流式）：分帧、多行 data、非 JSON 哨兵容错、无结尾空行
        import io
        from .providers import _sse_data
        stream = io.BytesIO(
            b'event: response.output_text.delta\n'
            b'data: {"type": "response.output_text.delta", "delta": "Hel"}\n\n'
            b'data: {"type": "response.output_text.delta",\n'
            b'data:  "delta": "lo"}\n\n'
            b'data: [DONE]\n\n'
            b'data: {"type": "response.completed", "response": {"usage": {}}}'
        )
        frames = list(_sse_data(stream))
        self.assertEqual(len(frames), 3)  # [DONE] 被容错跳过
        self.assertEqual(frames[0]["delta"], "Hel")
        self.assertEqual(frames[1]["delta"], "lo")  # 跨行 data 合并
        self.assertEqual(frames[2]["type"], "response.completed")  # 无结尾空行也收到

    def test_run_turn_offline_path_unchanged(self):
        # emit 不传 → streaming=False → providers.chat 必须收到 on_delta=None
        # （evaluate 离线路径零流式开销的守卫）
        from unittest.mock import patch
        from . import chat as chat_mod
        from .models import Conversation
        conv = Conversation.objects.create()
        fake = {"usage": {"input_tokens": 1, "output_tokens": 1},
                "output": [{"type": "message",
                            "content": [{"type": "output_text", "text": "hi"}]}]}
        with patch.object(chat_mod.providers, "chat", return_value=fake) as m:
            chat_mod.run_turn(conv, "q")
        self.assertIsNone(m.call_args.kwargs.get("on_delta"))

    def test_conversation_append_survives_concurrent_writer(self):
        # lost-update 回归（§十五 审查确认）：run_turn 持有的是请求线程的过期快照,
        # 期间另一并发轮已落库——原子重取追加后,两轮的消息都必须幸存
        from unittest.mock import patch
        from . import chat as chat_mod
        from .models import Conversation
        conv = Conversation.objects.create()
        stale = Conversation.objects.get(id=conv.id)  # run_turn 拿到的快照
        conv.messages = conv.messages + [
            {"role": "user", "content": [{"type": "input_text", "text": "OTHER-TURN"}]}]
        conv.save()  # 并发轮先写
        fake = {"usage": {}, "output": [{"type": "message",
                "content": [{"type": "output_text", "text": "A2"}]}]}
        with patch.object(chat_mod.providers, "chat", return_value=fake):
            chat_mod.run_turn(stale, "SECOND-TURN")
        final = str(Conversation.objects.get(id=conv.id).messages)
        self.assertIn("OTHER-TURN", final)   # 旧实现在这里被覆盖丢失
        self.assertIn("SECOND-TURN", final)

    def test_tool_output_images_deduped_within_turn(self):
        # §十七 成本修复:轮内同一页原图只附一次(api_input 累积,首份仍在上下文)
        import tempfile
        from pathlib import Path
        from django.test import override_settings
        from .chat import _tool_output_items
        with tempfile.TemporaryDirectory() as td:
            (Path(td) / "x.png").write_bytes(b"fakepng")
            payload = {"results": [{"document_id": 1, "page_number": 5,
                                    "has_visual": True, "png_path": "x.png"}]}
            with override_settings(PAGE_ASSET_DIR=Path(td)):
                sent: set = set()
                api1, _ = _tool_output_items("c1", payload, sent)
                api2, _ = _tool_output_items("c2", payload, sent)
        n_img = lambda item: sum(1 for c in item["output"] if c["type"] == "input_image")
        self.assertEqual(n_img(api1), 1)
        self.assertEqual(n_img(api2), 0)  # 同页第二次不再附
        self.assertEqual(sent, {(1, 5)})

    def test_valid_crop_guardrails(self):
        # §十八:bbox 的确定性校验——坏框回退整页,好框外扩 2% 并夹回边界
        from .chat import _valid_crop
        self.assertIsNone(_valid_crop(None))
        self.assertIsNone(_valid_crop({"x0": 10, "y0": 10}))            # 缺坐标
        self.assertIsNone(_valid_crop({"x0": 50, "y0": 10, "x1": 40, "y1": 90}))  # 退化
        self.assertIsNone(_valid_crop({"x0": 0, "y0": 0, "x1": 5, "y1": 5}))      # 太小
        self.assertIsNone(_valid_crop({"x0": 0, "y0": 0, "x1": 100, "y1": 99}))   # ≈整页
        c = _valid_crop({"x0": 5, "y0": 8, "x1": 50, "y1": 86})  # probe 实测框
        self.assertEqual(c, {"x0": 3.0, "y0": 6.0, "x1": 52.0, "y1": 88.0})
        c2 = _valid_crop({"x0": 0, "y0": 0, "x1": 50, "y1": 50})  # 外扩不越界
        self.assertEqual((c2["x0"], c2["y0"]), (0, 0))

    def test_figure_locator_three_way_decision(self):
        # §二十一:定位器三选一——坐标→crop;整页即图→show_page;纯文字页→不标;
        # 找到图但坐标没过校验→退回 show_page(图确实在,整页比不给强)
        import tempfile
        from pathlib import Path
        from unittest.mock import patch
        from django.test import override_settings
        from . import chat as chat_mod
        with tempfile.TemporaryDirectory() as td:
            (Path(td) / "a.png").write_bytes(b"x")
            cases = [
                ({"x0": 5, "y0": 8, "x1": 50, "y1": 86}, "crop"),
                ({"whole_page": True}, "show_page"),
                ({"no_figure": True}, None),
                ({"x0": 0, "y0": 0, "x1": 3, "y1": 3}, "show_page"),
                (None, None),  # 解析失败 → 不插图
            ]
            with override_settings(PAGE_ASSET_DIR=Path(td)):
                for ret, expect in cases:
                    bs = [{"citation": "[X, n.d., p.1]", "document_id": 1,
                           "page_number": 1, "png_path": "a.png", "has_visual": True}]
                    with patch.object(chat_mod.providers, "figure_bbox",
                                      return_value=(ret, {})):
                        chat_mod._figure_crops("q", bs, lambda e: None)
                    if expect is None:
                        self.assertNotIn("crop", bs[0], ret)
                        self.assertNotIn("show_page", bs[0], ret)
                    else:
                        self.assertIn(expect, bs[0], ret)

    def test_page_image_crop_endpoint(self):
        # §十八:?crop= 从 PDF 重渲区域;坐标非法回退整页(不 404 不炸)
        from django.conf import settings as st
        from .models import Document, Page
        pdfs = sorted(st.CORPUS_DIR.glob("*.pdf")) if st.CORPUS_DIR.exists() else []
        if not pdfs:
            self.skipTest("语料目录不存在")
        doc = Document.objects.create(
            filename=pdfs[0].name, content_hash="e" * 64, broker="X",
            status=Document.Status.DONE)
        Page.objects.create(document=doc, page_number=1, png_path="does-not-exist.png",
                            markdown="x")
        r = self.client.get(f"/page-image/{doc.id}/1", {"crop": "10,10,60,60"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r["Content-Type"], "image/png")
        body = b"".join(r.streaming_content) if r.streaming else r.content
        self.assertEqual(body[:8], b"\x89PNG\r\n\x1a\n")
        r2 = self.client.get(f"/page-image/{doc.id}/1", {"crop": "60,10,10,60"})
        self.assertEqual(r2.status_code, 404)  # 坏坐标→兜底整页,但 png 文件不存在→404

    def test_history_keeps_only_role_messages(self):
        from .chat import _history_to_api
        hist = [
            {"role": "user", "content": [{"type": "input_text", "text": "Q1"}]},
            {"type": "function_call", "call_id": "c1", "name": "search_pages",
             "arguments": "{}"},
            {"type": "function_call_output", "call_id": "c1",
             "output_text": "{\"results\": []}",
             "image_refs": [{"document_id": 1, "page_number": 1, "png_path": "x.png"}]},
            {"role": "assistant", "content": [{"type": "output_text", "text": "A1"}]},
        ]
        api = _history_to_api(hist)
        # 工具流量（含图像引用）不跨轮回传：推理模型的 reasoning 配对约束 + 成本
        self.assertEqual([m.get("role") for m in api], ["user", "assistant"])
