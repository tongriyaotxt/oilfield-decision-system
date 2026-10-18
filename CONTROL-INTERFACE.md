# 自主控制接口层（Integration Contract）

> 决策做得再好，落不了地都是零。这一层只干一件事：**把决策变成可下发、可回执、可回滚、可追责的命令**。
> 实现见 `scripts/control_api.py`，契约词汇见 `ontology/control-core.ttl`。

---

## 1. 七个接口（Python Protocol ↔ 生产协议）

| 接口 | 方法 | 生产映射 | 本仓库实现 |
|---|---|---|---|
| **数据源** | `well_features(start,end)` / `water_cut(well,...)` | 数仓 SQL / API / 湖（只换适配器） | `SQLiteSource` |
| **执行器** | `execute(cmd, attempt)` | OPC UA method call / MQTT publish + 回执 topic | `SimActuator`（确定性故障注入） |
| **命令总线** | `dispatch(cmd)` / `confirm(cmd, operator)` | 内部服务 / gRPC | `CommandBus` |
| **授权** | `level(action)` | 权限中心 / 审批流 | `Authorization` |
| **灰度** | `inside(well)` | 配置中心 | `Canary`（按井号哈希） |
| **指标注册** | `register(name, spec)` / `require(name)` | 语义层管理后台 | `MetricsRegistry` |
| **审计** | `log(...)` / `flush()` | 审计库 + 消息队列 | `AuditLog`（JSONL）+ RDF 回执图 |

**MQTT 主题约定**（生产）：

```
下行：site/{站}/cmd/{设备}          payload = 命令 JSON（含 request_id / deadline / expected_state）
上行：site/{站}/cmd_ack/{设备}      payload = 回执（accepted / executing / done / failed + 实测值）
遥测：site/{站}/well/{井}/telemetry/{metric}
```

命令 JSON 最小字段：

```json
{"request_id":"甲区-01-20261001-ShutIn","well":"甲区-01","action":"ShutIn",
 "on_h":3.0,"off_h":9.0,"duty":0.25,"deadline_s":30,
 "expected_state":"duty=0.25","auth":"A","issued_by":"agent"}
```

---

## 2. 命令状态机

```
                 ┌──────────── A 类 ────────────┐
dispatch ──► Accepted ──► Executing ──► Done（并校验 expected_state）
                 │              │
                 │              └─► Failed ──►（重试 ≤2）──► Done
                 │                                    └──► RolledBack（恢复原制度）
                 ├─► Rejected（参数越界/阀位不可达，不可重试）
                 └─► AwaitingHuman（B/C 类，等审批）

confirm(operator) ──► 解除幂等缓存 ──► 再次 dispatch（auth 升为 A）
```

三条硬规则：
1. **幂等**：同一 `request_id` 重复下发只回放原回执，不重复执行。
2. **回执校验**：不只看"指令成功"，还要比对 `expected_state`——防"指令成功、设备没动"。
3. **失败即回滚**：重试耗尽后自动恢复原制度，不允许留在半执行状态。

---

## 3. 授权分级（单向安全）

| 级别 | 动作 | 谁决定 | 最坏结果 |
|---|---|---|---|
| **A** | 缩短时率、关井、维持、升级人工 | 系统自动 | 停井（安全方向） |
| **B** | 提高时率、恢复生产 | 系统建议 + 人工确认（带工号） | 加剧抽汲与磨损 |
| **C** | 关停、措施、注采比 | 仅建议 + 经济评价 | 经营偏差 |

> A 类之所以敢自动，是因为**动作集合只有刹车**。B/C 类是踩油门，必须有人签字。

---

## 4. 灰度与回滚

| 项 | 做法 |
|---|---|
| 灰度抽样 | 按 `md5(well|salt)` 稳定抽样，同一口井不会忽进忽出（本仓库 40%） |
| 灰度周期 | 2 周：一致率≥85% → 扩到 80% → 全量 |
| 回滚触发 | 回执失败 / `expected_state` 未达 / 24h 回采偏差 >15% / 人工一键 |
| 回滚动作 | 恢复原制度命令（同一总线、同样带 request_id 与审计） |

---

## 5. 审计（自主控制的追责基础）

`build/control-audit.jsonl` 每行一个事件：

```json
{"type":"command.result","request_id":"乙区-04-20261001-ShutIn","well":"乙区-04",
 "detail":"ShutIn → Done (134ms)","at":"2026-10-01T00:00:00+00:00"}
```

事件类型：`command.idempotent_replay`、`command.retry`、`command.rollback`、
`command.awaiting_human`、`command.suggestion_only`、`command.approved`、`command.result`。

同时输出 RDF 回执图 `build/control-report.ttl`：`Command`（井、动作、授权级、deadline、expected_state）
+ `Receipt`（状态、时延、重试次数、错误信息）。

