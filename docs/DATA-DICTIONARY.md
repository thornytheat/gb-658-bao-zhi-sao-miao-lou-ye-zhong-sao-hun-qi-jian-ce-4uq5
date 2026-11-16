# 数据字典

- run_id：规则版本与输入 SHA-256 前 12 位组成的稳定批次标识。
- line_no：从 1 开始的物理来源行号，不因隔离或排序改变。
- business_key：本项目声明的业务唯一键，始终按字符串存储。
- reason_code：隔离原因的稳定英文编码。
- accepted：进入业务计算的记录数。
- quarantined：保留证据但不参与业务计算的记录数。
- 数值：金额、税额、用量和权重使用 Decimal；对外序列化为字符串。
- 时间：存储为带偏移的 ISO 8601，分桶使用半开区间。
- 空值：缺失键、空串和业务哨兵分别记录。
- 幂等：完整输入字节与规则版本相同则复用 run_id，不新增结果。
- batch_id：重扫清单的归档批次标识，由输入 SHA-256 前 12 位组成；同一字节复用批次。
- scan_id：业务键 + 文件名 + 摘要的稳定标识，与导入批次无关，保证同一原件不被重复登记。
- revision：采纳组版本，基线导入为 1，每次发布加 1；裁决须携带 expected_revision。
- publish_state：决定发布状态，只允许 `PENDING → PUBLISHED` 或 `PENDING → SUPERSEDED`。
- request_id：裁决幂等键；发布中断后用同一 request_id 重试只重放，不新增决定。
- header_verified：页眉证据，1 一致、0 缺失、-1 与清单期号不符；非 1 只能复核采信。
- 归档快照：批次导入时的原扫描、当时缺页与混期证据，存于 `rescan_batches.snapshot`，裁决不改写。

