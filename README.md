# 燃气管线泄漏检测与隔离协调

模块化纯 Python 3.9.6+ 标准库项目，默认端口 `8333`。

- `app.py`：参数、依赖和服务生命周期。
- `src/domain.py`：管段、传感值和来源记录校验。
- `src/rules.py`：泄漏评分、三档分级（一般/较大/重大）、分级响应时限与自动升级、阀门顺序、修复、试压、恢复状态机。
- `src/repository.py`：SQLite、重复保护、乐观版本、审计链和事件关联关系。
- `src/service.py`：角色权限、超时督办、影响面登记和复发关联事件编排。
- `src/http_api.py`：JSON 接口与首页。
- `src/audit.py`：可校验的审计事件。

## 分级响应

- 按评分（压降 + 浓度 + 异味）分三档：`<45` 一般、`45–74` 较大、`>=75` 重大。
- 处置时限（当前处置步距上报/升级/上一步完成）：一般 180 分钟、较大 60 分钟、**重大 30 分钟（上报后半小时内完成核验，并督办后续每一步）**。
- 超时未处置自动扫描（`POST /api/escalations/sweep`，读取列表/状态、提交来源、执行操作前也会触发）：升一档并写审计 `auto_escalated`，记录发生时间、档位变化、**卡在哪一步**（如「现场核验」「登记影响范围并隔离」），并按新档位重置时限；已在重大档则保持重大并再次登记超时。
- 核验通过后须用 `register_impact` 登记受影响管段（`affected_segments`）和用户（`affected_users`）；隔离请求必须携带已登记的影响面，否则以 `impact_not_registered`（422）退回重填。
- 已恢复供气的事件再收到来源（传感/巡检）记录时，不并入旧事件，自动另建复发关联事件：新事件 `payload.related_to` 指向旧事件并说明关系，旧事件以 `restored_followup_link` 留痕。
- 页面展示三档标识、评分、剩余时限（超时高亮+卡点）、升级记录、影响范围与关联事件。

```bash
python3 app.py --init --db ./data.db
python3 app.py --db ./data.db --port 8333
python3 -m unittest discover -s tests -v
```

接口包括 `GET /health`、`GET /api/state`、`GET /api/items`、`POST /api/items`、`POST /api/items/<id>/sources`、`POST /api/items/<id>/actions`（action：`verify`、`register_impact`、`isolate`、`repair`、`pressure_test`、`restore`、`cancel`）、`POST /api/escalations/sweep` 和审计查询。测试覆盖完整抢修流程、三档评分、30 分钟核验时限、超时自动升级与卡点登记、未登记影响范围的隔离退回、恢复后复发关联事件、重复事件、阀门顺序、试压阈值、现场危险条件、权限和版本冲突。模型不替代 SCADA、管网水力计算或正式应急预案。
