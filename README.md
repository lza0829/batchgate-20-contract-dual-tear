# 02-batchgate（闸序）

Batchgate — 离线作业依赖编排：作业定义 → 依赖就绪检查 → 执行/失败重试 → 产出水位与对账。

与 Feedstitch（多源 Feed 归并）主链路不同，禁止做成 Feed 解析/冲突/回看换皮。

## 启动

```bash
docker compose up --build
```

| 入口 | 地址 |
| --- | --- |
| 前端 | http://localhost:5200 |
| API | http://localhost:10200 |

## 主链

配置作业与依赖 → 触发一次编排 Run → 按依赖顺序执行夹具作业 → 写入尝试记录与产出水位 → 报告对账

## 产出契约闸（output-contract-gate）

下游可对指定上游配置产出契约，编排时在上游 success 之后、下游执行之前核验：

- **min_rows**：上游产出摘要 `artifact_rows` 不得低于该值（拦 0 行空成功）。
- **required_field**：上游尝试详情（产出摘要）里该字段必须存在且非 null（如 `watermark_ts`）。

契约不过 → 下游不执行（夹具不跑），尝试记为 `skipped_contract`（detail 含 `violations`、
`executed=false`、`watermark_advanced=false` 与 `reason`，写明哪条不过），
且该下游不抬自己的水位、也不会被更下游当成可信上游（更下游记 `skipped_dep`）；
契约通过则尝试 detail 带 `contract_verdict=passed` 与核验过的上游列表。
报告计数直接由尝试记录汇总得出，success / failed / skipped_dep / skipped_contract 与尝试列表
一一对得上，并由报告里的 `reconciliation` 与 `contract_checks` 逐项互证（尝试计数、契约跳过数、
跳过合计、拦截作业未被信任、拦截作业水位未动、已执行水位与尝试一致）。

契约核验规则（任一不满足即不通过，绝不当作空成功放行）：

- 上游必须在本轮有可核验的产出摘要；缺失摘要记 `upstream_summary` 违规。
- 摘要必须有整数 `artifact_rows`；缺失记 `min_rows` 违规，0 行在 `min_rows>=1` 时同样违规。
- `required_field` 存在但为 null 同样违规。

接口：

- `GET /api/contracts` 查看全部契约
- `PUT /api/jobs/{job_id}/contracts/{upstream_job_id}` 配置/覆盖契约（上游须在该下游 depends_on 内）
- `DELETE /api/jobs/{job_id}/contracts/{upstream_job_id}` 删除契约

前端「产出契约」页可配置与查看；「编排台」一次 Run 即可看到契约通过与契约拦住两条路径。

## 种子说明

- **ingest_orders**：无依赖，写出订单水位。
- **dim_customer**：无依赖，写出客户维表水位。
- **ingest_refunds**：无依赖，夹具为 0 行空成功（success 但 `artifact_rows=0`）。
- **fact_order_daily**：依赖 ingest_orders / dim_customer，各配契约 `min_rows=1 + required_field=watermark_ts` → 契约通过路径。
- **fact_refund_daily**：依赖 ingest_refunds，契约 `min_rows=1` 被空成功触发 → 契约拦截路径（`skipped_contract`，不抬水位）。

## 技术栈

Python 3.12 + FastAPI + SQLite；Vue 3 + Vite + Nginx。

## 0-1 切口（未实现）

`dep_gate` / `retry_budget` / `watermark_reconcile` / `sla_breach`
