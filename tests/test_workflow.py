import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from src.repository import Repository
from src.service import Service


def iso(dt):
    return dt.isoformat()


class WorkflowTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.repo = Repository(self.tmp.name)
        self.repo.initialize()
        self.base = datetime(2026, 9, 27, 8, 0, 0, tzinfo=timezone.utc)
        self.current = self.base
        self.service = Service(self.repo, clock=lambda: self.current)

    def tearDown(self):
        os.unlink(self.tmp.name)

    def create_payload(self, **overrides):
        payload = {
            "pipeline_id": "P-1",
            "segment_id": "S-8",
            "reported_at": iso(self.base),
            "pressure_drop_kpa": 30,
            "sensor_value_ppm": 120,
            "odor_reports": 3,
            "reporter": "dispatch-1",
        }
        payload.update(overrides)
        return payload

    def test_complete_leak_workflow(self):
        item = self.service.create_item(self.create_payload(), "dispatch-1", "dispatcher")
        self.assertEqual(item["payload"]["assessment"]["tier"], "major")
        self.assertEqual(item["payload"]["response"]["tier"], "major")
        self.current = self.base + timedelta(minutes=10)
        item = self.service.act(item["id"], "verify", {"field_confirmed": True}, "resp-1", "responder", item["version"])
        item = self.service.act(item["id"], "register_impact", {
            "affected_segments": ["S-8", "S-9"],
            "affected_users": ["U-1001", "U-1002"],
        }, "sup-1", "supervisor", item["version"])
        item = self.service.act(item["id"], "isolate", {"valve_sequence": ["V-1", "V-2"]}, "sup-1", "supervisor", item["version"])
        self.assertEqual(item["payload"]["valve_sequence"], ["V-1", "V-2"])
        self.assertIn("impact", item["audit"][-1]["payload"])
        item = self.service.act(item["id"], "repair", {"work_order": "WO-1"}, "tech-1", "technician", item["version"])
        item = self.service.act(item["id"], "pressure_test", {"test_passed": True, "pressure_kpa": 150, "minimum_pressure_kpa": 100}, "tech-1", "technician", item["version"])
        item = self.service.act(item["id"], "restore", {"hazards_clear": True}, "sup-1", "supervisor", item["version"])
        self.assertEqual(item["status"], "restored")
        self.assertGreaterEqual(len(item["audit"]), 7)

    def test_major_verification_sla_30_minutes(self):
        item = self.service.create_item(self.create_payload(), "dispatch-1", "dispatcher")
        view = self.service.get_item(item["id"])["response_view"]
        self.assertEqual(view["tier"], "major")
        self.assertEqual(view["sla_minutes"], 30)
        self.assertEqual(view["remaining_minutes"], 30.0)
        # 29 分钟内核验，不升级
        self.current = self.base + timedelta(minutes=29)
        item = self.service.act(item["id"], "verify", {"field_confirmed": True}, "resp-1", "responder", item["version"])
        view = self.service.get_item(item["id"])["response_view"]
        self.assertEqual(view["tier"], "major")
        self.assertEqual(view["escalations"], [])

    def test_three_tier_assignment(self):
        major = self.service.create_item(self.create_payload(
            pressure_drop_kpa=60, sensor_value_ppm=400, odor_reports=4), "d", "dispatcher")
        elevated = self.service.create_item(self.create_payload(
            segment_id="S-20", reported_at=iso(self.base + timedelta(seconds=1)),
            pressure_drop_kpa=20, sensor_value_ppm=100, odor_reports=1), "d", "dispatcher")
        ordinary = self.service.create_item(self.create_payload(
            segment_id="S-30", reported_at=iso(self.base + timedelta(seconds=2)),
            pressure_drop_kpa=1, sensor_value_ppm=2, odor_reports=0), "d", "dispatcher")
        self.assertEqual(major["payload"]["assessment"]["tier"], "major")
        self.assertEqual(elevated["payload"]["assessment"]["tier"], "elevated")
        self.assertEqual(ordinary["payload"]["assessment"]["tier"], "ordinary")

    def test_isolation_without_impact_is_returned(self):
        item = self.service.create_item(self.create_payload(), "dispatch-1", "dispatcher")
        item = self.service.act(item["id"], "verify", {"field_confirmed": True}, "resp-1", "responder", item["version"])
        from src.domain import DomainError
        with self.assertRaises(DomainError) as context:
            self.service.act(item["id"], "isolate", {"valve_sequence": ["V-1", "V-2"]}, "sup-1", "supervisor", item["version"])
        self.assertEqual(context.exception.code, "impact_not_registered")
        self.assertEqual(context.exception.status, 422)

    def test_restored_event_gets_followup(self):
        item = self.service.create_item(self.create_payload(), "dispatch-1", "dispatcher")
        item = self.service.act(item["id"], "verify", {"field_confirmed": True}, "resp-1", "responder", item["version"])
        item = self.service.act(item["id"], "register_impact", {
            "affected_segments": ["S-8"], "affected_users": ["U-1001"]}, "sup-1", "supervisor", item["version"])
        item = self.service.act(item["id"], "isolate", {"valve_sequence": ["V-1", "V-2"]}, "sup-1", "supervisor", item["version"])
        item = self.service.act(item["id"], "repair", {"work_order": "WO-1"}, "tech-1", "technician", item["version"])
        item = self.service.act(item["id"], "pressure_test", {"test_passed": True, "pressure_kpa": 150}, "tech-1", "technician", item["version"])
        item = self.service.act(item["id"], "restore", {"hazards_clear": True}, "sup-1", "supervisor", item["version"])
        restored_id = item["id"]

        self.current = self.base + timedelta(hours=5)
        result = self.service.add_source(restored_id, {
            "source_type": "sensor",
            "external_id": "SN-9-again",
            "observed_at": iso(self.base + timedelta(hours=5)),
            "sensor_value_ppm": 260,
        }, "sensor-gw", "sensor")

        # 新记录没有并入旧事件，而是挂到另建的关联事件上
        self.assertNotEqual(result["item_id"], restored_id)
        followup = self.service.get_item(result["item_id"])
        self.assertEqual(followup["status"], "reported")
        self.assertEqual(followup["related"]["item_id"], restored_id)
        self.assertEqual(followup["related"]["relation"], "followup_after_restoration")
        old = self.service.get_item(restored_id)
        self.assertEqual(old["status"], "restored")
        self.assertEqual(len(old["sources"]), 0)
        self.assertEqual(len(followup["sources"]), 1)
        self.assertEqual(old["followups"][0]["child_item_id"], followup["id"])


if __name__ == "__main__":
    unittest.main()
