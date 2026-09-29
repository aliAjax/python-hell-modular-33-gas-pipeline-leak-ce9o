from datetime import datetime, timedelta, timezone

from .domain import DomainError, normalize_impact

ENTITY_TYPE = "pipeline_leak"
INITIAL_STATUS = "reported"
CREATE_ROLES = {"dispatcher", "responder"}
SOURCE_ROLES = {"dispatcher", "responder", "patrol", "sensor"}
ACTION_ROLES = {
    "verify": {"dispatcher", "responder"},
    "isolate": {"supervisor", "responder"},
    "repair": {"technician"},
    "pressure_test": {"technician"},
    "restore": {"supervisor"},
    "cancel": {"supervisor"},
}
ENFORCE_REGION = False
REGION_SENSITIVE_ACTIONS = set()
ACTION_REQUIRES_VERSION = {"isolate", "repair", "pressure_test", "restore", "cancel"}

# ---------------------------------------------------------------------------
# 分级响应：按评分分为 一般 / 较大 / 重大 三档
# ---------------------------------------------------------------------------
GRADE_GENERAL = "general"
GRADE_MAJOR = "major"
GRADE_CRITICAL = "critical"
GRADE_ORDER = (GRADE_GENERAL, GRADE_MAJOR, GRADE_CRITICAL)
GRADE_LABELS = {
    GRADE_GENERAL: "一般",
    GRADE_MAJOR: "较大",
    GRADE_CRITICAL: "重大",
}
# 各档“上报 -> 完成现场核验”的处置时限（分钟）。重大为半小时。
GRADE_SLA_MINUTES = {
    GRADE_GENERAL: 120,
    GRADE_MAJOR: 60,
    GRADE_CRITICAL: 30,
}

STEP_LABELS = {
    "reported": "待现场核验",
    "verified": "待隔离",
    "isolated": "待修复",
    "repaired": "待试压",
    "tested": "待恢复供气",
    "restored": "已恢复供气",
    "cancelled": "已取消",
}

LEVEL_LABELS = {
    "critical": "紧急",
    "high": "高",
    "medium": "中",
    "low": "低",
}


def assess(payload):
    pressure = float(payload.get("pressure_drop_kpa", 0))
    ppm = float(payload.get("sensor_value_ppm", 0))
    odor = int(payload.get("odor_reports", 0))
    score = min(100.0, pressure * 2.0 + min(ppm, 500.0) * 0.1 + odor * 5.0)
    if score >= 75:
        level = "critical"
    elif score >= 45:
        level = "high"
    elif score >= 20:
        level = "medium"
    else:
        level = "low"
    return {"score": round(score, 2), "level": level}


def parse_dt(value):
    """解析 ISO 时间，无时区标记时按 UTC 处理。"""
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def to_iso(value):
    return value.isoformat()


def grade_for_score(score):
    if score >= 75:
        return GRADE_CRITICAL
    if score >= 45:
        return GRADE_MAJOR
    return GRADE_GENERAL


def grade_rank(grade):
    return GRADE_ORDER.index(grade) if grade in GRADE_ORDER else 0


def next_grade(grade):
    """超时自动升一档；已到重大档时维持重大（由调用方记持续告警）。"""
    index = grade_rank(grade)
    return GRADE_ORDER[min(index + 1, len(GRADE_ORDER) - 1)]


def initial_response_fields(payload, now):
    """建档时按客观上报数据定级并起算核验时限，档位不接受客户端指定。"""
    assessment = assess(payload)
    grade = grade_for_score(assessment["score"])
    reported_at = parse_dt(payload["reported_at"])
    sla_minutes = GRADE_SLA_MINUTES[grade]
    return {
        "grade": grade,
        "grading": {
            "score": assessment["score"],
            "level": assessment["level"],
            "grade": grade,
            "grade_label": GRADE_LABELS[grade],
            "sla_minutes": sla_minutes,
            "graded_at": to_iso(now),
            "basis": "initial_report",
        },
        "response_sla": {
            "grade": grade,
            "reported_at": payload["reported_at"],
            "sla_minutes": sla_minutes,
            "anchor_at": to_iso(reported_at),
            "due_at": to_iso(reported_at + timedelta(minutes=sla_minutes)),
            "completed_at": None,
            "history": [],
        },
    }


