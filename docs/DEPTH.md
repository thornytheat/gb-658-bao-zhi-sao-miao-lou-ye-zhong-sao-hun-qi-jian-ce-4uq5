# 深度登记 扫描期版质检台

- 规模：中型
- 核心规则：期号、版次、页码构成业务键，图像相似只生成候选，页眉证据决定混期。
- 使用者与工作流：质检员导入扫描清单，核对期版与页序，裁决重扫和混期，归档证据。

## 功能模块

| 模块 | 使用者动作 | 入口 | 核心文件 | 测试 |
|---|---|---|---|---|
| M1 清单采信 | 导入扫描清单并定位坏行 | POST /api/scans/import | `app/modules/m1_intake/parser.py` `app/modules/m1_intake/integrity.py` | `tests/test_m1_intake.py` |
| M2 期版契约 | 规范期号、版次和页码 | POST /api/issues/normalize | `app/modules/m2_issue/rules.py` `app/modules/m2_issue/catalog.py` | `tests/test_m2_issue.py` |
| M3 页码序列 | 查缺页、重页与越界页 | POST /api/pages/check | `app/modules/m3_pages/sequence.py` `app/modules/m3_pages/reconcile.py` | `tests/test_m3_pages.py` |
| M4 重扫采信 | 导入扫描清单、按证据裁决并发布采纳页 | POST /api/rescans/import；POST /api/rescans/decide；GET /api/rescans/state；GET /api/rescans/batches/{batch_id} | `app/modules/m4_rescans/intake.py` `app/modules/m4_rescans/adjudication.py` `app/modules/m4_rescans/repository.py` | `tests/test_m4_rescans.py` `tests/test_m4_rescan_workflow.py` |
| M5 混期归因 | 用页眉和页码证据裁决混期 | POST /api/mixed/evaluate | `app/modules/m5_mixed/evidence.py` `app/modules/m5_mixed/conflicts.py` | `tests/test_m5_mixed.py` |
| M6 批次对账 | 检查四账并导出证据 | POST /api/reconcile | `app/modules/m6_batches/compare.py` `app/modules/m6_batches/exporter.py` | `tests/test_m6_batches.py` |

## 陷阱登记

| TRAP | 现象 | 朴素实现会怎样 | 本项目处理 | 处理位置 | 测试 |
|---|---|---|---|---|---|
| TRAP-02 | 首列带 BOM | 期号字段取不到 | 读取表头时移除 BOM | `app/modules/m1_intake/parser.py` | `tests/test_m1_intake.py::test_strip_bom_header` |
| TRAP-05 | 行列数不齐 | 错位行进入页序 | 隔离并保留物理行号 | `app/modules/m1_intake/parser.py` | `tests/test_m1_intake.py::test_column_count_goes_quarantine` |
| TRAP-07 | 页眉为空 | 空串被当作一致 | 返回未知并转人工复核 | `app/modules/m5_mixed/evidence.py` | `tests/test_m5_mixed.py::test_missing_header_is_unknown` |
| TRAP-08 | 前导零页码 | 转整数后无法复核原编号 | 规范为三位字符串 | `app/modules/m2_issue/rules.py` | `tests/test_m2_issue.py::test_page_zero_pads_without_numeric_roundtrip` |
| TRAP-09 | 大摘要或相似哈希 | 数字往返或只凭相似度误判 | 摘要保形，相似仅生成候选 | `app/modules/m5_mixed/conflicts.py` | `tests/test_m5_mixed.py::test_hash_candidate_without_header_needs_review` |
| TRAP-14 | 同版多扫描值冲突 | 后写覆盖清晰版本 | 版本、清晰度、时间稳定择优 | `app/modules/m3_pages/sequence.py` | `tests/test_m3_pages.py::test_duplicate_page_detected` |
| TRAP-16 | 清单尾行截断 | 半行被当成扫描件 | 完整尾行检查并隔离 | `app/modules/m1_intake/integrity.py` | `tests/test_m1_intake.py::test_truncated_tail_detected` |
| TRAP-19 | 候选顺序改变 | 先到者决定保留项 | 显式排序键保持顺序无关 | `app/modules/m3_pages/sequence.py` | `tests/test_m3_pages.py::test_selection_order_independent` |
| TRAP-22 | 同内容多别名 | 合并顺序改变代表扫描 | 清晰度后以时间和文件名择一 | `app/modules/m4_rescans/decision.py` | `tests/test_m4_rescans.py::test_selection_is_deterministic_on_tie` |
| TRAP-26 | 陈旧重扫晚提交 | 旧版本覆盖新版 | 版本优先于输入顺序 | `app/modules/m3_pages/sequence.py` | `tests/test_m3_pages.py::test_higher_version_wins_not_later_input` |
| TRAP-27 | 补扫清单到达后刷新采纳页 | 覆盖归档扫描或冻结导致补扫无效 | 归档批次按导入字节冻结，裁决只切换当前采纳页 | `app/modules/m4_rescans/intake.py` | `tests/test_m4_rescan_workflow.py::test_archive_batch_keeps_original_evidence_after_rescan` |
| TRAP-28 | 页眉缺失/不符的重扫候选 | 相似度更高即自动采信 | 留作候选，只能复核采信并写明理由 | `app/modules/m4_rescans/adjudication.py` | `tests/test_m4_rescan_workflow.py::test_missing_header_candidate_waits_for_review` |
| TRAP-29 | 两人同时裁决同一页 | 后写覆盖前写 | 版本比对，过期决定返回 STALE_VERSION | `app/modules/m4_rescans/adjudication.py` | `tests/test_m4_rescan_workflow.py::test_two_concurrent_decisions_only_one_wins` |
| TRAP-30 | 发布中断后重试 | 重复点击新增决定或页面停在半态 | 决定先留档，发布独立事务；重试同一 request_id 只重放 | `app/modules/m4_rescans/adjudication.py` | `tests/test_m4_rescan_workflow.py::test_publish_interruption_keeps_original_page_and_retry_adds_no_decision` |
| TRAP-31 | 采信后只看当前页 | 缺页/对账与采纳页不同步 | 发布事务一起切换采纳页、页序缺口与扫描件四账 | `app/modules/m4_rescans/intake.py` | `tests/test_m4_rescan_workflow.py::test_adoption_switches_gaps_and_accounts_together` |

## 重扫采信规则

- 业务键为期号 + 版次 + 页码；同一业务键的同一文件与摘要只保留一份原件。
- 页眉一致才可普通采信；页眉缺失或不符只能复核采信（REVIEW_APPROVE）并写明理由。
- 采信理由码：`BASELINE` `CLEARER_SCAN` `EXACT_COPY` `REVIEW_APPROVED` `MANUAL_OVERRIDE` `REJECTED`。
- 决定只追加；发布只允许 `PENDING → PUBLISHED/SUPERSEDED`，已发布的行不再改写。
- 归档批次快照（扫描原件、当时缺页、混期证据）在导入事务内冻结，裁决不改写它。

## 对账规则

- 应有页数 = 唯一采纳页 + 缺页数。
- 扫描件数 = 唯一采纳 + 被替代重扫 + 混期 + 隔离。
- 同一输入与规则版本重跑，运行标识和结果行数不变。

## 已知不覆盖

- 不读取图像二进制，感知哈希与 OCR 页眉由上游扫描软件提供。

