# Validation Report (deterministic scoring against the preregistered thresholds in DESIGN.md Appendix A)

Generated: 2026-08-25 21:07 · zero LLM-judge scoring throughout

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
- Watermark & contact-info leak (P12): 0 leak(s) (122 corpus-derived canaries × 14 answers) → **PASS**
- Figure-crop accuracy: 1/1 → **PASS**

Behavior validation API cost: $2.43

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

## Behavior validation — extra set `items:CT3,TN2,TN3,XT2,NF2,RQ5,RQ6,RQ7,RQ8,RQ9,SQ1,SQ2,SQ5,SQ7,SQ8,XT3,XT4,XT5,XT6,XT7,XT8,XT9,XT10,TS1,TS2,TS3,TS4,TS5,TS6,TS7,TS8,TS9,TS10,PC13,PC14,CT4,CT5,CT6,CT7,CT8,CT9,CT10,CT11,CT12,TN5,TN6,TN7,TN8,TN9,TN10,TN11,TN12,TN13,TN14,TN15,TN16,TN17,TN18,TN19,TN20,AB5,AB6,AB7,AB8,AB9,AB10,AB11,AB12,AB13,AB14,AB15,NF3,NF4,NF5,NF6,NF7,NF8,NF9,NF10,MT1,MT2,MT3,MT4,MT5,MI1,MI2,MI3,MI4,MI5,MI6,MI7,MI8,MI9,MI10` (not preregistered; scored with the same rules)

- Correctness: fact hit rate 1.0; unsupported-number rate 0.012
- Hallucination rate: 0.0 (0/11 answered anyway)
- Multi-turn context carry: 5/5 = 1.0 → **PASS**
- Attachment input: 10/10 = 1.0 → **PASS**
- Watermark & contact-info leak: 0 leak(s) over 94 answers
- API cost: $5.91

---
Details in `eval/results.json`. Rationale for cutting non-applicable dimensions (fairness/calibration/benchmarking) is in DESIGN.md §10.