def check_escalation(payload, status, now):
    """
    惰性 SLA 检查：仅针对尚未完成现场核验（reported）的事件。
    超时未处置则自动升一档，并写清卡在哪一步；已到重大档则记持续告警。
    返回 (new_payload, audit_payload)，无需升级时返回 (None, None)。
    """
    if status != "reported":
        return None, None
    sla = payload.get("response_sla")
    if not sla or not sla.get("due_at"):
        return None, None
    due_at = parse_dt(sla["due_at"])
    if now <= due_at:
        return None, None

    old_grade = sla.get("grade", GRADE_GENERAL)
    new_grade = next_grade(old_grade)
    at_top = new_grade == old_grade
    overdue_seconds = max(0, int((now - due_at).total_seconds()))
    entry_type = "overdue_top_alert" if at_top else "overdue_escalation"
    blocked_step = "reported"
    if at_top:
        reason = "已为最高档（重大），超过 %d 分钟核验时限仍未完成现场核验，记持续告警" % sla.get("sla_minutes", GRADE_SLA_MINUTES[GRADE_CRITICAL])
    else:
        reason = "超过%s档 %d 分钟核验时限仍未完成现场核验，自动升至%s档" % (
            GRADE_LABELS[old_grade],
            sla.get("sla_minutes", GRADE_SLA_MINUTES[old_grade]),
            GRADE_LABELS[new_grade],
        )
    history_entry = {
        "type": entry_type,
        "from_grade": old_grade,
        "to_grade": new_grade,
        "from_grade_label": GRADE_LABELS[old_grade],
        "to_grade_label": GRADE_LABELS[new_grade],
        "at": to_iso(now),
        "previous_due_at": sla["due_at"],
        "overdue_seconds": overdue_seconds,
        "blocked_step": blocked_step,
        "blocked_step_label": STEP_LABELS[blocked_step],
        "reason": reason,
    }
    new_sla_minutes = GRADE_SLA_MINUTES[new_grade]
    new_sla = dict(sla)
    new_sla["grade"] = new_grade
    new_sla["sla_minutes"] = new_sla_minutes
    new_sla["anchor_at"] = to_iso(now)
    new_sla["due_at"] = to_iso(now + timedelta(minutes=new_sla_minutes))
    new_sla["history"] = list(sla.get("history", [])) + [history_entry]

    new_payload = dict(payload)
    new_payload["response_sla"] = new_sla
    new_payload["grade"] = new_grade
    audit_payload = {
        "type": entry_type,
        "from_grade": old_grade,
        "to_grade": new_grade,
        "previous_due_at": sla["due_at"],
        "new_due_at": new_sla["due_at"],
        "overdue_seconds": overdue_seconds,
        "blocked_step": blocked_step,
        "blocked_step_label": STEP_LABELS[blocked_step],
        "reason": reason,
    }
    return new_payload, audit_payload


def apply_verified_grade(current, assessment, now):
    """核验复评：分数对应档位更高则升级，更低不得降级（防止重大被压成普通）。"""
    sla = current.get("response_sla")
    if not sla:
        # 兼容建档时没有 SLA 信息的旧数据
        fields = initial_response_fields(current, now)
        current["grade"] = fields["grade"]
        sla = fields["response_sla"]
    suggested = grade_for_score(assessment["score"])
    old_grade = sla.get("grade", suggested)
    history = list(sla.get("history", []))
    if grade_rank(suggested) > grade_rank(old_grade):
        history.append({
            "type": "verification_upgrade",
            "from_grade": old_grade,
            "to_grade": suggested,
            "from_grade_label": GRADE_LABELS[old_grade],
            "to_grade_label": GRADE_LABELS[suggested],
            "at": to_iso(now),
            "blocked_step": "reported",
            "blocked_step_label": STEP_LABELS["reported"],
            "reason": "现场核验评分高于初判档位，上调至%s档" % GRADE_LABELS[suggested],
        })
        old_grade = suggested
    sla["grade"] = old_grade
    sla["completed_at"] = to_iso(now)
    sla["verified_at"] = to_iso(now)
    sla["history"] = history
    current["response_sla"] = sla
    current["grade"] = old_grade
    current["grading"] = {
        "score": assessment["score"],
        "level": assessment["level"],
        "grade": old_grade,
        "grade_label": GRADE_LABELS[old_grade],
        "sla_minutes": sla.get("sla_minutes", GRADE_SLA_MINUTES[old_grade]),
        "graded_at": to_iso(now),
        "basis": "field_verification",
    }


def response_view(payload, status, now):
    """供页面/接口展示的分档、剩余时限与卡点摘要。"""
    sla = payload.get("response_sla")
    if not sla:
        return None
    completed_at = sla.get("completed_at")
    due_at = parse_dt(sla["due_at"])
    grade = sla.get("grade", payload.get("grade"))
    history = sla.get("history", [])
    view = {
        "grade": grade,
        "grade_label": GRADE_LABELS.get(grade, grade),
        "sla_minutes": sla.get("sla_minutes"),
        "reported_at": sla.get("reported_at"),
        "due_at": sla["due_at"],
        "completed_at": completed_at,
        "blocked_step": status,
        "blocked_step_label": STEP_LABELS.get(status, status),
        "escalation_count": len([item for item in history if item["type"] != "verification_upgrade"]),
        "history": history,
    }
    if completed_at:
        completed = parse_dt(completed_at)
        view["verified_on_time"] = completed <= due_at
        view["remaining_seconds"] = None
        view["overdue"] = completed > due_at
        view["sla_breached"] = completed > due_at or view["escalation_count"] > 0
    else:
        remaining = int((due_at - now).total_seconds())
        view["remaining_seconds"] = remaining
        view["overdue"] = remaining < 0
        view["verified_on_time"] = None
        view["sla_breached"] = view["escalation_count"] > 0
    return view


