# Validation Report（确定性打分，对照 DESIGN.md Appendix A 的预注册阈值）

生成时间：2026-08-18 04:23 · 打分全程零 LLM-judge

## 检索消融（recall@10，原始问题句作查询）

| 类目 | dense | fts | hybrid |
|---|---|---|---|
| comparison_timeseries | 0.667 | 0.0 | 0.667 |
| cross_ticker_recall | 1.0 | 1.0 | 1.0 |
| pt_not_on_page1 | 1.0 | 0.5 | 1.0 |
| pure_chart | 0.667 | 0.0 | 0.667 |
| rephrased | 0.889 | 0.111 | 0.889 |
| table_numeric | 0.75 | 0.0 | 0.75 |
| **平均** | **0.804** | **0.196** | **0.804** |

- P1 hybrid ≥ 0.85：**FAIL**（0.804）
- P2 hybrid ≥ 两个单路：**PASS**
- P3 中文题 FTS-only ≤ 0.2：**PASS**（0.167）
- P4 表格数值类 dense < fts：**FAIL**（0.75 vs 0.0）
- P5 纯图表类 hybrid ≥ 0.67：**PASS**（0.667）
- P6 reranker：hybrid 未达标 → 触发 reranker 评估

## 行为验证（端到端）

- P7 Groundedness：徽章 grounded 率 1.0（阈值 ≥0.90 → **PASS**）；事实命中率 1.0（阈值 ≥0.85 → **PASS**）
- P8 Abstention：4/4 → **PASS**
- P9 Reproducibility：3/3 次含全部不变式 → **PASS**
- P10 Robustness：3/3 对 → **PASS**
- P11 Injection：canary 未泄漏 → **PASS**
- P12 Watermark/PII：0 次泄漏（扫描 14 个回答）→ **PASS**

行为验证 API 成本：$7.17

---
明细见 `eval/results.json`。不适用维度（fairness/calibration/benchmarking）的砍除理由见 DESIGN.md §10。