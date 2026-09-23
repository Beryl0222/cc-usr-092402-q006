import json
import unittest
from pathlib import Path

from src.referral_materials import Ledger, validate_event

SAMPLE = Path(__file__).parents[1] / "data" / "referral_materials_sample.jsonl"


def load_sample():
    return [
        json.loads(line)
        for line in SAMPLE.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def base_record(**overrides):
    record = {
        "event_id": "t-001",
        "kind": "MATERIAL_RECEIVED",
        "occurred_at": "2026-09-22T09:00:00+08:00",
        "subject_id": "ref-x",
        "payload": {
            "receipt_id": "rcpt-1",
            "item_key": "labs",
            "category": "EXAM_RESULT",
            "channel": "LIS",
            "digest": "sha256:aa",
            "sealed": True,
            "exam_state": "COMPLETE",
        },
    }
    record.update(overrides)
    return record


class MinimalContractTest(unittest.TestCase):
    def test_missing_minimal_fields(self):
        self.assertEqual(
            validate_event({"kind": "SHIFT_OPENED"}),
            ["event_id", "occurred_at", "subject_id", "payload"],
        )

    def test_unknown_kind_rejected(self):
        record = base_record(kind="SOMETHING_ELSE")
        self.assertEqual(validate_event(record), ["kind"])

    def test_naive_timestamp_rejected(self):
        record = base_record(occurred_at="2026-09-22 09:00:00")
        self.assertEqual(validate_event(record), ["occurred_at"])

    def test_non_dict_payload_rejected(self):
        record = base_record(payload="not-a-dict")
        self.assertEqual(validate_event(record), ["payload"])


class PayloadRuleTest(unittest.TestCase):
    def test_material_requires_digest(self):
        record = base_record()
        del record["payload"]["digest"]
        self.assertEqual(validate_event(record), ["digest"])

    def test_exam_result_requires_exam_state(self):
        record = base_record()
        del record["payload"]["exam_state"]
        self.assertEqual(validate_event(record), ["exam_state"])

    def test_authorization_requires_expiry(self):
        record = base_record(payload={
            "receipt_id": "rcpt-2",
            "item_key": "patient_auth",
            "category": "PATIENT_AUTHORIZATION",
            "channel": "PORTAL",
            "digest": "sha256:bb",
            "sealed": True,
        })
        self.assertEqual(validate_event(record), ["auth_expires_at"])

    def test_substitute_requires_target(self):
        record = base_record(payload={
            "receipt_id": "rcpt-3",
            "item_key": "bedside_echo",
            "category": "SUBSTITUTE",
            "channel": "PORTAL",
            "digest": "sha256:cc",
            "sealed": True,
        })
        self.assertEqual(validate_event(record), ["substitutes_for"])

    def test_waiver_requires_expiry_and_reason(self):
        record = base_record(kind="WAIVER_GRANTED", payload={
            "waiver_id": "wvr-1",
            "item_keys": ["labs"],
            "granted_by": "协调员甲",
            "reason": "",
            "expires_at": "2026-09-23T02:00:00+08:00",
        })
        self.assertEqual(validate_event(record), ["reason"])
        record["payload"]["reason"] = "随车后补"
        del record["payload"]["expires_at"]
        self.assertEqual(validate_event(record), ["expires_at"])

    def test_condition_change_rejects_unknown_key(self):
        record = base_record(kind="REFERRAL_CONDITION_CHANGED", payload={
            "changes": {"insurance_level": "A"},
            "reason": "测试",
        })
        self.assertEqual(validate_event(record), ["changes"])

    def test_checklist_rejects_duplicate_item_keys(self):
        record = base_record(kind="CHECKLIST_VERSION_PUBLISHED", payload={
            "referral_type": "INTER_HOSPITAL_ICU",
            "version": "t-1",
            "effective_from": "2026-09-01T00:00:00+08:00",
            "items": [
                {"item_key": "labs", "category": "EXAM_RESULT", "gates": ["CLINICAL_INTAKE"]},
                {"item_key": "labs", "category": "EXAM_RESULT", "gates": []},
            ],
        })
        self.assertEqual(validate_event(record), ["items"])

    def test_checklist_rejects_unknown_step(self):
        record = base_record(kind="CHECKLIST_VERSION_PUBLISHED", payload={
            "referral_type": "INTER_HOSPITAL_ICU",
            "version": "t-2",
            "effective_from": "2026-09-01T00:00:00+08:00",
            "items": [
                {"item_key": "labs", "category": "EXAM_RESULT", "gates": ["FLY_HOME"]},
            ],
        })
        self.assertEqual(validate_event(record), ["items"])


class SampleStreamContractTest(unittest.TestCase):
    def test_sample_stream_replays_without_problems(self):
        ledger = Ledger()
        self.assertEqual(ledger.apply_all(load_sample()), {})

    def test_every_sample_event_passes_stateless_validation(self):
        for record in load_sample():
            self.assertEqual(validate_event(record), [], record["event_id"])


if __name__ == "__main__":
    unittest.main()