def _need_status(item, allowed):
    if item["status"] not in allowed:
        raise DomainError("invalid_state", "当前状态 %s 不允许执行该操作" % item["status"])


def _text(payload, name):
    value = payload.get(name)
    if not isinstance(value, str) or not value.strip():
        raise DomainError("field_required", "%s 不能为空" % name)
    return value.strip()


def _optional_number(payload, name):
    if name not in payload:
        return None
    value = payload[name]
    if isinstance(value, bool):
        raise DomainError("invalid_number", "%s 必须是数字" % name)
    try:
        return float(value)
    except (TypeError, ValueError):
        raise DomainError("invalid_number", "%s 必须是数字" % name)


def _impact_signature(impact):
    return set(impact["segments"]), {customer["customer_id"] for customer in impact["customers"]}


def apply_action(item, action, payload, actor, role, now=None):
    status = item["status"]
    current = dict(item["payload"])

    if action == "verify":
        _need_status(item, {"reported", "verified"})
        if current.get("valve_status_conflict"):
            raise DomainError("valve_status_conflict", "阀门状态存在冲突，不能完成核验", 409)
        confirmed = bool(payload.get("field_confirmed"))
        if not confirmed:
            raise DomainError("field_confirmation_required", "需要现场确认", 409)
        # 核验通过时必须登记受影响管段与下游用户，供隔离使用
        impact = normalize_impact(payload)
        if now is not None:
            impact = dict(impact, registered_at=to_iso(now))
        # 允许现场核验时提交修正读数，评分以核验数据为准但档位只升不降
        score_input = dict(current)
        for field in ("pressure_drop_kpa", "sensor_value_ppm", "odor_reports"):
            override = _optional_number(payload, "field_" + field)
            if override is not None:
                score_input[field] = override
        assessment = assess(score_input)
        current["assessment"] = assessment
        current["verification"] = {"confirmed": True, "note": payload.get("note", "")}
        current["impact"] = impact
        if now is not None:
            apply_verified_grade(current, assessment, now)
        return "verified", current, {"assessment": current["assessment"], "verification": current["verification"], "impact": impact}

    if action == "isolate":
        _need_status(item, {"verified"})
        registered = current.get("impact")
        if not registered or not registered.get("segments") or not registered.get("customers"):
            raise DomainError(
                "impact_not_registered",
                "尚未登记受影响管段和下游用户，隔离请求退回重填",
                409,
            )
        # 隔离请求必须带上核验时登记的影响面，且与登记一致；没带或不一致一律退回重填
        has_impact = "affected_segments" in payload or "affected_customers" in payload or "impact" in payload
        if not has_impact:
            raise DomainError(
                "impact_required",
                "隔离请求必须携带受影响管段和下游用户（登记管段：%s；下游用户：%s），退回重填"
                % ("、".join(registered["segments"]), "、".join(c["customer_id"] for c in registered["customers"])),
                409,
            )
        sequence = payload.get("valve_sequence")
        if not isinstance(sequence, list) or len(sequence) < 2:
            raise DomainError("valve_sequence_required", "至少需要提交两个阀门及顺序，隔离请求退回重填")
        if not all(isinstance(value, str) and value.strip() for value in sequence):
            raise DomainError("invalid_valve_sequence", "阀门顺序格式无效，隔离请求退回重填")
        requested = normalize_impact(payload)
        if _impact_signature(requested) != _impact_signature(registered):
            raise DomainError(
                "impact_mismatch",
                "隔离请求携带的影响面与核验登记不一致（登记管段：%s；下游用户：%s），退回重填"
                % ("、".join(registered["segments"]), "、".join(c["customer_id"] for c in registered["customers"])),
                409,
            )
        if current.get("valve_status_conflict"):
            raise DomainError("valve_status_conflict", "阀门状态存在冲突，不能隔离", 409)
        current["valve_sequence"] = [value.strip() for value in sequence]
        current["isolation_impact"] = requested
        return "isolated", current, {"valve_sequence": current["valve_sequence"], "isolation_impact": requested}

    if action == "repair":
        _need_status(item, {"isolated", "repaired"})
        work_order = _text(payload, "work_order")
        current["repair"] = {"work_order": work_order, "result": payload.get("result", "completed")}
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
        return "tested", current, {"pressure_test": current["pressure_test"]}

    if action == "restore":
        _need_status(item, {"tested"})
        if not payload.get("hazards_clear"):
            raise DomainError("hazards_not_clear", "现场危险条件尚未解除", 409)
        if not current.get("pressure_test", {}).get("passed"):
            raise DomainError("pressure_test_missing", "缺少通过的压力测试", 409)
        current["hazards_clear"] = True
        current["restoration"] = {"actor": actor, "note": payload.get("note", "")}
        return "restored", current, {"restoration": current["restoration"]}

    if action == "cancel":
        _need_status(item, {"reported", "verified"})
        reason = _text(payload, "reason")
        current["cancellation"] = {"reason": reason, "actor": actor}
        return "cancelled", current, {"reason": reason}

    raise DomainError("unknown_action", "不支持的操作")
