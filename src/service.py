from datetime import datetime, timezone

from . import domain, rules
from .domain import DomainError

SYSTEM_ACTOR = "sla-monitor"
SYSTEM_ROLE = "system"


class Service:
    def __init__(self, repository, clock=None):
        self.repository = repository
        # clock 注入便于测试；默认使用 UTC 当前时间
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def now(self):
        return self._clock()

    # -- SLA -------------------------------------------------------------

    def _run_escalation(self, item):
        """对单个事件做一次惰性超时升级检查，必要时落库。返回（可能已更新的）事件。"""
        new_payload, audit_payload = rules.check_escalation(
            item["payload"], item["status"], self.now()
        )
        if new_payload is None:
            return item
        try:
            updated = self.repository.apply_escalation(
                item["id"],
                item["payload"]["response_sla"]["due_at"],
                new_payload,
                audit_payload,
                SYSTEM_ACTOR,
                SYSTEM_ROLE,
            )
            return updated
        except Exception:
            # 并发下其它请求已推进 SLA，则以库内最新记录为准
            return self.repository.get_item(item["id"])

    def _prepare_item(self, item):
        return self._run_escalation(item)

    def _decorate(self, item):
        item["sources"] = self.repository.list_sources(item["id"])
        item["audit"] = self.repository.audit_trail(item["id"])
        item["assessment"] = rules.assess(item["payload"])
        item["response"] = rules.response_view(item["payload"], item["status"], self.now())
        item["grade"] = item["payload"].get("grade")
        item["impact"] = item["payload"].get("impact")
        item["recurrence_of"] = item["payload"].get("recurrence_of")
        item["related_recurrences"] = item["payload"].get("related_recurrences", [])
        return item

    # -- 命令 -------------------------------------------------------------

    def create_item(self, payload, actor, role, region=None):
        if not actor or not role:
            raise DomainError("identity_required", "需要用户身份和角色", 401)
        if role not in rules.CREATE_ROLES:
            raise DomainError("forbidden", "当前角色不能创建此类业务记录", 403)
        normalized = domain.normalize_create(payload)
        stable_key = normalized.pop("_stable_key")
        # 建档即按客观上报数据定级并起算核验时限，档位由服务端计算，防止被压级
        normalized.update(rules.initial_response_fields(normalized, self.now()))
        return self.repository.create_item(
            rules.ENTITY_TYPE, stable_key, rules.INITIAL_STATUS, normalized, actor, role
        )

    def add_source(self, item_id, payload, actor, role, region=None):
        if not actor or not role:
            raise DomainError("identity_required", "需要用户身份和角色", 401)
        if role not in rules.SOURCE_ROLES:
            raise DomainError("forbidden", "当前角色不能提交来源记录", 403)
        item = self.repository.get_item(item_id)
        if item["status"] == "reported":
            item = self._run_escalation(item)
        normalized = domain.normalize_source(payload)
        if region and rules.ENFORCE_REGION and role != "regulator" and normalized.get("region") and normalized["region"] != region:
            raise DomainError("region_mismatch", "来源记录不属于当前管辖区域", 403)

        # 已恢复供气的事件又收到新的传感记录：不并入旧事件，另行建成关联事件
        if item["status"] == "restored":
            return self._create_recurrence(item, normalized, actor, role)

        result = self.repository.add_source(
            item_id,
            normalized.pop("source_type"),
            normalized.pop("external_id"),
            normalized,
            normalized.pop("observed_at"),
            actor,
            role,
        )
        return result

    def _create_recurrence(self, parent, normalized, actor, role):
        old = parent["payload"]
        sensor_ppm = normalized.get("sensor_value_ppm")
        pressure_drop = normalized.get("pressure_drop_kpa")
        odor = int(normalized.get("odor_reports", 0) or 0)
        child_payload = {
            "pipeline_id": old["pipeline_id"],
            "segment_id": old["segment_id"],
            "reported_at": normalized["observed_at"],
            "pressure_drop_kpa": float(pressure_drop if pressure_drop is not None else 0),
            "sensor_value_ppm": float(sensor_ppm if sensor_ppm is not None else 0),
            "odor_reports": odor,
            "reporter": actor,
            "source_comparison": [],
            "valve_sequence": [],
            "hazards_clear": False,
            "recurrence_of": parent["id"],
            "recurrence_note": "已恢复供气的事件 %s 在 %s 再次收到 %s/%s 记录，另立关联事件处置；与已恢复事件同管段，可能为复漏或新泄漏点。" % (
                parent["id"], normalized["observed_at"], normalized["source_type"], normalized["external_id"]
            ),
            "related_recurrences": [],
        }
        child_payload.update(rules.initial_response_fields(child_payload, self.now()))
        stable_key = "%s|%s|%s" % (
            child_payload["pipeline_id"],
            child_payload["segment_id"],
            normalized["observed_at"],
        )
        child = self.repository.create_item(
            rules.ENTITY_TYPE, stable_key, rules.INITIAL_STATUS, child_payload, actor, role
        )
        # 在已恢复事件上留下关联指针和审计，说明新记录的去向
        updated_parent = dict(old)
        related = list(old.get("related_recurrences", []))
        related.append({
            "recurrence_id": child["id"],
            "source_type": normalized["source_type"],
            "external_id": normalized["external_id"],
            "observed_at": normalized["observed_at"],
            "relation": "同一管段恢复供气后再次出现的泄漏迹象，已另行建档",
        })
        updated_parent["related_recurrences"] = related
        self.repository.link_recurrence(
            parent["id"],
            updated_parent,
            actor,
            role,
            {
                "recurrence_id": child["id"],
                "source_type": normalized["source_type"],
                "external_id": normalized["external_id"],
                "observed_at": normalized["observed_at"],
                "relation": "已恢复事件收到新传感记录，另建关联事件",
            },
        )
        return {
            "id": child["id"],
            "item_id": child["id"],
            "recurrence_of": parent["id"],
            "relation": child_payload["recurrence_note"],
            "payload": child_payload,
            "status": child["status"],
            "source_type": normalized["source_type"],
            "external_id": normalized["external_id"],
            "observed_at": normalized["observed_at"],
        }

    def act(self, item_id, action, payload, actor, role, expected_version=None, region=None):
        if not actor or not role:
            raise DomainError("identity_required", "需要用户身份和角色", 401)
        item = self.repository.get_item(item_id)
        allowed = rules.ACTION_ROLES.get(action, set())
        if role not in allowed:
            raise DomainError("forbidden", "当前角色不能执行该操作", 403)
        if rules.ENFORCE_REGION and action in rules.REGION_SENSITIVE_ACTIONS and region and role != "regulator":
            if item["payload"].get("region") != region:
                raise DomainError("region_mismatch", "不能处理其他区域的记录", 403)
        if action in rules.ACTION_REQUIRES_VERSION and expected_version is None:
            raise DomainError("expected_version_required", "该操作需要 expected_version", 400)
        # 动作执行前先推进 SLA：超时未核验自动升档。系统升级不改版本，
        # 但会随本次操作一起提交，客户端版本不会失效。
        item = self._prepare_item(item)
        new_status, new_payload, event_payload = rules.apply_action(
            item, action, payload, actor, role, self.now()
        )
        self.repository.apply_action(
            item_id, action, actor, role, new_status, new_payload, event_payload, expected_version
        )
        return self.get_item(item_id)

    def get_item(self, item_id):
        item = self.repository.get_item(item_id)
        item = self._run_escalation(item)
        return self._decorate(item)

    def list_items(self, status=None):
        items = self.repository.list_items(status)
        result = []
        for item in items:
            item = self._run_escalation(item)
            result.append(self._decorate(item))
        return result

    def state(self):
        summary = self.repository.state_summary()
        items = self.list_items()
        counts = {}
        for item in items:
            counts[item["status"]] = counts.get(item["status"], 0) + 1
        summary["items"] = items
        summary["counts"] = counts
        return summary
