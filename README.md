# 燃气管线泄漏检测与隔离协调

模块化纯 Python 3.9.6+ 标准库项目，默认端口 `8333`。

- `app.py`：参数、依赖和服务生命周期。
- `src/domain.py`：管段、传感值和来源记录校验。
- `src/rules.py`：泄漏评分、阀门顺序、修复、试压、恢复状态机。
- `src/repository.py`：SQLite、重复保护、乐观版本和审计链。
- `src/service.py`：角色权限和业务编排。
- `src/http_api.py`：JSON 接口与首页。
- `src/audit.py`：可校验的审计事件。

```bash
python3 app.py --init --db ./data.db
python3 app.py --db ./data.db --port 8333
python3 -m unittest discover -s tests -v
```

接口包括 `GET /health`、`GET /api/state`、`POST /api/items`、`POST /api/items/<id>/sources`、`POST /api/items/<id>/actions` 和审计查询。测试覆盖完整抢修流程、重复事件、阀门顺序、试压阈值、现场危险条件、权限和版本冲突。模型不替代 SCADA、管网水力计算或正式应急预案。

## 分级响应

- **建档即定级，不允许压级**：创建事件时按 `压力降×2 + min(浓度,500)×0.1 + 异味报告×5`（满分 100）评分，服务端据评分分三档并起算核验时限：
  - 一般（`general`，<45 分）：120 分钟
  - 较大（`major`，45–74 分）：60 分钟
  - 重大（`critical`，≥75 分）：**30 分钟内完成现场核验**
- **超时自动升档**：在上报→核验环节超过当前档时限仍未处置的，读取/操作事件时惰性自动升一档（一般→较大→重大），新档按新时限重新起算；已到重大档仍超时则记持续告警。每次升级都写入 `response_sla.history` 与审计链（`sla_escalation`），记录 `blocked_step=待现场核验`、`overdue_seconds`、原因和新到期时间。系统升级随下一次操作提交，不使客户端的 `expected_version` 失效。
- **核验只升不降**：核验可提交现场修正读数（`field_pressure_drop_kpa` 等），分数对应档位更高则升级并记录（`verification_upgrade`），更低不得降级。
- **影响范围登记**：`verify` 通过时必须同时提交 `affected_segments`（管段）和 `affected_customers`（下游用户，支持字符串或 `{customer_id,name}`），作为隔离依据。
- **隔离强制核对影响面**：`isolate` 必须携带与核验登记一致的影响面；未登记返回 `impact_not_registered`，未携带返回 `impact_required`，不一致（管段或下游用户集合有差异）返回 `impact_mismatch`，均提示“退回重填”。
- **恢复后复发另立关联事件**：已 `restored` 的事件再收到来源（传感/巡检）记录时，不并入旧事件，而是创建一条新事件，带 `recurrence_of=<旧事件id>` 与中文关系说明，独立定级、独立计时；旧事件保持 `restored`，写入 `related_recurrences` 指针与 `recurrence_linked` 审计。
- **页面展示**：首页调度台展示每个事件的分档徽标、剩余/超时时限与当前卡点、升级记录（时间、档位变化、卡在哪一步、原因）、受影响管段与下游用户，以及关联复发事件关系，30 秒自动刷新、倒计时每秒更新。
