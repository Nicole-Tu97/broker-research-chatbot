# Validation Report (deterministic scoring against the preregistered thresholds in DESIGN.md Appendix A)

Generated: 2026-08-25 18:59 · zero LLM-judge scoring throughout

## Retrieval ablation (recall@10, raw question text as query)

| Category | dense | fts | hybrid |
|---|---|---|---|
| comparison_timeseries | 0.587 | 0.064 | 0.58 |
| deep_page_recovery | 1.0 | 0.091 | 1.0 |
| pure_chart | 0.812 | 0.062 | 0.812 |
| simple_qa | 0.95 | 0.1 | 0.95 |
| table_numeric | 0.708 | 0.125 | 0.708 |
| temporal | 0.5 | 0.1 | 0.4 |
| **Mean** | **0.773** | **0.094** | **0.761** |

- P1 hybrid ≥ 0.85: **FAIL** (0.761)
- P2 hybrid ≥ both single modes: **FAIL**
- P3 Chinese items FTS-only ≤ 0.2: **PASS** (0.195)
- P4 table_numeric dense < fts: **FAIL** (0.708 vs 0.125)
- P5 pure_chart hybrid ≥ 0.67: **PASS** (0.812)
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