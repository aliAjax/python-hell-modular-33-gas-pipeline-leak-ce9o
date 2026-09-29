import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from src.repository import Repository
from src.service import Service
from src.domain import DomainError
from src.rules import (
    GRADE_GENERAL,
    GRADE_MAJOR,
    GRADE_CRITICAL,
    GRADE_LABELS,
    GRADE_SLA_MINUTES,
    grade_for_score,
)

UTC = timezone.utc


class FakeClock:
    def __init__(self, value):
        self.value = value

    def __call__(self):
        return self.value

    def advance(self, minutes):
        self.value += timedelta(minutes=minutes)


def base_payload(**overrides):
    payload = {
        "pipeline_id": "P-9",
        "segment_id": "S-9",
        "reported_at": "2026-09-29T08:00:00+00:00",
        "pressure_drop_kpa": 0,
        "sensor_value_ppm": 0,
        "odor_reports": 0,
        "reporter": "dispatch-9",
    }
    payload.update(overrides)
    return payload


IMPACT = {
    "affected_segments": ["S-9", "S-9-DOWN"],
    "affected_customers": [{"customer_id": "U-1", "name": "一号用户"}, {"customer_id": "U-2"}],
}


class GradingTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.clock = FakeClock(datetime(2026, 9, 29, 8, 0, tzinfo=UTC))
        self.repo = Repository(self.tmp.name)
        self.repo.initialize()
        self.service = Service(self.repo, clock=self.clock)

    def tearDown(self):
        os.unlink(self.tmp.name)

    def _create(self, **overrides):
        return self.service.create_item(base_payload(**overrides), "d", "dispatcher")

    def test_score_maps_to_three_grades(self):
        self.assertEqual(grade_for_score(0), GRADE_GENERAL)
        self.assertEqual(grade_for_score(44.9), GRADE_GENERAL)
        self.assertEqual(grade_for_score(45), GRADE_MAJOR)
        self.assertEqual(grade_for_score(74.9), GRADE_MAJOR)
        self.assertEqual(grade_for_score(75), GRADE_CRITICAL)

    def test_graded_at_creation(self):
        # 重大：30*2 + 120*0.1 + 4*5 = 92
        critical = self._create(pressure_drop_kpa=30, sensor_value_ppm=120, odor_reports=4)
        self.assertEqual(critical["payload"]["grade"], GRADE_CRITICAL)
        # 较大：10*2 + 300*0.1 + 1*5 = 55
        major = self._create(segment_id="S-8", pressure_drop_kpa=10, sensor_value_ppm=300, odor_reports=1)
        self.assertEqual(major["payload"]["grade"], GRADE_MAJOR)
        # 一般：1*2 + 20*0.1 = 4
        general = self._create(segment_id="S-7", pressure_drop_kpa=1, sensor_value_ppm=20, odor_reports=0)
        self.assertEqual(general["payload"]["grade"], GRADE_GENERAL)

    def test_critical_sla_is_thirty_minutes(self):
        item = self._create(pressure_drop_kpa=30, sensor_value_ppm=120, odor_reports=4)
        sla = item["payload"]["response_sla"]
        self.assertEqual(sla["sla_minutes"], 30)
        self.assertEqual(sla["due_at"], "2026-09-29T08:30:00+00:00")
        view = self.service.get_item(item["id"])["response"]
        self.assertEqual(view["remaining_seconds"], 1800)
        self.assertFalse(view["overdue"])
        self.assertEqual(view["grade_label"], GRADE_LABELS[GRADE_CRITICAL])

    def test_other_grades_have_own_sla(self):
        general = self._create(segment_id="S-7", pressure_drop_kpa=1, sensor_value_ppm=20)
        self.assertEqual(general["payload"]["response_sla"]["sla_minutes"], GRADE_SLA_MINUTES[GRADE_GENERAL])
        major = self._create(segment_id="S-8", pressure_drop_kpa=10, sensor_value_ppm=300, odor_reports=1)
        self.assertEqual(major["payload"]["response_sla"]["sla_minutes"], GRADE_SLA_MINUTES[GRADE_MAJOR])

    def test_on_time_verification_closes_sla(self):
        item = self._create(pressure_drop_kpa=30, sensor_value_ppm=120, odor_reports=4)
        self.clock.advance(20)  # 20 分钟内核验
        item = self.service.act(item["id"], "verify", dict({"field_confirmed": True}, **IMPACT),
                                "r", "responder", item["version"])
        view = item["response"]
        self.assertIsNotNone(view["completed_at"])
        self.assertTrue(view["verified_on_time"])
        self.assertEqual(view["escalation_count"], 0)
        self.assertIsNone(view["remaining_seconds"])

    def test_critical_overdue_escalates_and_records_blocked_step(self):
        item = self._create(pressure_drop_kpa=10, sensor_value_ppm=300, odor_reports=1)  # 55 分，较大
        self.assertEqual(item["payload"]["grade"], GRADE_MAJOR)
        self.clock.advance(61)  # 超过较大档 60 分钟
        item = self.service.get_item(item["id"])
        sla = item["payload"]["response_sla"]
        self.assertEqual(sla["grade"], GRADE_CRITICAL)
        self.assertEqual(item["grade"], GRADE_CRITICAL)
        entry = sla["history"][-1]
        self.assertEqual(entry["type"], "overdue_escalation")
        self.assertEqual(entry["from_grade"], GRADE_MAJOR)
        self.assertEqual(entry["to_grade"], GRADE_CRITICAL)
        self.assertEqual(entry["blocked_step"], "reported")
        self.assertEqual(entry["blocked_step_label"], "待现场核验")
        # 新档按 30 分钟重新起算
        self.assertEqual(sla["sla_minutes"], 30)
        self.assertEqual(item["response"]["escalation_count"], 1)
        # 审计链中有系统升级事件
        kinds = [event["event_type"] for event in item["audit"]]
        self.assertIn("sla_escalation", kinds)

    def test_critical_overdue_records_alert_and_keeps_critical(self):
        item = self._create(pressure_drop_kpa=30, sensor_value_ppm=120, odor_reports=4)
        self.assertEqual(item["payload"]["grade"], GRADE_CRITICAL)
        self.clock.advance(31)
        item = self.service.get_item(item["id"])
        entry = item["payload"]["response_sla"]["history"][-1]
        self.assertEqual(entry["type"], "overdue_top_alert")
        self.assertEqual(entry["to_grade"], GRADE_CRITICAL)
        self.assertEqual(entry["blocked_step"], "reported")
        self.assertGreater(entry["overdue_seconds"], 0)

    def test_escalation_triggered_before_action_does_not_invalidate_version(self):
        item = self._create(pressure_drop_kpa=10, sensor_value_ppm=300, odor_reports=1)
        version = item["version"]
        self.clock.advance(61)  # 已超时
        # 用旧 version 直接提交动作：系统升级随本次动作提交，不应产生版本冲突
        item = self.service.act(item["id"], "verify", dict({"field_confirmed": True}, **IMPACT),
                                "r", "responder", version)
        self.assertEqual(item["status"], "verified")
        self.assertEqual(item["payload"]["grade"], GRADE_CRITICAL)
        # 核验前已超时升档，应标记为曾超时（sla_breached）
        self.assertTrue(item["response"]["sla_breached"])
        self.assertEqual(item["response"]["escalation_count"], 1)

    def test_verification_cannot_downgrade(self):
        # 建档即重大（92 分），核验时现场读数很低（一般档），仍不得降级
        item = self._create(pressure_drop_kpa=30, sensor_value_ppm=120, odor_reports=4)
        item = self.service.act(
            item["id"], "verify",
            {"field_confirmed": True, "field_pressure_drop_kpa": 1, "field_sensor_value_ppm": 5,
             "field_odor_reports": 0, **IMPACT},
            "r", "responder", item["version"],
        )
        self.assertEqual(item["payload"]["grade"], GRADE_CRITICAL)
        self.assertEqual(item["payload"]["assessment"]["score"], 2.5)

    def test_verification_can_upgrade(self):
        # 建档一般（4 分），现场核验读数很高（80.5 分 -> 重大），应升级并记录
        item = self._create(pressure_drop_kpa=1, sensor_value_ppm=20)
        item = self.service.act(
            item["id"], "verify",
            {"field_confirmed": True, "field_pressure_drop_kpa": 30, "field_sensor_value_ppm": 200,
             "field_odor_reports": 2, **IMPACT},
            "r", "responder", item["version"],
        )
        self.assertEqual(item["payload"]["grade"], GRADE_CRITICAL)
        entry = item["payload"]["response_sla"]["history"][-1]
        self.assertEqual(entry["type"], "verification_upgrade")
        self.assertEqual(entry["to_grade"], GRADE_CRITICAL)


class ImpactTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.clock = FakeClock(datetime(2026, 9, 29, 8, 0, tzinfo=UTC))
        self.repo = Repository(self.tmp.name)
        self.repo.initialize()
        self.service = Service(self.repo, clock=self.clock)

    def tearDown(self):
        os.unlink(self.tmp.name)

    def _verified(self):
        item = self.service.create_item(base_payload(pressure_drop_kpa=1, sensor_value_ppm=20), "d", "dispatcher")
        item = self.service.act(item["id"], "verify", dict({"field_confirmed": True}, **IMPACT),
                                "r", "responder", item["version"])
        return item

    def test_verify_requires_impact(self):
        item = self.service.create_item(base_payload(), "d", "dispatcher")
        with self.assertRaises(DomainError) as ctx:
            self.service.act(item["id"], "verify", {"field_confirmed": True},
                             "r", "responder", item["version"])
        self.assertEqual(ctx.exception.code, "affected_segments_required")

    def test_verify_registers_segments_and_customers(self):
        item = self._verified()
        impact = item["payload"]["impact"]
        self.assertEqual(impact["segments"], ["S-9", "S-9-DOWN"])
        self.assertEqual([c["customer_id"] for c in impact["customers"]], ["U-1", "U-2"])
        self.assertIn("registered_at", impact)

    def test_isolate_without_impact_is_returned(self):
        item = self._verified()
        with self.assertRaises(DomainError) as ctx:
            self.service.act(item["id"], "isolate", {"valve_sequence": ["V-1", "V-2"]},
                             "s", "supervisor", item["version"])
        self.assertEqual(ctx.exception.code, "impact_required")
        self.assertIn("退回重填", str(ctx.exception))

    def test_isolate_with_wrong_impact_is_returned(self):
        item = self._verified()
        payload = {
            "valve_sequence": ["V-1", "V-2"],
            "affected_segments": ["S-9", "S-OTHER"],
            "affected_customers": ["U-1", "U-2"],
        }
        with self.assertRaises(DomainError) as ctx:
            self.service.act(item["id"], "isolate", payload, "s", "supervisor", item["version"])
        self.assertEqual(ctx.exception.code, "impact_mismatch")

    def test_isolate_with_matching_impact_succeeds(self):
        item = self._verified()
        item = self.service.act(item["id"], "isolate", dict({"valve_sequence": ["V-1", "V-2"]}, **IMPACT),
                                "s", "supervisor", item["version"])
        self.assertEqual(item["status"], "isolated")
        self.assertEqual(item["payload"]["isolation_impact"]["segments"], ["S-9", "S-9-DOWN"])


class RecurrenceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.clock = FakeClock(datetime(2026, 9, 29, 8, 0, tzinfo=UTC))
        self.repo = Repository(self.tmp.name)
        self.repo.initialize()
        self.service = Service(self.repo, clock=self.clock)
        self.impact = {
            "affected_segments": ["S-5", "S-5-DOWN"],
            "affected_customers": ["U-9"],
        }

    def tearDown(self):
        os.unlink(self.tmp.name)

    def _restore(self):
        item = self.service.create_item(
            base_payload(pipeline_id="P-5", segment_id="S-5", pressure_drop_kpa=1, sensor_value_ppm=20),
            "d", "dispatcher")
        item = self.service.act(item["id"], "verify", dict({"field_confirmed": True}, **self.impact),
                                "r", "responder", item["version"])
        item = self.service.act(item["id"], "isolate", dict({"valve_sequence": ["V-1", "V-2"]}, **self.impact),
                                "s", "supervisor", item["version"])
        item = self.service.act(item["id"], "repair", {"work_order": "WO-9"}, "t", "technician", item["version"])
        item = self.service.act(item["id"], "pressure_test",
                                {"test_passed": True, "pressure_kpa": 150, "minimum_pressure_kpa": 100},
                                "t", "technician", item["version"])
        item = self.service.act(item["id"], "restore", {"hazards_clear": True}, "s", "supervisor", item["version"])
        return item

    def test_new_sensor_after_restore_creates_related_event(self):
        parent = self._restore()
        self.assertEqual(parent["status"], "restored")
        result = self.service.add_source(
            parent["id"],
            {"source_type": "sensor", "external_id": "SN-2", "observed_at": "2026-09-30T02:00:00+00:00",
             "sensor_value_ppm": 260, "pressure_drop_kpa": 12},
            "sensor-bot", "sensor",
        )
        child_id = result["id"]
        self.assertNotEqual(child_id, parent["id"])
        self.assertEqual(result["recurrence_of"], parent["id"])
        self.assertIn("已恢复", result["relation"])

        child = self.service.get_item(child_id)
        self.assertEqual(child["status"], "reported")
        self.assertEqual(child["payload"]["recurrence_of"], parent["id"])
        self.assertEqual(child["payload"]["pipeline_id"], "P-5")
        self.assertEqual(child["payload"]["segment_id"], "S-5")
        # 新事件独立定级、独立起算 SLA
        self.assertEqual(child["payload"]["response_sla"]["reported_at"], "2026-09-30T02:00:00+00:00")

        # 父事件保留已恢复状态，并登记关联指针
        parent_view = self.service.get_item(parent["id"])
        self.assertEqual(parent_view["status"], "restored")
        related = parent_view["related_recurrences"]
        self.assertEqual(len(related), 1)
        self.assertEqual(related[0]["recurrence_id"], child_id)
        kinds = [event["event_type"] for event in parent_view["audit"]]
        self.assertIn("recurrence_linked", kinds)

    def test_new_source_before_restore_stays_on_same_event(self):
        item = self.service.create_item(base_payload(), "d", "dispatcher")
        result = self.service.add_source(
            item["id"],
            {"source_type": "patrol", "external_id": "P-1", "observed_at": "2026-09-29T08:10:00+00:00",
             "odor_reports": 2, "note": "闻到明显气味"},
            "p", "patrol",
        )
        self.assertEqual(result["item_id"], item["id"])
        self.assertNotIn("recurrence_of", result.get("payload", {}))
        self.assertEqual(len(self.service.get_item(item["id"])["sources"]), 1)


if __name__ == "__main__":
    unittest.main()
