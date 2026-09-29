import os
import sys
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from src.rules import assess, escalate_response, response_view, TIER_MAJOR, TIER_ELEVATED, TIER_ORDINARY
from src.domain import DomainError


class RuleTest(unittest.TestCase):
    def test_leak_score_tiers(self):
        major = assess({"pressure_drop_kpa": 60, "sensor_value_ppm": 400, "odor_reports": 4})
        ordinary = assess({"pressure_drop_kpa": 1, "sensor_value_ppm": 2, "odor_reports": 0})
        self.assertEqual(major["tier"], "major")
        self.assertEqual(major["tier_label"], "重大")
        self.assertEqual(ordinary["tier"], "ordinary")
        self.assertGreater(major["score"], ordinary["score"])

    def test_elevated_tier_band(self):
        # 20*2 + 60*0.1 + 1*5 = 51 → 较大
        elevated = assess({"pressure_drop_kpa": 20, "sensor_value_ppm": 60, "odor_reports": 1})
        self.assertEqual(elevated["tier"], "elevated")

    def test_timeout_escalation_records_blocked_step(self):
        now = datetime(2026, 9, 27, 8, 0, 0, tzinfo=timezone.utc)
        response = {
            "tier": TIER_ORDINARY,
            "tier_label": "一般",
            "score": 10,
            "escalations": [],
            "deadline": (now - timedelta(minutes=1)).isoformat(),
            "sla_minutes": 180,
        }
        new_response, record = escalate_response(response, now, "reported")
        self.assertEqual(record["from_tier"], TIER_ORDINARY)
        self.assertEqual(record["to_tier"], TIER_ELEVATED)
        self.assertEqual(record["blocked_step"], "reported")
        self.assertEqual(record["blocked_step_label"], "现场核验")
        self.assertEqual(new_response["tier"], TIER_ELEVATED)
        self.assertEqual(len(new_response["escalations"]), 1)
        # 再次超时：较大升为重大
        new_response["deadline"] = (now - timedelta(minutes=1)).isoformat()
        new_response, record2 = escalate_response(new_response, now + timedelta(hours=2), "verified")
        self.assertEqual(record2["from_tier"], TIER_ELEVATED)
        self.assertEqual(record2["to_tier"], TIER_MAJOR)
        self.assertEqual(record2["blocked_step_label"], "登记影响范围并隔离")
        # 重大档超时保持重大，但仍要再次写清卡在哪一步
        new_response["deadline"] = (now - timedelta(minutes=1)).isoformat()
        new_response, record3 = escalate_response(new_response, now + timedelta(hours=3), "isolated")
        self.assertEqual(record3["from_tier"], TIER_MAJOR)
        self.assertEqual(record3["to_tier"], TIER_MAJOR)
        self.assertEqual(record3["blocked_step_label"], "抢修")
        self.assertEqual(len(new_response["escalations"]), 3)

    def test_response_view_remaining_time(self):
        now = datetime(2026, 9, 27, 8, 0, 0, tzinfo=timezone.utc)
        payload = {
            "assessment": {"score": 90, "tier": TIER_MAJOR, "tier_label": "重大"},
            "response": {
                "tier": TIER_MAJOR,
                "deadline": (now + timedelta(minutes=12)).isoformat(),
                "sla_minutes": 30,
                "escalations": [],
            },
        }
        view = response_view(payload, "reported", now)
        self.assertEqual(view["remaining_minutes"], 12.0)
        self.assertFalse(view["overdue"])
        self.assertEqual(view["blocked_step"], "现场核验")
        view = response_view(payload, "restored", now)
        self.assertIsNone(view["remaining_minutes"])
        self.assertFalse(view["overdue"])

    def test_restore_requires_hazard_clearance(self):
        item = {
            "status": "tested",
            "payload": {"pressure_test": {"passed": True}, "valve_status_conflict": False},
        }
        from src.rules import apply_action
        with self.assertRaises(DomainError) as context:
            apply_action(item, "restore", {"hazards_clear": False}, "s", "supervisor")
        self.assertEqual(context.exception.code, "hazards_not_clear")

    def test_isolate_requires_registered_impact(self):
        item = {
            "status": "verified",
            "payload": {"valve_status_conflict": False, "assessment": {"score": 90, "tier": "major"}},
        }
        from src.rules import apply_action
        with self.assertRaises(DomainError) as context:
            apply_action(item, "isolate", {"valve_sequence": ["V-1", "V-2"]}, "s", "supervisor")
        self.assertEqual(context.exception.code, "impact_not_registered")


if __name__ == "__main__":
    unittest.main()
