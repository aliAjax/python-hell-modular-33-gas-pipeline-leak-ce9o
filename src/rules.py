from datetime import datetime, timedelta, timezone

from .domain import DomainError

ENTITY_TYPE = "pipeline_leak"
INITIAL_STATUS = "reported"
CREATE_ROLES = {"dispatcher", "responder"}
SOURCE_ROLES = {"dispatcher", "responder", "patrol", "sensor"}
ACTION_ROLES = {
    "verify": {"dispatcher", "responder"},
    "register_impact": {"dispatcher", "responder", "supervisor"},
    "isolate": {"supervisor", "responder"},
    "repair": {"technician"},
    "pressure_test": {"technician"},
    "restore": {"supervisor"},
    "cancel": {"supervisor"},
}
ENFORCE_REGION = False
REGION_SENSITIVE_ACTIONS = set()
ACTION_REQUIRES_VERSION = {"isolate", "repair", "pressure_test", "restore", "cancel", "register_impact"}

# 分级响应：按评分分三档；时限为当前处置步距最近一次上报/升级/操作的允许时长（分钟）
TIER_ORDINARY = "ordinary"
TIER_ELEVATED = "elevated"
TIER_MAJOR = "major"

TIER_LABELS = {
    TIER_ORDINARY: "一般",
    TIER_ELEVATED: "较大",
    TIER_MAJOR: "重大",
}
TIER_ORDER = [TIER_ORDINARY, TIER_ELEVATED, TIER_MAJOR]
TIER_SLA_MINUTES = {
    TIER_ORDINARY: 180,
    TIER_ELEVATED: 60,
    TIER_MAJOR: 30,  # 重大事件上报后半小时内完成核验（并督办后续每一步）
}

# 状态机下一步：用于说明超时事件“卡在哪一步”
NEXT_STEP_LABELS = {
    "reported": "现场核验",
    "verified": "登记影响范围并隔离",
    "isolated": "抢修",
    "repaired": "压力测试",
    "tested": "恢复供气",
}
ACTIVE_STATUSES = set(NEXT_STEP_LABELS)


def parse_ts(value):
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def tier_for_score(score):
    if score >= 75:
        return TIER_MAJOR
    if score >= 45:
        return TIER_ELEVATED
    return TIER_ORDINARY


def assess(payload):
    pressure = float(payload.get("pressure_drop_kpa", 0))
    ppm = float(payload.get("sensor_value_ppm", 0))
    odor = int(payload.get("odor_reports", 0))
    score = min(100.0, pressure * 2.0 + min(ppm, 500.0) * 0.1 + odor * 5.0)
    score = round(score, 2)
    tier = tier_for_score(score)
    return {"score": score, "tier": tier, "tier_label": TIER_LABELS[tier]}


def initial_response(reported_at, assessment):
    tier = assessment["tier"]
    deadline = parse_ts(reported_at) + timedelta(minutes=TIER_SLA_MINUTES[tier])
    return {
        "tier": tier,
        "tier_label": TIER_LABELS[tier],
        "score": assessment["score"],
        "escalations": [],
        "deadline": deadline.isoformat(),
        "sla_minutes": TIER_SLA_MINUTES[tier],
    }


def escalate_response(response, now, status, reason=None):
    """超时未处置自动升一档；已在重大档则保持重大并再次登记超时。"""
    current = response.get("tier", TIER_ORDINARY)
    index = TIER_ORDER.index(current)
    new_tier = TIER_MAJOR if index >= len(TIER_ORDER) - 1 else TIER_ORDER[index + 1]
    blocked_at = NEXT_STEP_LABELS.get(status, status)
    record = {
        "at": now.isoformat(),
        "from_tier": current,
        "to_tier": new_tier,
        "blocked_step": status,
        "blocked_step_label": blocked_at,
        "reason": reason or "超时未处置，自动升一档",
        "deadline": response.get("deadline"),
    }
    response = dict(response)
    response.setdefault("escalations", []).append(record)
    response["tier"] = new_tier
    response["tier_label"] = TIER_LABELS[new_tier]
    response["sla_minutes"] = TIER_SLA_MINUTES[new_tier]
    response["deadline"] = (now + timedelta(minutes=TIER_SLA_MINUTES[new_tier])).isoformat()
    return response, record


def response_view(payload, status, now=None):
    """在存储数据上叠加动态分档/剩余时限视图，不修改事件本身。"""
    assessment = payload.get("assessment") or assess(payload)
    stored = dict(payload.get("response", {}))
    tier = stored.get("tier") or assessment["tier"]
    deadline_str = stored.get("deadline")
    remaining_minutes = overdue = None
    if deadline_str and status in ACTIVE_STATUSES:
        reference = now or datetime.now(timezone.utc)
        deadline = parse_ts(deadline_str)
        remaining_minutes = round((deadline - reference).total_seconds() / 60.0, 1)
        overdue = reference > deadline
    return {
        "tier": tier,
        "tier_label": TIER_LABELS[tier],
        "score": assessment["score"],
        "deadline": deadline_str,
        "sla_minutes": stored.get("sla_minutes", TIER_SLA_MINUTES[tier]),
        "remaining_minutes": remaining_minutes,
        "overdue": overdue,
        "blocked_step": NEXT_STEP_LABELS.get(status) if status in ACTIVE_STATUSES else None,
        "escalations": stored.get("escalations", []),
    }


