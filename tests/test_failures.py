import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from src.repository import Repository
from src.service import Service
from src.domain import ConflictError, DomainError


class FailureTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.repo = Repository(self.tmp.name)
        self.repo.initialize()
        self.base = datetime(2026, 9, 27, 9, 0, 0, tzinfo=timezone.utc)
        self.current = self.base
        self.service = Service(self.repo, clock=lambda: self.current)
        self.payload = {
            "pipeline_id": "P-2",
            "segment_id": "S-4",
            "reported_at": self.base.isoformat(),
            "pressure_drop_kpa": 20,
            "sensor_value_ppm": 50,
            "odor_reports": 1,
            "reporter": "dispatch-2",
        }

    def tearDown(self):
        os.unlink(self.tmp.name)

    def test_duplicate_and_valve_conflict(self):
        item = self.service.create_item(self.payload, "d", "dispatcher")
        with self.assertRaises(ConflictError):
            self.service.create_item(self.payload, "d", "dispatcher")
        item["payload"]["valve_status_conflict"] = False
        item = self.service.act(item["id"], "verify", {"field_confirmed": True}, "r", "responder", item["version"])
        with self.assertRaises(DomainError) as context:
            self.service.act(item["id"], "isolate", {"valve_sequence": ["V-1"]}, "s", "supervisor", item["version"])
        self.assertEqual(context.exception.code, "impact_not_registered")
        item = self.service.act(item["id"], "register_impact", {
            "affected_segments": ["S-4"], "affected_users": ["U-2"]}, "s", "supervisor", item["version"])
        with self.assertRaises(DomainError) as context:
            self.service.act(item["id"], "isolate", {"valve_sequence": ["V-1"]}, "s", "supervisor", item["version"])
        self.assertEqual(context.exception.code, "valve_sequence_required")

    def test_version_conflict_and_permission(self):
        item = self.service.create_item(self.payload, "d", "dispatcher")
        with self.assertRaises(DomainError) as context:
            self.service.act(item["id"], "verify", {"field_confirmed": True}, "x", "sensor", item["version"])
        self.assertEqual(context.exception.status, 403)
        item = self.service.act(item["id"], "verify", {"field_confirmed": True}, "r", "responder", item["version"])
        item = self.service.act(item["id"], "register_impact", {
            "affected_segments": ["S-4"], "affected_users": ["U-2"]}, "s", "supervisor", item["version"])
        with self.assertRaises(ConflictError):
            self.service.act(item["id"], "isolate", {"valve_sequence": ["V-1", "V-2"]}, "s", "supervisor", item["version"] - 1)

    def test_overdue_major_auto_escalated_by_sweep(self):
        # 重大事件上报后 30 分钟内未核验
        payload = dict(self.payload, segment_id="S-9", reported_at=self.base.isoformat(),
                       pressure_drop_kpa=60, sensor_value_ppm=400, odor_reports=4)
        item = self.service.create_item(payload, "d", "dispatcher")
        self.current = self.base + timedelta(minutes=31)
        escalated = self.service.sweep_escalations()
        self.assertEqual(len(escalated), 1)
        self.assertEqual(escalated[0]["item_id"], item["id"])
        record = escalated[0]["record"]
        self.assertEqual(record["to_tier"], "major")  # 已在重大档，保持重大
        self.assertEqual(record["blocked_step"], "reported")
        self.assertEqual(record["blocked_step_label"], "现场核验")
        view = self.service.get_item(item["id"])["response_view"]
        self.assertFalse(view["overdue"])  # 升级后按新档位刷新了时限
        self.assertEqual(view["remaining_minutes"], 30.0)
        self.assertEqual(len(view["escalations"]), 1)
        self.assertIn("自动升一档", view["escalations"][0]["reason"])

    def test_overdue_elevated_steps_up(self):
        # 较大档超时升为重大
        payload = dict(self.payload, segment_id="S-10",
                       reported_at=(self.base + timedelta(seconds=1)).isoformat(),
                       pressure_drop_kpa=20, sensor_value_ppm=100, odor_reports=1)
        item = self.service.create_item(payload, "d", "dispatcher")
        self.assertEqual(item["payload"]["response"]["tier"], "elevated")
        self.current = self.base + timedelta(minutes=61)
        escalated = self.service.sweep_escalations()
        self.assertEqual(escalated[0]["record"]["from_tier"], "elevated")
        self.assertEqual(escalated[0]["record"]["to_tier"], "major")
        events = self.service.get_item(item["id"])["audit"]
        self.assertEqual(events[-1]["event_type"], "auto_escalated")
        self.assertEqual(events[-1]["actor"], "system")


if __name__ == "__main__":
    unittest.main()
