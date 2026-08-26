"""Stage 1 tests: pure functions (metadata/ticker/number checks) + full regression
over real-corpus filenames.

Tests for the deterministic core need no API key or database (except the model tests),
echoing the "guarantees come from validation" design.
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
        self.assertEqual(m.claimed_page_count, 14)  # known unreliable; archived only

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
        # Content-side fallback: deck has no filename date, so rely on first-page text
        self.assertEqual(date_from_text("NVIDIA Corp 8 July 2025 ab"), date(2025, 7, 8))
        self.assertEqual(date_from_text("... June 11, 2025 keynote"), date(2025, 6, 11))
        self.assertEqual(date_from_text("North America Equity Research 28 August 2025"),
                         date(2025, 8, 28))
        self.assertIsNone(date_from_text("no date here, just 2025 revenue"))

    def test_whole_corpus_parses(self):
        """Full regression on 30 real corpus files: each parses a broker; reports have dates."""
        corpus = Path(settings.CORPUS_DIR)
        if not corpus.exists():
            self.skipTest("corpus directory missing")
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
        # Key regression: NVIDIA's own deck and multi-industry reports use only company names
        self.assertIn("NVDA", tickers_in("NVIDIA Serving $2 Trillion Europe Automotive"))
        self.assertIn("MSFT", tickers_in("Microsoft capex is rising"))
        self.assertIn("GOOG", tickers_in("Alphabet and Google Cloud"))

    def test_uppercase_common_words_not_tagged(self):
        # Uppercase common words like "AI"/"ON"/"ARM" must not be mistagged
        # (29/30 documents contain uppercase "AI")
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
        self.assertEqual(canon("(123)"), "-123")  # accounting negative
        self.assertIsNone(canon("2025"))  # bare-year whitelist
        # Whitelist narrowed (review fix): 4-digit values with commas/decimals/units
        # are not years and do take part in comparison
        self.assertEqual(canon("2,080"), "2080")
        self.assertEqual(canon("2025.5"), "2025.5")
        self.assertEqual(canon("$2025"), "2025")

    def test_reformat_not_flagged(self):
        # Source of false positives on 37% of pages: reformatting is not suspect
        self.assertEqual(suspect_numbers("PT 170 with 3539639 shares",
                                         "PT $170.00 ... 3,539,639"), [])

    def test_invented_number_flagged(self):
        self.assertEqual(suspect_numbers("PT raised to 250", "PT is 240.00"), ["250"])

    def test_restatement_allowed(self):
        # Chart descriptions may legitimately restate numbers that exist on the page
        self.assertEqual(suspect_numbers("peak 45.6 ... again 45.6", "45.6%"), [])

    def test_empty_raw_text_returns_empty(self):
        self.assertEqual(suspect_numbers("$100T only in pixels", ""), [])

    def test_known_blind_spot_documented(self):
        # Honest boundary: same-value collisions are undetectable — this test
        # documents that fact rather than hiding it
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
        # search_vector is a generated column: writing markdown yields an index value
        self.assertIsNotNone(p.search_vector)


class ToolTests(TestCase):
    """Retrieval tools: synthetic vectors + mocked embed; no external API calls."""

    @classmethod
    def setUpTestData(cls):
        from datetime import date as d

        from .models import Document, Page

        def vec(axis):  # 1024-dim unit vector; direction set by axis
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
        # Undated document (company's own deck with no parseable date on any page —
        # e.g. an NVIDIA earnings deck)
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
        with self._patch_embed(0):  # vector leg points at Barclays p1; FTS hits it too
            out = tools.search_pages("price target raised", k=2)
        top = out["results"][0]
        self.assertEqual((top["broker"], top["page_number"]), ("Barclays", 1))
        ranks = out["trace"]["per_result_ranks"][0]
        self.assertIsNotNone(ranks["vector"])
        self.assertIsNotNone(ranks["fts"])  # both legs hit; both ranks are in the trace

    def test_filters_narrow_results(self):
        from . import tools
        with self._patch_embed(0):
            out = tools.search_pages("price target", brokers=["UBS"], k=5)
        self.assertTrue(all(r["broker"] == "UBS Research" for r in out["results"]))
        with self._patch_embed(0):
            out = tools.search_pages("price target", tickers=["tsm"], k=5)  # case-normalized
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
                         ["Barclays", "UBS Research", "NVIDIA"])  # date ascending, undated last
        self.assertIn("USD 200", out["reports"][0]["first_page"]["markdown"])
        self.assertEqual(out["reports"][0]["ticker_hit_pages"], {"NVDA": [1, 3]})

    def test_date_filter_warns_about_undated_documents(self):
        # Regression: docs with published_date=None were silently excluded by date
        # filters, once costing the model 13 calls ($4.85 a round) failing to find
        # the NVIDIA deck. The tool must give a recovery hint.
        from datetime import date as d
        from . import tools
        out = tools.list_reports(brokers=["NVIDIA"], date_from=d(2025, 6, 1),
                                 date_to=d(2025, 9, 30))
        self.assertEqual(out["count"], 0)          # filter semantics unchanged: still excluded
        self.assertIn("warning", out)              # but the exclusion must be visible
        self.assertIn("EXCLUDED", out["warning"])
        # Dropping the date filter → the recovery path works
        out2 = tools.list_reports(brokers=["NVIDIA"])
        self.assertEqual(out2["count"], 1)
        self.assertNotIn("warning", out2)
        # Same for search_pages (fts mode avoids the embed call)
        out3 = tools.search_pages("Data Center revenue", brokers=["NVIDIA"],
                                  date_from=d(2025, 6, 1), mode="fts")
        self.assertEqual(len(out3["results"]), 0)
        self.assertIn("warning", out3)
        # No warning noise when no date filter is applied
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


class EvaluateScoringTests(SimpleTestCase):
    """Pure scoring helpers of the evaluation harness — no DB, no API."""

    def test_fact_in_answer_scale_words_and_times_sign(self):
        # Answers in other locales may write 亿/万 scale words and × — the scorer must not
        # penalize a correct answer for its surface form (the 131,651 vs 131.651 lesson)
        from .management.commands.evaluate import fact_in_answer
        self.assertTrue(fact_in_answer("17,500", "增量中国收入 **175亿美元**"))   # millions key
        self.assertTrue(fact_in_answer("17.5", "增量中国收入 175 亿美元"))        # billions key
        self.assertTrue(fact_in_answer("15", "价格从 1.5 万美元上调"))            # thousands key
        self.assertTrue(fact_in_answer("100", "最高 1,000 亿美元"))               # 1,000亿 = 100bn
        self.assertTrue(fact_in_answer("3x", "delivers **3×** the speed"))
        self.assertFalse(fact_in_answer("17,500", "收入 17 亿美元"))

    def test_iou_basic(self):
        from .management.commands.evaluate import iou
        self.assertAlmostEqual(iou([0, 0, 50, 50], [0, 0, 50, 50]), 1.0)
        self.assertAlmostEqual(iou([0, 0, 50, 50], [50, 50, 100, 100]), 0.0)
        self.assertAlmostEqual(iou([0, 0, 50, 50], [25, 0, 75, 50]), 1 / 3)

    def test_figure_decision_three_way(self):
        from .management.commands.evaluate import figure_decision_ok
        crop = {"crop": {"x0": 5, "y0": 10, "x1": 60, "y1": 40}}
        full = {"show_page": True}
        none = {}
        # box annotation: overlapping crop passes, wrong crop fails, full page only if whole_page_ok
        self.assertTrue(figure_decision_ok({"box": [5, 10, 60, 42]}, [crop], 0.5))
        self.assertFalse(figure_decision_ok({"box": [50, 50, 90, 90]}, [crop], 0.5))
        self.assertFalse(figure_decision_ok({"box": [5, 10, 60, 42]}, [full], 0.5))
        self.assertTrue(figure_decision_ok({"box": [5, 10, 60, 42], "whole_page_ok": True}, [full], 0.5))
        # no_figure: any embed is a failure
        self.assertTrue(figure_decision_ok({"no_figure": True}, [none], 0.5))
        self.assertFalse(figure_decision_ok({"no_figure": True}, [crop], 0.5))
        self.assertFalse(figure_decision_ok({"no_figure": True}, [full], 0.5))
        # whole_page_ok without box: any embed counts, nothing shown fails
        self.assertTrue(figure_decision_ok({"whole_page_ok": True}, [crop], 0.5))
        self.assertFalse(figure_decision_ok({"whole_page_ok": True}, [none], 0.5))

    def test_label_page_hit_regex(self):
        from .management.commands.evaluate import _LABEL_RE
        m = _LABEL_RE.search("see [Bernstein Research, 2025-07-15, p.4] and [NVIDIA, n.d., p.16]")
        self.assertEqual((m.group(1), m.group(2)), ("Bernstein Research", "4"))
        self.assertEqual(len(_LABEL_RE.findall("[UBS Research, 2025-07-08, p.2]")), 1)

    def test_expected_page_hit(self):
        from .management.commands.evaluate import expected_page_hit
        cited = {("20250925 - Barclays - NVDA - x.pdf", 1), ("NVDA-F2Q26-deck.pdf", 7)}
        self.assertTrue(expected_page_hit([["NVDA-F2Q26", 7]], cited))
        self.assertFalse(expected_page_hit([["NVDA-F2Q26", 8]], cited))
        self.assertFalse(expected_page_hit([["UBS", 1]], cited))

    def test_build_synthetic_attachments(self):
        import base64
        from .management.commands.evaluate import build_attachment
        img, pdf, name = build_attachment({"kind": "synthetic_image", "text": "hello"})
        self.assertIsNone(pdf)
        self.assertEqual(base64.b64decode(img)[:8], b"\x89PNG\r\n\x1a\n")
        img, pdf, name = build_attachment({"kind": "synthetic_pdf", "text": "ACME note"})
        self.assertIsNone(img)
        self.assertEqual(base64.b64decode(pdf)[:5], b"%PDF-")
        self.assertEqual(name, "external.pdf")


class ChatPureTests(TestCase):
    """Deterministic post-processing in the chat layer: no external API calls."""

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
        self.assertEqual(len(frags), 3)  # compound citations split on ';'
        self.assertEqual(frags[0][1].group(1), "Barclays")
        self.assertEqual(int(frags[2][1].group(3)), 1)

    def test_grounding_badge_matches_numbers(self):
        from .chat import grounding_badges
        pages = {("Barclays", "2025-06-17", 1): self.p1}
        answer = "Barclays 将 PT 从 $170 上调至 $200 [Barclays, 2025-06-17, p.1]"
        badges = grounding_badges(answer, pages)
        self.assertEqual(badges[0]["status"], "grounded")
        self.assertIn("200", badges[0]["matched_numbers"])
        # Citing a page absent from this turn's retrieval results → unknown
        # (guards against fabricated citations)
        badges = grounding_badges("[UBS, 2025-07-08, p.4]", pages)
        self.assertEqual(badges[0]["status"], "unknown")

    def test_citation_human_date_formats_resolve(self):
        # Observed bug: the model wrote "September 2025" instead of ISO and the old
        # parser dropped the whole citation → links/badges/Sources all vanished.
        # The parsing layer must be looser than the prompt wording.
        from .chat import grounding_badges
        pages = {("Barclays", "2025-06-17", 1): self.p1}
        for cite in ("[Barclays, June 2025, p.1]",
                     "[Barclays, 2025-06, p.1]",   # YYYY-MM, seen on undated decks
                     "[Barclays, Jun 2025, p.1]",
                     "[Barclays, June 17, 2025, p.1]",
                     "[Barclays, 2025, p.1]",
                     "[Barclays, n.d., p.1]"):
            badges = grounding_badges(f"PT $200 {cite}", pages)
            self.assertEqual(badges[0]["status"], "grounded", cite)
        # Month mismatch → must not be misattributed to a different report
        badges = grounding_badges("PT $200 [Barclays, July 2025, p.1]", pages)
        self.assertEqual(badges[0]["status"], "unknown")
        # Document itself undated (e.g. NVIDIA's own deck) → the date cannot be
        # checked, so it is not a veto; broker + page number + this turn's retrieval
        # set remain the hard anti-fabrication gate
        pages_nd = {("NVIDIA", "None", 1): self.p1}
        badges = grounding_badges("$200 [NVIDIA, September 2025, p.1]", pages_nd)
        self.assertEqual(badges[0]["status"], "grounded")

    def test_badge_carries_has_visual_for_inline_figures(self):
        # Front-end inline original-page images (requirement: "surface the
        # original asset") rely on this field
        from .chat import grounding_badges
        pages = {("Barclays", "2025-06-17", 1): self.p1}
        badges = grounding_badges("PT $200 [Barclays, 2025-06-17, p.1]", pages)
        self.assertIn("has_visual", badges[0])

    def test_badge_ignores_numbers_inside_citation_labels(self):
        # A text-only answer must not be flagged just because its citation says "p.35":
        # page numbers and dates inside [ ... ] are not numeric claims about the page
        from .chat import grounding_badges
        pages = {("Barclays", "2025-06-17", 1): self.p1}
        badges = grounding_badges("The library is LightOn. [Barclays, 2025-06-17, p.1]", pages)
        self.assertEqual(badges[0]["status"], "grounded")

    def test_prior_pages_rebuilt_from_stored_tool_outputs(self):
        # Follow-up turns answered from memory must still verify citations against pages
        # retrieved in earlier turns (otherwise every multi-turn answer gets an unknown badge)
        import json as _json
        from .chat import _prior_pages
        msgs = [{"type": "function_call_output", "call_id": "c1",
                 "output_text": _json.dumps({"results": [
                     {"document_id": self.old.id, "page_number": 1}]}), "image_refs": []}]
        pages = _prior_pages(msgs)
        self.assertIn(("Barclays", "2025-06-17", 1), pages)
        self.assertEqual(_prior_pages([{"role": "user", "content": []}]), {})

    def test_recency_label_flags_superseded_citation(self):
        from .chat import grounding_badges, recency_labels
        pages = {("Barclays", "2025-06-17", 1): self.p1}
        badges = grounding_badges("PT $200 [Barclays, 2025-06-17, p.1]", pages)
        labels = recency_labels(badges)
        self.assertEqual(len(labels), 1)
        self.assertEqual(labels[0]["superseded_by"], "2025-09-25")

    def test_sse_parser_frames_and_tolerance(self):
        # SSE frame parsing: framing, multi-line data, tolerance for non-JSON
        # sentinels, and no trailing blank line
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
        self.assertEqual(len(frames), 3)  # [DONE] tolerated and skipped
        self.assertEqual(frames[0]["delta"], "Hel")
        self.assertEqual(frames[1]["delta"], "lo")  # multi-line data merged
        self.assertEqual(frames[2]["type"], "response.completed")  # received without trailing blank

    def test_run_turn_offline_path_unchanged(self):
        # No emit passed → streaming=False → providers.chat must receive on_delta=None
        # (guards the zero streaming overhead of evaluate's offline path)
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
        # Lost-update regression (confirmed in review): run_turn holds the request
        # thread's stale snapshot while a concurrent turn has already persisted —
        # after the atomic refetch-and-append, both turns' messages must survive
        from unittest.mock import patch
        from . import chat as chat_mod
        from .models import Conversation
        conv = Conversation.objects.create()
        stale = Conversation.objects.get(id=conv.id)  # the snapshot run_turn holds
        conv.messages = conv.messages + [
            {"role": "user", "content": [{"type": "input_text", "text": "OTHER-TURN"}]}]
        conv.save()  # the concurrent turn writes first
        fake = {"usage": {}, "output": [{"type": "message",
                "content": [{"type": "output_text", "text": "A2"}]}]}
        with patch.object(chat_mod.providers, "chat", return_value=fake):
            chat_mod.run_turn(stale, "SECOND-TURN")
        final = str(Conversation.objects.get(id=conv.id).messages)
        self.assertIn("OTHER-TURN", final)   # the old implementation lost this to an overwrite
        self.assertIn("SECOND-TURN", final)

    def test_tool_output_images_deduped_within_turn(self):
        # Cost fix: attach each page's original image only once per turn
        # (api_input accumulates; the first copy stays in context)
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
        self.assertEqual(n_img(api2), 0)  # same page is not attached a second time
        self.assertEqual(sent, {(1, 5)})

    def test_valid_crop_guardrails(self):
        # Deterministic bbox validation — bad boxes fall back to the full page;
        # good boxes expand 2% and clamp to the bounds
        from .chat import _valid_crop
        self.assertIsNone(_valid_crop(None))
        self.assertIsNone(_valid_crop({"x0": 10, "y0": 10}))            # missing coords
        self.assertIsNone(_valid_crop({"x0": 50, "y0": 10, "x1": 40, "y1": 90}))  # degenerate
        self.assertIsNone(_valid_crop({"x0": 0, "y0": 0, "x1": 5, "y1": 5}))      # too small
        self.assertIsNone(_valid_crop({"x0": 0, "y0": 0, "x1": 100, "y1": 99}))   # ~ full page
        c = _valid_crop({"x0": 5, "y0": 8, "x1": 50, "y1": 86})  # box measured in probe
        self.assertEqual(c, {"x0": 3.0, "y0": 6.0, "x1": 52.0, "y1": 88.0})
        c2 = _valid_crop({"x0": 0, "y0": 0, "x1": 50, "y1": 50})  # expansion stays in bounds
        self.assertEqual((c2["x0"], c2["y0"]), (0, 0))

    def test_figure_locator_three_way_decision(self):
        # Locator picks one of three — coords → crop; whole page is the figure →
        # show_page; text-only page → no tag; figure found but coords fail validation
        # → fall back to show_page (the figure is real; full page beats nothing)
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
                (None, None),  # parse failure → no figure inserted
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

    def test_whole_page_decision_denied_on_text_heavy_pages(self):
        # Deterministic guard: "whole_page" from the locator is honored only for
        # pixel-dominant pages; a report cover with a long text layer gets no image
        import tempfile
        from pathlib import Path
        from unittest.mock import patch
        from django.test import override_settings
        from . import chat as chat_mod
        from .models import Document, Page
        doc = Document.objects.create(filename="wp.pdf", content_hash="w" * 64, broker="X")
        cover = Page.objects.create(document=doc, page_number=1, png_path="c.png",
                                    raw_text="lorem " * 200, markdown="x", has_visual=True)
        slide = Page.objects.create(document=doc, page_number=2, png_path="c.png",
                                    raw_text="", markdown="chart", has_visual=True)
        with tempfile.TemporaryDirectory() as td:
            (Path(td) / "c.png").write_bytes(b"x")
            with override_settings(PAGE_ASSET_DIR=Path(td)), \
                 patch.object(chat_mod.providers, "figure_bbox", return_value=({"whole_page": True}, {})):
                b_cover = [{"citation": "[X, n.d., p.1]", "document_id": doc.id, "page_number": 1,
                            "png_path": "c.png", "has_visual": True}]
                b_slide = [{"citation": "[X, n.d., p.2]", "document_id": doc.id, "page_number": 2,
                            "png_path": "c.png", "has_visual": True}]
                chat_mod._figure_crops("q", b_cover, lambda e: None)
                chat_mod._figure_crops("q", b_slide, lambda e: None)
        self.assertNotIn("show_page", b_cover[0])   # text-heavy cover → no image
        self.assertTrue(b_slide[0].get("show_page"))  # pixel-dominant slide → full page ok
        # located-but-invalid box (too small) on a text-heavy cover: also no image
        with tempfile.TemporaryDirectory() as td:
            (Path(td) / "c.png").write_bytes(b"x")
            with override_settings(PAGE_ASSET_DIR=Path(td)), \
                 patch.object(chat_mod.providers, "figure_bbox",
                              return_value=({"x0": 70, "y0": 20, "x1": 75, "y1": 26}, {})):
                b_small = [{"citation": "[X, n.d., p.1]", "document_id": doc.id, "page_number": 1,
                            "png_path": "c.png", "has_visual": True}]
                chat_mod._figure_crops("q", b_small, lambda e: None)
        self.assertNotIn("show_page", b_small[0]); self.assertNotIn("crop", b_small[0])

    def test_page_image_crop_endpoint(self):
        # ?crop= re-renders the region from the PDF; invalid coords fall back to
        # the full page (no 404, no crash)
        from django.conf import settings as st
        from .models import Document, Page
        pdfs = sorted(st.CORPUS_DIR.glob("*.pdf")) if st.CORPUS_DIR.exists() else []
        if not pdfs:
            self.skipTest("corpus directory missing")
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
        self.assertEqual(r2.status_code, 404)  # bad coords → full-page fallback, but png missing → 404

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
        # Tool traffic (incl. image refs) never replays across turns: reasoning
        # models' reasoning-pairing constraint + cost
        self.assertEqual([m.get("role") for m in api], ["user", "assistant"])

class ValidationReportTemplateTests(SimpleTestCase):
    """The report's module structure was frozen with the reviewer on 2026-08-26.
    Structure changes must be deliberate: update the marker list here alongside
    render_report. Also guards against editing the .md by hand — the committed
    file must be byte-identical to what the generator produces."""

    MARKERS = [
        "# Validation Report",
        "## Retrieval quality — golden-set (reference-based) evaluation",
        "**What this is.**",
        "**How the test runs.**",
        "**The six question types.**",
        "**The four columns.**",
        "| Category | dense | fts | hybrid |",
        "| non-English questions",
        "| **Mean (",
        "**Acceptance bar — judged on the production (agentic) column.**",
        "- Overall mean ≥ 0.90:",
        "**One weak spot to note.**",
        "DESIGN.md §7",
        "## Behavior validation (end-to-end)",
        "**What is being tested.**",
        "**What was asked.**",
        "**How answers are graded — preset rules, never an LLM judging an LLM.**",
        "1. **Must contain**",
        "2. **Must NOT contain**",
        "3. **Box match**",
        "- **Correctness (P7b)**",
        "- **Unsupported-number rate (P7a)**",
        "- **Hallucination rate (P8)**",
        "- **Multi-turn context carry**",
        "- **Attachment input**",
        "- **Figure-crop accuracy**",
        "- **Reproducibility (P9)**",
        "- **Robustness (P10)**",
        "- **Injection resistance (P11)**",
        "- **Watermark & contact-info leak (P12)**",
        "Behavior validation total API cost",
    ]

    def test_report_structure_and_sync(self):
        import json as jsonlib
        from .management.commands.evaluate import EVAL, Command
        report = Command().render_report(jsonlib.loads((EVAL / "results.json").read_text()))
        pos = -1
        for marker in self.MARKERS:
            i = report.find(marker, pos + 1)
            self.assertGreater(i, pos, f"report section missing or out of order: {marker!r}")
            pos = i
        self.assertEqual(
            report, (EVAL / "validation_report.md").read_text(),
            "eval/validation_report.md is out of sync with the generator — regenerate it "
            "via render_report instead of editing the file by hand")
