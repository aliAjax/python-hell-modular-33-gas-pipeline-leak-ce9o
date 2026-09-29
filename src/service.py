from datetime import datetime, timezone

from . import domain, rules
from .domain import ConflictError, DomainError, NotFoundError


class Service:
    def __init__(self, repository, clock=None):
        self.repository = repository
        self.clock = clock

    def now(self):
        return self.clock() if self.clock else datetime.now(timezone.utc)

    def create_item(self, payload, actor, role, region=None):
        if not actor or not role:
            raise DomainError("identity_required", "需要用户身份和角色", 401)
        if role not in rules.CREATE_ROLES:
            raise DomainError("forbidden", "当前角色不能创建此类业务记录", 403)
        normalized = domain.normalize_create(payload)
        stable_key = normalized.pop("_stable_key")
        reported_at = normalized["reported_at"]
        assessment = rules.assess(normalized)
        normalized["assessment"] = assessment
        normalized["response"] = rules.initial_response(reported_at, assessment)
        return self.repository.create_item(
            rules.ENTITY_TYPE, stable_key, rules.INITIAL_STATUS, normalized, actor, role
        )

    def add_source(self, item_id, payload, actor, role, region=None):
        if not actor or not role:
            raise DomainError("identity_required", "需要用户身份和角色", 401)
        if role not in rules.SOURCE_ROLES:
            raise DomainError("forbidden", "当前角色不能提交来源记录", 403)
        item = self.repository.get_item(item_id)
        normalized = domain.normalize_source(payload)
        if region and rules.ENFORCE_REGION and role != "regulator" and normalized.get("region") and normalized["region"] != region:
            raise DomainError("region_mismatch", "来源记录不属于当前管辖区域", 403)
        observed_at = normalized.pop("observed_at")
        source_type = normalized.pop("source_type")
        external_id = normalized.pop("external_id")

        # 已恢复供气的事件又收到新的来源（传感/巡检）记录时，不并入旧事件：
        # 另行建成关联（复发）事件，并说明与已恢复事件的关系。
        if item["status"] == "restored":
            followup = self._create_followup(item, source_type, external_id, normalized, observed_at, actor, role)
            result = self.repository.add_source(
                followup["id"], source_type, external_id, normalized, observed_at, actor, role
            )
            result["followup_item_id"] = followup["id"]
            result["related_item_id"] = item["id"]
            return result

        # 写入前先做一次超时督办，避免超时限被新记录掩盖
        self.sweep_escalations(exclude_id=item_id)
        result = self.repository.add_source(
            item_id, source_type, external_id, normalized, observed_at, actor, role
        )
        return result

    def _create_followup(self, parent, source_type, external_id, source_payload, observed_at, actor, role):
        parent_payload = parent["payload"]
        fields = ["pipeline_id", "segment_id"]
        new_payload = {field: parent_payload[field] for field in fields}
        new_payload["reported_at"] = observed_at
        # 复发事件以本次来源读数为初始评分依据，缺失的读数按 0 计
        new_payload["pressure_drop_kpa"] = source_payload.get("pressure_drop_kpa") or 0
        new_payload["sensor_value_ppm"] = source_payload.get("sensor_value_ppm") or 0
        new_payload["odor_reports"] = source_payload.get("odor_reports") or 0
        new_payload["reporter"] = actor
        new_payload["source_comparison"] = []
        new_payload["valve_sequence"] = []
        new_payload["hazards_clear"] = False
        assessment = rules.assess(new_payload)
        new_payload["assessment"] = assessment
        new_payload["response"] = rules.initial_response(observed_at, assessment)
        new_payload["related_to"] = {
            "item_id": parent["id"],
            "relation": "followup_after_restoration",
            "relation_label": "已恢复供气事件的复发关联事件",
            "source_type": source_type,
            "external_id": external_id,
            "description": "原事件已恢复供气后再次收到%s记录，另建事件处理" % source_type,
            "observed_at": observed_at,
        }
        stable_key = "followup|%s|%s|%s|%s" % (parent["id"], new_payload["pipeline_id"], new_payload["segment_id"], observed_at)
        try:
            followup = self.repository.create_item(
                rules.ENTITY_TYPE, stable_key, rules.INITIAL_STATUS, new_payload,
                actor, role,
                created_event_type="followup_created",
                created_event_payload={"parent_item_id": parent["id"], "relation": "followup_after_restoration",
                                       "source_type": source_type, "external_id": external_id},
            )
        except ConflictError:
            existing = self.repository.find_item_by_stable_key(rules.ENTITY_TYPE, stable_key)
            return existing
        # 在已恢复的旧事件上留痕，说明复发记录已另案处理
        self.repository.record_relation(
            parent["id"], followup["id"], "restored_followup_link", actor, role,
            {"followup_item_id": followup["id"], "source_type": source_type,
             "external_id": external_id, "observed_at": observed_at,
             "description": "已恢复后收到新记录，未并入本事件，已另建关联事件"},
        )
        return followup

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
        # 操作前先督办超时（可能由系统升级改变版本），升级记录不强制用户重试
        self.sweep_escalations()
        item = self.repository.get_item(item_id)
        new_status, new_payload, event_payload = rules.apply_action(item, action, payload, actor, role, self.now())
        self.repository.apply_action(
            item_id, action, actor, role, new_status, new_payload, event_payload, expected_version,
        )
        return self.get_item(item_id)

    def sweep_escalations(self, exclude_id=None):
        """扫描在办事件：超过当前档位时限仍未推进的，自动升一档并登记卡在哪一步。"""
        now = self.now()
        escalated = []
        for item in self.repository.list_items():
            if item["id"] == exclude_id:
                continue
            if item["status"] not in rules.ACTIVE_STATUSES:
                continue
            response = item["payload"].get("response") or {}
            deadline_str = response.get("deadline")
            if not deadline_str:
                continue
            if now <= rules.parse_ts(deadline_str):
                continue
            new_response, record = rules.escalate_response(response, now, item["status"])
            record = self.repository.record_escalation(item["id"], new_response, record, "system", "system")
            escalated.append({"item_id": item["id"], "record": record})
        return escalated

    def get_item(self, item_id):
        item = self.repository.get_item(item_id)
        item["sources"] = self.repository.list_sources(item_id)
        item["audit"] = self.repository.audit_trail(item["id"])
        stored_assessment = item["payload"].get("assessment")
        item["assessment"] = stored_assessment or rules.assess(item["payload"])
        item["response_view"] = rules.response_view(item["payload"], item["status"], self.now())
        item["impact"] = item["payload"].get("impact")
        related = item["payload"].get("related_to")
        item["related"] = related
        item["followups"] = self.repository.list_relations(item["id"])
        return item

    def list_items(self, status=None):
        items = self.repository.list_items(status)
        now = self.now()
        for item in items:
            stored_assessment = item["payload"].get("assessment")
            item["assessment"] = stored_assessment or rules.assess(item["payload"])
            item["response_view"] = rules.response_view(item["payload"], item["status"], now)
            item["impact"] = item["payload"].get("impact")
            item["related"] = item["payload"].get("related_to")
        return items

    def state(self):
        self.sweep_escalations()
        return self.repository.state_summary()