def _need_status(item, allowed):
    if item["status"] not in allowed:
        raise DomainError("invalid_state", "当前状态 %s 不允许执行该操作" % item["status"])


def _text(payload, name):
    value = payload.get(name)
    if not isinstance(value, str) or not value.strip():
        raise DomainError("field_required", "%s 不能为空" % name)
    return value.strip()


def _identifier_list(payload, name):
    values = payload.get(name)
    if not isinstance(values, list) or not values:
        raise DomainError("field_required", "%s 不能为空，至少登记一项" % name)
    result = []
    for value in values:
        if not isinstance(value, str) or not value.strip():
            raise DomainError("invalid_%s" % name, "%s 条目必须是非空文本" % name)
        text = value.strip()
        if text not in result:
            result.append(text)
    return result


def _reset_deadline(current, now, tier=None):
    tier = tier or current.get("response", {}).get("tier") or TIER_ORDINARY
    response = current.setdefault("response", {})
    response["tier"] = tier
    response["tier_label"] = TIER_LABELS[tier]
    response["sla_minutes"] = TIER_SLA_MINUTES[tier]
    response["deadline"] = (now + timedelta(minutes=TIER_SLA_MINUTES[tier])).isoformat()
    response.setdefault("escalations", [])


def apply_action(item, action, payload, actor, role, now=None):
    status = item["status"]
    current = dict(item["payload"])
    now = now or datetime.now(timezone.utc)

    if action == "verify":
        _need_status(item, {"reported", "verified"})
        if current.get("valve_status_conflict"):
            raise DomainError("valve_status_conflict", "阀门状态存在冲突，不能完成核验", 409)
        confirmed = bool(payload.get("field_confirmed"))
        if not confirmed:
            raise DomainError("field_confirmation_required", "需要现场确认", 409)
        current["assessment"] = assess(current)
        current["verification"] = {"confirmed": True, "note": payload.get("note", "")}
        _reset_deadline(current, now, current["assessment"]["tier"])
        return "verified", current, {"assessment": current["assessment"], "verification": current["verification"]}

    if action == "register_impact":
        # 核验通过后登记受影响的管段和用户，隔离请求必须带上这里的影响面
        _need_status(item, {"verified"})
        affected_segments = _identifier_list(payload, "affected_segments")
        affected_users = _identifier_list(payload, "affected_users")
        current["impact"] = {
            "affected_segments": affected_segments,
            "affected_users": affected_users,
            "registered_by": actor,
            "note": payload.get("note", ""),
        }
        return "verified", current, {"impact": current["impact"]}

    if action == "isolate":
        _need_status(item, {"verified"})
        impact = current.get("impact")
        if not impact or not impact.get("affected_segments") or not impact.get("affected_users"):
            raise DomainError("impact_not_registered", "未登记影响范围（受影响管段和用户），隔离请求退回重填", 422)
        sequence = payload.get("valve_sequence")
        if not isinstance(sequence, list) or len(sequence) < 2:
            raise DomainError("valve_sequence_required", "至少需要提交两个阀门及顺序")
        if current.get("valve_status_conflict"):
            raise DomainError("valve_status_conflict", "阀门状态存在冲突，不能隔离", 409)
        if not all(isinstance(value, str) and value.strip() for value in sequence):
            raise DomainError("invalid_valve_sequence", "阀门顺序格式无效")
        current["valve_sequence"] = [value.strip() for value in sequence]
        _reset_deadline(current, now)
        # 隔离请求携带已登记的影响面，明确会影响哪些下游管段与用户
        return "isolated", current, {
            "valve_sequence": current["valve_sequence"],
            "impact": impact,
        }

    if action == "repair":
        _need_status(item, {"isolated", "repaired"})
        work_order = _text(payload, "work_order")
        current["repair"] = {"work_order": work_order, "result": payload.get("result", "completed")}
        _reset_deadline(current, now)
        return "repaired", current, {"work_order": work_order}

    if action == "pressure_test":
        _need_status(item, {"repaired", "tested"})
        if not payload.get("test_passed"):
            raise DomainError("pressure_test_failed", "压力测试未通过，不能恢复供气", 409)
        pressure = float(payload.get("pressure_kpa", 0))
        minimum = float(payload.get("minimum_pressure_kpa", 100))
        if pressure < minimum:
            raise DomainError("pressure_below_threshold", "试验压力低于最低要求", 409)
        current["pressure_test"] = {"passed": True, "pressure_kpa": pressure, "minimum_pressure_kpa": minimum}
        _reset_deadline(current, now)
        return "tested", current, {"pressure_test": current["pressure_test"]}

    if action == "restore":
        _need_status(item, {"tested"})
        if not payload.get("hazards_clear"):
            raise DomainError("hazards_not_clear", "现场危险条件尚未解除", 409)
        if not current.get("pressure_test", {}).get("passed"):
            raise DomainError("pressure_test_missing", "缺少通过的压力测试", 409)
        current["hazards_clear"] = True
        current["restoration"] = {"actor": actor, "note": payload.get("note", ""), "restored_at": now.isoformat()}
        return "restored", current, {"restoration": current["restoration"]}

    if action == "cancel":
        _need_status(item, {"reported", "verified"})
        reason = _text(payload, "reason")
        current["cancellation"] = {"reason": reason, "actor": actor}
        return "cancelled", current, {"reason": reason}

    raise DomainError("unknown_action", "不支持的操作")