**审计要能回答四个问题**：谁、什么时候、基于什么证据、下了什么命令，回执是什么。

---

## 6. 指标注册：未建模就拒绝

```python
registry.require("oil_rate_weekly")   # → UnknownMetric
# 指标「oil_rate_weekly」未建模。请先注册口径（单位/公式/可加性/时间口径），
# 本系统不会用猜测值回答。
```

注册必填五项：`unit / formula / additive / grain / time_sem`。缺一项直接拒绝。
这条接口是"口径治理"能长期活下来的前提——**没有注册中心，三个月后又会长出一堆私货指标。**

---

## 7. 经济评价：把硬编码阈值换成参数

```python
Economics(price_usd=60, fx=7.2, bbl_per_t=7.33, tax_rate=0.12,
          var_cost_per_t=200, fixed_cost_per_well_year=80000)
# 边际贡献 2587 元/t → 经济极限 0.085 t/d
# 敏感性：$40→0.132 ｜ $60→0.085 ｜ $80→0.062 t/d
```

原来规则里写死 `0.1 t/d`，现在随油价、成本、汇率浮动。
**这就是"经营决策"与"技术规则"的分界线**：阈值不该是常量，它应该是输入。

---

## 8. 措施建议：不承诺产量，但把方案排序并报价

| 井况 | 建议 | 预期增油 | 投入 |
|---|---|---|---|
| 产油≈0 且时率≈0 | 长停/复产评估（测压 + 液面恢复） | 0~0.3 t/d | 10 万 |
| 泵效<20% 且时率<30% | 优化间开 + 评估注水/补能 | 0~0.1 t/d | 5 万 |
| 泵效<20% 且时率>90% | 降冲次/换小泵 | 0（降耗 30~50%） | 15 万 |
| 泵效 20~70% | 换泵或调参 | 0.05~0.2 t/d | 20 万 |
| 泵效>70% 且产油低 | 措施增产（酸化/补孔/压裂） | 0.2~0.8 t/d | 30~80 万 |

**注意**：这些是**经验区间**，必须用邻井类比与本井历史校准后使用。
系统能做的是"排序 + 报价 + 给区间"，不是"保证增产"。

---

## 9. 实时边界（必须写死，否则会出安全事故）

| 时间尺度 | 谁负责 | 本层是否参与 |
|---|---|---|
| 毫秒~2s | SIS 安全联锁（独立回路） | **不参与** |
| 秒~分钟 | 边缘控制器（本地自愈、断网自治） | 只下发策略与命令，不介入执行 |
| 分钟~小时 | 站控/区域中心（远程操作） | 提供建议与审批流 |
| 小时~日 | **本层（决策 + 优化 + 灰度）** | ✅ 主体 |
| 周~月 | 经营决策（关停/措施/注采比） | 只给 C 类建议 |

`deadline_s` 与 `expected_state` 是跨界契约：本层给目标和期限，秒级动作由边缘兜住。

---

## 10. 原先的"不能"，现在怎么处理

| 原先不能 | 现在 | 仍存在的真实边界 |
|---|---|---|
| 替代执行机构 | `Actuator` 接口 + 回执校验 + 失败回滚 | 阀门、变频器、RTU 仍需采购安装 |
| 做实时控制 | 命令带 deadline/expected_state，秒级交给边缘 | 本层不进入 2s 以内的联锁环 |
| 处理未建模指标 | `MetricsRegistry.require` 直接拒绝 | 新指标仍需人建模（这是特性不是缺陷） |
| 替人做经营决策 | 经济极限参数化 + C 类只给建议与报价 | 关停/措施签字权永远在人 |
| 提高产能上限 | `Recommender` 给方案、区间、投入 | **间开不创造供液，产量天花板仍是 `qi`** |
| 复现真实数据 | `DataSource` 适配器化 | 换真实台账后必须重跑标定 |
| 保证不跑飞 | 灰度 + 回滚 + 审计三件套 | 仍需人值守前两周 |

---

## 11. 上线 checklist（照这个打勾就能接现场）

- [ ] 执行器接到 OPC UA / MQTT，`expected_state` 有真实可观测量（阀位/频率/启停反馈）
- [ ] 权限中心对接：A/B/C 与审批流（双人签）
- [ ] 灰度比例、观察期、回滚预案写入变更单
- [ ] 审计落库并对接安全审计（保留 ≥1 年）
- [ ] 指标注册中心上线，owner 签字流程生效
- [ ] 经济参数（油价/汇率/成本/税率）接入财务口径，按月更新
- [ ] 前两周双人值守，每日复核回执与偏差
- [ ] 回采偏差 >15% 自动降级已联调通过

---

## 12. 一句话

> **自主控制 = 命令化 + 授权分级 + 灰度回滚 + 审计。**
> 四件缺一件，都不叫自主控制，叫"把风险自动化"。
