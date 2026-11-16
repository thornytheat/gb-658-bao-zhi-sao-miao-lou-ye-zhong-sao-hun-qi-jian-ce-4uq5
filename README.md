# 扫描期版质检台

面向真实数据复核的中型数据工程工具，包含六个可独立调用的业务模块。规则、失败原因与证据字段见 docs/DATA-DICTIONARY.md，陷阱和测试映射见 docs/DEPTH.md。

## 启动

docker compose up --build

浏览器访问 http://localhost:18217。测试：docker compose exec -T app pytest -q

## 模块

- M1 清单采信：页面 /intake，接口 /api/scans/import
- M2 期版契约：页面 /issues，接口 /api/issues/normalize
- M3 页码序列：页面 /pages，接口 /api/pages/check
- M4 重扫采信：页面 /rescans，接口 /api/rescans/import、/api/rescans/decide、/api/rescans/state、/api/rescans/batches/{batch_id}
- M5 混期归因：页面 /mixed，接口 /api/mixed/evaluate
- M6 批次对账：页面 /batches，接口 /api/reconcile

## 重扫采信工作流

1. 导入真实扫描清单（CSV，列见 `sample_data/`）。批次按输入字节冻结：同一输入复用批次，不新增结果。
2. 当期号、版次、页码与页眉一致时按清晰度择清晰扫描，自动生成基线采纳；
   页眉缺失或与清单期号不符的行只留作候选，必须复核采信并写明理由。
3. 裁决写入历次决定（只追加），随后单独发布。发布在一个事务里切换当前采纳页、
   页序缺口与扫描件对账。
4. 两人同时裁决时，后提交者因版本过期被拒绝（`STALE_VERSION`），页面提示刷新。
5. 发布中断时决定停留在 `PENDING`，页面仍显示原采纳页；重试同一请求只重放发布，
   不新增决定；服务启动时也会自动重放积压的发布。
6. 归档批次始终展示导入时的原扫描、当时缺页与混期证据；页面可把归档视图与当前采纳页并排对照。

## 运行规则

同一输入内容生成相同运行标识，重复执行复用已有批次。隔离记录包含原因码、原始行号与原值；业务导出只保留一份证据清单。

