# 深度登记 水产拍卖结算

规模：中型。使用者：水产批发市场结算员。核心规则：先毛皮配对，再扣杂计价，按更正链选择唯一生效版，只支付差额；承运费按同车票面计价重分摊，合计不符不得封存。

## 功能模块

| 模块 | 使用者动作 | 入口 | 核心文件 | 测试 |
|---|---|---|---|---|
| M1 称重采信 | 查看并执行称重采信 | `POST /api/intake` | `app/modules/intake/rules.py` | `tests/test_intake.py` |
| M2 货品换算 | 查看并执行货品换算 | `POST /api/conversion` | `app/modules/conversion/rules.py` | `tests/test_conversion.py` |
| M3 毛皮配对 | 查看并执行毛皮配对 | `POST /api/pairing` | `app/modules/pairing/rules.py` | `tests/test_pairing.py` |
| M4 扣杂竞价 | 查看并执行扣杂竞价 | `POST /api/auction` | `app/modules/auction/rules.py` | `tests/test_auction.py` |
| M5 更正裁决 | 查看并执行更正裁决 | `POST /api/correction` | `app/modules/correction/rules.py` | `tests/test_correction.py` |
| M6 应付对账 | 查看并执行应付对账 | `POST /api/reconcile` | `app/modules/reconcile/rules.py` | `tests/test_reconcile.py` |
| M7 承运结算 | 查看同车逐票计价重、已结/待定、封存与后补更正补退 | `POST /api/freight`、`POST /api/runs/{run_id}/publish` | `app/modules/freight/rules.py`、`app/store.py` | `tests/test_freight.py`、`tests/test_freight_publish.py` |
| M8 结算归档 | 查看并执行结算归档 | `POST /api/archive` | `app/modules/archive/rules.py` | `tests/test_archive.py` |

## 承运费裁决

一车次一张承运单，票号清单与承运费总额是唯一入账依据（`sample_data/context.json` 的 `trips`，也可由 `POST /api/runs` 的 `overrides.trips` 覆盖）。

- **每票计价重来源**：已结票取生效版 `billable_kg`；未结票先取暂定版（最新版本无扣杂计价重），再退到车次清单的 `declared_weight`。两者皆无、或票号与清单对不上的票标为“不入账”，计入 `freight.unassigned`，车费不会挤给其余票。
- **分摊**：只有全部票都有重量依据时才按重量分摊；以分为单位用最大余数法，逐票承运费合计恰等于承运单金额。承运商直接给出逐票分项（`ticket_freights`）时逐票核对，清单外票号、缺票或多出金额都阻断封存。
- **封存**：`balanced`（合计相符）且无阻断项才可封存，否则 `POST .../publish` 返回 422 `FREIGHT_NOT_SEALABLE`。封存写入按车次版本化的 `publications` 表，事务内完成。
- **重跑**：同一车次同一结果 `result_digest` 相同，重复封存返回原版本（`idempotent`），不新增承运费。
- **后补更正**：已发布后遇到复秤/扣杂更正，页面“已发布 vs 待发布”列出每票原计价重、新计价重、补退承运费、货款差额与合计补退（各票承运费补退之和恒为 0，承运单总额不变）。再次封存生成新版本并写入 `adjustments`，旧版本及其 `payments` 原付款证据保留。
- **中断恢复**：写入在单个 SQLite 事务内，异常回滚；旧账、旧版本和原付款证据保持原样，批次仍可重新读取和再次封存。
- **归档**：归档封存校验同时要求承运费合计相符且无未入账票；`GET /api/export/{run_id}` 的 zip 内含 `freight.json` 与 `publications.json`。

## 陷阱登记

