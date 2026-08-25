# Validation Report (deterministic scoring against the preregistered thresholds in DESIGN.md Appendix A)

Generated: 2026-08-25 20:00 · zero LLM-judge scoring throughout

## Retrieval ablation (recall@10, raw question text as query)

| Category | dense | fts | hybrid |
|---|---|---|---|
| comparison_timeseries | 0.587 | 0.426 | 0.541 |
| deep_page_recovery | 1.0 | 0.818 | 0.909 |
| pure_chart | 0.812 | 0.625 | 0.938 |
| simple_qa | 0.95 | 0.85 | 0.925 |
| table_numeric | 0.708 | 0.708 | 0.833 |
| temporal | 0.5 | 0.55 | 0.6 |
| **Mean** | **0.773** | **0.681** | **0.814** |

- P1 hybrid ≥ 0.85: **FAIL** (0.814)
- P2 hybrid ≥ both single modes: **PASS**
- P3 Chinese items FTS-only ≤ 0.2: **FAIL** (0.624)
- P4 table_numeric dense < fts: **FAIL** (0.708 vs 0.708)
- P5 pure_chart hybrid ≥ 0.67: **PASS** (0.938)
- P6 reranker: hybrid below threshold → triggers reranker evaluation

## Behavior validation (end-to-end)

- Unsupported-number rate (P7a): 0.0 (threshold ≤0.10 → **PASS**)
- Correctness (P7b): fact hit rate 1.0 (threshold ≥0.85 → **PASS**)
- Hallucination rate (P8): 0.0 — 0/4 unanswerable items answered anyway (threshold = 0 → **PASS**)
- Reproducibility (P9): 3/3 runs contain all invariants → **PASS**
- Robustness (P10): 3/3 paraphrase pairs → **PASS**
- Injection resistance (P11): canary not leaked → **PASS**
- Watermark & contact-info leak (P12): 0 leak(s) (14 answers) → **PASS**

Behavior validation API cost: $7.17

## Behavior validation — extra set `crop` (not preregistered; scored with the same rules)

- Correctness: fact hit rate 0.882; unsupported-number rate 0.176
- Figure-crop accuracy: 14/17 = 0.824 → **PASS** (IoU ≥ 0.5; threshold ≥ 0.80)
- Watermark & contact-info leak: 0 leak(s) over 17 answers
- API cost: $5.84

## Behavior validation — extra set `items:PC5,PC9,PC11` (not preregistered; scored with the same rules)

- Correctness: fact hit rate 0.8; unsupported-number rate 0.0
- Figure-crop accuracy: 1/3 = 0.333 → **FAIL** (IoU ≥ 0.5; threshold ≥ 0.80)
- Watermark & contact-info leak: 0 leak(s) over 3 answers
- API cost: $0.59

---
Details in `eval/results.json`. Rationale for cutting non-applicable dimensions (fairness/calibration/benchmarking) is in DESIGN.md §10.