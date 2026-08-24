# Validation Report (deterministic scoring against the preregistered thresholds in DESIGN.md Appendix A)

Generated: 2026-08-18 04:23 · zero LLM-judge scoring throughout

## Retrieval ablation (recall@10, raw question text as query)

| Category | dense | fts | hybrid |
|---|---|---|---|
| comparison_timeseries | 0.667 | 0.0 | 0.667 |
| cross_ticker_recall | 1.0 | 1.0 | 1.0 |
| pt_not_on_page1 | 1.0 | 0.5 | 1.0 |
| pure_chart | 0.667 | 0.0 | 0.667 |
| rephrased | 0.889 | 0.111 | 0.889 |
| table_numeric | 0.75 | 0.0 | 0.75 |
| **Mean** | **0.804** | **0.196** | **0.804** |

- P1 hybrid ≥ 0.85: **FAIL** (0.804)
- P2 hybrid ≥ both single modes: **PASS**
- P3 Chinese items FTS-only ≤ 0.2: **PASS** (0.167)
- P4 table_numeric dense < fts: **FAIL** (0.75 vs 0.0)
- P5 pure_chart hybrid ≥ 0.67: **PASS** (0.667)
- P6 reranker: hybrid below threshold → triggers reranker evaluation

## Behavior validation (end-to-end)

- P7 Groundedness: badge grounded rate 1.0 (threshold ≥0.90 → **PASS**); fact hit rate 1.0 (threshold ≥0.85 → **PASS**)
- P8 Abstention: 4/4 → **PASS**
- P9 Reproducibility: 3/3 runs contain all invariants → **PASS**
- P10 Robustness: 3/3 pairs → **PASS**
- P11 Injection: canary not leaked → **PASS**
- P12 Watermark/PII: 0 leak(s) (scanned 14 answers) → **PASS**

Behavior validation API cost: $7.17

---
Details in `eval/results.json`. Rationale for cutting non-applicable dimensions (fairness/calibration/benchmarking) is in DESIGN.md §10.