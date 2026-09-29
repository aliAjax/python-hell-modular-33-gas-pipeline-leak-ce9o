import os
import sys
import tempfile
import unittest

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
        self.service = Service(self.repo)
        self.payload = {
            "pipeline_id": "P-2",
            "segment_id": "S-4",
            "reported_at": "2026-09-27T09:00:00+00:00",
            "pressure_drop_kpa": 20,
            "sensor_value_ppm": 50,
            "odor_reports": 1,
            "reporter": "dispatch-2",
        }

    IMPACT = {
        "affected_segments": ["S-4", "S-4-DOWN"],
        "affected_customers": ["U-201"],
    }

    def tearDown(self):
        os.unlink(self.tmp.name)

    def test_duplicate_and_valve_conflict(self):
        item = self.service.create_item(self.payload, "d", "dispatcher")
        with self.assertRaises(ConflictError):
            self.service.create_item(self.payload, "d", "dispatcher")
        item["payload"]["valve_status_conflict"] = False
        item = self.service.act(item["id"], "verify", dict({"field_confirmed": True}, **self.IMPACT), "r", "responder", item["version"])
        # 没登记影响面的隔离退回重填
        with self.assertRaises(DomainError) as context:
            self.service.act(item["id"], "isolate", {"valve_sequence": ["V-1"]}, "s", "supervisor", item["version"])
        self.assertEqual(context.exception.code, "impact_required")
        # 阀门数量不足
        with self.assertRaises(DomainError) as context:
            self.service.act(item["id"], "isolate", dict({"valve_sequence": ["V-1"]}, **self.IMPACT), "s", "supervisor", item["version"])
        self.assertEqual(context.exception.code, "valve_sequence_required")
        # 影响面与登记不一致退回重填
        bad_impact = {"affected_segments": ["S-4", "OTHER"], "affected_customers": ["U-201"]}
        with self.assertRaises(DomainError) as context:
            self.service.act(item["id"], "isolate", dict({"valve_sequence": ["V-1", "V-2"]}, **bad_impact), "s", "supervisor", item["version"])
        self.assertEqual(context.exception.code, "impact_mismatch")

    def test_version_conflict_and_permission(self):
        item = self.service.create_item(self.payload, "d", "dispatcher")
        with self.assertRaises(DomainError) as context:
            self.service.act(item["id"], "verify", dict({"field_confirmed": True}, **self.IMPACT), "x", "sensor", item["version"])
        self.assertEqual(context.exception.status, 403)
        item = self.service.act(item["id"], "verify", dict({"field_confirmed": True}, **self.IMPACT), "r", "responder", item["version"])
        with self.assertRaises(ConflictError):
            self.service.act(item["id"], "isolate", dict({"valve_sequence": ["V-1", "V-2"]}, **self.IMPACT), "s", "supervisor", item["version"] - 1)


if __name__ == "__main__":
    unittest.main()