| TRAP | 现象 | 朴素实现 | 本项目处理 | 处理位置 | 测试 |
|---|---|---|---|---|---|
| TRAP-01 | 旧编码中文票据 | 静默错算或丢失证据 | 显式归一与原因裁决 | `app/modules/intake/rules.py` | `tests/test_intake.py::test_legacy_encoding` |
| TRAP-02 | BOM表头 | 静默错算或丢失证据 | 显式归一与原因裁决 | `app/modules/intake/rules.py` | `tests/test_intake.py::test_bom_real` |
| TRAP-04 | 分段表头 | 静默错算或丢失证据 | 显式归一与原因裁决 | `app/modules/intake/rules.py` | `tests/test_intake.py::test_repeated_header_real` |
| TRAP-05 | 缺列记录 | 静默错算或丢失证据 | 显式归一与原因裁决 | `app/modules/intake/rules.py` | `tests/test_intake.py::test_bad_columns_real` |
| TRAP-07 | 空值哨兵 | 静默错算或丢失证据 | 显式归一与原因裁决 | `app/modules/conversion/rules.py` | `tests/test_conversion.py::test_sentinel_real` |
| TRAP-08 | 前导零票号 | 静默错算或丢失证据 | 显式归一与原因裁决 | `app/modules/intake/rules.py` | `tests/test_intake.py::test_leading_zero_real` |
| TRAP-14 | 重复编号不同原值 | 静默错算或丢失证据 | 显式归一与原因裁决 | `app/modules/intake/rules.py` | `tests/test_intake.py::test_duplicate_conflict_real` |
| TRAP-18 | 三类空态 | 静默错算或丢失证据 | 显式归一与原因裁决 | `app/modules/intake/rules.py` | `tests/test_intake.py::test_empty_states` |
| TRAP-15 | 吨与千克混合 | 静默错算或丢失证据 | 显式归一与原因裁决 | `app/modules/conversion/rules.py` | `tests/test_conversion.py::test_tonnes_counterexample` |
| TRAP-06 | 全角千分位 | 静默错算或丢失证据 | 显式归一与原因裁决 | `app/modules/conversion/rules.py` | `tests/test_conversion.py::test_fullwidth_thousands` |
| TRAP-10 | 金额半分舍入 | 静默错算或丢失证据 | 显式归一与原因裁决 | `app/modules/auction/rules.py` | `tests/test_auction.py::test_half_cent_rounding` |
| TRAP-26 | 陈旧更正分支 | 静默错算或丢失证据 | 显式归一与原因裁决 | `app/modules/correction/rules.py` | `tests/test_correction.py::test_stale_branch` |
| TRAP-31 | 同车多票、部分票未闭合 | 只结已闭合票，车费落到其余票；或整车等待 | 逐票分摊、待定票按暂定/申报重量预留，已结票只承担自己份额 | `app/modules/freight/rules.py` | `tests/test_freight.py::test_closed_ticket_does_not_carry_unclosed_share` |
| TRAP-32 | 车次票号对不上 | 仍给该票摊车费或入账 | 标为不入账并阻断封存 | `app/modules/freight/rules.py` | `tests/test_freight.py::test_unknown_ticket_not_booked` |
| TRAP-33 | 分摊尾差 | 合计与承运单差几分钱 | 分单位最大余数法，合计恰等于承运单 | `app/modules/freight/rules.py` | `tests/test_freight.py::test_allocate_remainder_uses_exact_cents` |
| TRAP-34 | 后补复秤/扣杂更正 | 覆盖旧账，原付款证据丢失 | 新版本 + 独立调整记录，旧版本与 payments 保留；重跑幂等 | `app/store.py` | `tests/test_freight_publish.py::test_late_correction_creates_independent_adjustment` |
| TRAP-35 | 发布中断 | 半写状态、丢记录 | 单事务提交，异常回滚到旧账旧版 | `app/store.py` | `tests/test_freight_publish.py::test_interrupted_publish_rolls_back` |

## 对账规则

输入=采纳原始行+隔离行+空行+同值重复行+重复表头。计价版本总额与生效票总额分别展示，避免把更正前后重复求和。最终应付=已付+待付，负待付为退款。承运费另账：逐票分摊合计必须等于承运单，未入账票单列，不并入应付。

## 已知不覆盖

无称重设备实时接入，采用现场导出文件。无支付执行，只生成可复核结算证据。
