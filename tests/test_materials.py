import json
import unittest
from pathlib import Path

from src import referral_materials as rm

SAMPLE = json.loads(
    (Path(__file__).parents[1] / "data" / "materials_sample.json").read_text(encoding="utf-8")
)
EVENTS = SAMPLE["events"]
REF = "ref-0922-01"
NOW = "2026-09-23T10:00:00+08:00"          # ov-002 放行有效、授权已过期
AFTER_OVERRIDE = "2026-09-23T21:00:00+08:00"  # ov-002 已到期收回


class ContractTest(unittest.TestCase):
    def test_every_sample_event_matches_contract(self):
        for event in EVENTS:
            self.assertEqual(rm.validate_event(event), [], event["event_id"])

    def test_sample_covers_every_material_kind(self):
        kinds = {event["kind"] for event in EVENTS}
        self.assertTrue(set(rm.MATERIAL_EVENT_KINDS) <= kinds)

    def test_unknown_kind_and_missing_payload_field_are_reported(self):
        self.assertEqual(rm.validate_event({"kind": "NOPE"}), ["event_id", "occurred_at", "subject_id", "payload", "kind"])
        bad = {"event_id": "x", "kind": "OVERRIDE_GRANTED", "occurred_at": NOW,
               "subject_id": "s", "payload": {"override_id": "ov"}}
        self.assertEqual(rm.validate_event(bad), ["payload.referral_id", "payload.item_id", "payload.expires_at"])


class ChecklistVersionTest(unittest.TestCase):
    def setUp(self):
        self.state = rm.replay(EVENTS)

    def test_checklist_at_respects_effective_window(self):
        checklists = self.state["checklists"]
        self.assertEqual(rm.checklist_at(checklists, "EMERGENCY_TRANSFER", "2026-09-15T00:00:00+08:00")["version"], 3)
        self.assertEqual(rm.checklist_at(checklists, "EMERGENCY_TRANSFER", "2026-10-15T00:00:00+08:00")["version"], 4)
        self.assertIsNone(rm.checklist_at(checklists, "EMERGENCY_TRANSFER", "2026-08-01T00:00:00+08:00"))

    def test_departed_referral_keeps_bound_version_after_upgrade(self):
        referral = self.state["referrals"][REF]
        bound = rm.bind_checklist(referral, self.state["checklists"], "2026-10-15T00:00:00+08:00")
        self.assertEqual(bound["version"], 3)  # 清单升级后已出发患者仍按 v3 核对
        trace = rm.trace_blocker(self.state, REF, "CLINICAL_INTAKE", NOW)
        self.assertEqual(trace["checklist"]["version"], 3)


class MaterialTest(unittest.TestCase):
    def setUp(self):
        self.state = rm.replay(EVENTS)
        self.checklist = rm.find_checklist(self.state["checklists"], "cl-erreq", 3)

    def test_correction_keeps_old_version_and_tracks_reads(self):
        followups = rm.correction_followups(self.state["receipts"], self.state["reads"])
        self.assertEqual(len(followups), 2)  # ref-0922-01 与 ref-0923-02 各一次更正
        followup = next(f for f in followups if f["old_receipt_id"] == "rc-lab-1")
        self.assertTrue(followup["old_version_read"])    # 接收方读过旧版
        self.assertEqual(followup["new_receipt_id"], "rc-lab-2")
        self.assertFalse(followup["new_version_read"])   # 更正版尚未读
        kept = {r["receipt_id"] for r in self.state["receipts"]}
        self.assertIn("rc-lab-1", kept)                  # 旧版保留

    def test_same_digest_from_multiple_channels_is_merged(self):
        merged = rm.merge_receipts(self.state["receipts"])
        imaging = [
            m for m in merged["materials"]
            if m["referral_id"] == REF and m["item_id"] == "imaging-status"
        ]
        self.assertEqual(len(imaging), 1)
        self.assertEqual(imaging[0]["channels"], ["mail-gateway", "pacs-gateway"])
        self.assertEqual(imaging[0]["receipt_ids"], ["rc-img-1", "rc-img-2"])

    def test_conflict_pauses_only_dependent_steps(self):
        merged = rm.merge_receipts(self.state["receipts"])
        # ref-0923-02 的影像冲突已被更正解除，只剩 ref-0922-01 的运输交接冲突
        self.assertEqual(
            [(c["referral_id"], c["item_id"]) for c in merged["conflicts"]],
            [(REF, "transport-handover")],
        )
        categories = {i["item_id"]: i["category"] for i in self.checklist["items"]}
        paused = rm.paused_steps(merged["conflicts"], categories)
        self.assertEqual(paused, ["CLINICAL_INTAKE"])  # 运输与床位准备继续

    def test_missing_items_reflect_expiry_conflict_and_substitute(self):
        def missing(at):
            return {
                m["item_id"]: m["reasons"]
                for m in rm.missing_items(
                    self.checklist, REF, self.state["receipts"],
                    self.state["substitutes"], self.state["overrides"], at,
                )
            }
        now_missing = missing(NOW)
        self.assertNotIn("patient-consent", now_missing)      # ov-002 限时放行覆盖
        self.assertNotIn("receiving-confirm", now_missing)    # 替代材料覆盖
        self.assertNotIn("sealed-lab-report", now_missing)    # 更正版已签章
        self.assertEqual(now_missing["transport-handover"], ["conflicted"])
        later = missing(AFTER_OVERRIDE)
        self.assertIn("expired", later["patient-consent"])    # 放行到期自动收回，授权仍过期

    def test_override_only_for_explicit_missing_item_with_future_expiry(self):
        missing = rm.missing_items(
            self.checklist, REF, self.state["receipts"],
            self.state["substitutes"], self.state["overrides"], NOW,
        )
        with self.assertRaises(ValueError):
            rm.grant_override(missing, {"override_id": "ov-x", "referral_id": REF,
                                        "item_id": "imaging-status", "granted_by": "g",
                                        "expires_at": "2026-09-24T00:00:00+08:00"}, NOW)
        with self.assertRaises(ValueError):
            rm.grant_override(missing, {"override_id": "ov-y", "referral_id": REF,
                                        "item_id": "transport-handover", "granted_by": "g",
                                        "expires_at": "2026-09-23T09:00:00+08:00"}, NOW)
        granted = rm.grant_override(
            missing, {"override_id": "ov-z", "referral_id": REF, "item_id": "transport-handover",
                      "granted_by": "g", "expires_at": "2026-09-24T00:00:00+08:00"}, NOW)
        self.assertEqual(granted["granted_at"], NOW)


class PlanAndViewTest(unittest.TestCase):
    def setUp(self):
        self.state = rm.replay(EVENTS)
        self.checklist = rm.find_checklist(self.state["checklists"], "cl-erreq", 3)

    def plan(self, context, at):
        return rm.recompute_plan(
            self.checklist, REF, self.state["receipts"], self.state["substitutes"],
            self.state["overrides"], context, at,
        )

    def test_recompute_plan_reflects_context_changes(self):
        window = {"start": "2026-09-23T07:00:00+08:00", "end": "2026-09-23T19:00:00+08:00"}
        plan = self.plan({"bed_window": window, "emergency_level": "URGENT"}, NOW)
        status = {s["step"]: s["status"] for s in plan["steps"]}
        self.assertEqual(status["CLINICAL_INTAKE"], "paused")   # 冲突只暂停接诊
        self.assertEqual(status["TRANSPORT"], "ready")          # 放行覆盖过期授权
        self.assertEqual(status["BED"], "ready")
        self.assertFalse(plan["executable"])
        self.assertEqual(plan["waived"][0]["override_id"], "ov-002")

        night = "2026-09-23T22:00:00+08:00"
        plan = self.plan({"bed_window": window, "emergency_level": "ROUTINE"}, night)
        status = {s["step"]: s["status"] for s in plan["steps"]}
        self.assertEqual(status["BED"], "waiting")              # 床位窗口外等待
        self.assertEqual(status["TRANSPORT"], "blocked")        # 放行已收回，授权过期

        plan = self.plan({"bed_window": window, "emergency_level": "CRITICAL"}, night)
        status = {s["step"]: s["status"] for s in plan["steps"]}
        self.assertEqual(status["BED"], "ready")                # 危重急诊不等床位窗口

    def test_handover_view_lists_unfinished_responsibilities(self):
        board = rm.handover_view(self.state, NOW)
        self.assertEqual([entry["referral_id"] for entry in board], [REF])
        entry = board[0]
        self.assertEqual(entry["checklist"]["version"], 3)
        self.assertEqual(entry["paused_steps"], ["CLINICAL_INTAKE"])
        self.assertEqual(entry["unread_corrections"][0]["new_receipt_id"], "rc-lab-2")
        self.assertEqual(entry["active_overrides"][0]["override_id"], "ov-002")
        self.assertEqual(entry["responsibility"]["accepted_by"], "nurse-night-02")

    def test_patient_view_shares_missing_items_with_coordinator(self):
        patient = rm.patient_view(self.state, REF, NOW)
        status = rm.referral_status(self.state, REF, NOW)
        self.assertEqual(patient["missing"], status["missing"])
        self.assertEqual(patient["next_step"]["action"], "RESOLVE_CONFLICT")

    def test_trace_blocker_returns_version_receipts_and_override_basis(self):
        trace = rm.trace_blocker(self.state, REF, "CLINICAL_INTAKE", NOW)
        self.assertEqual(trace["checklist"]["version"], 3)
        receipt_ids = {r["receipt_id"] for r in trace["receipts"]}
        self.assertTrue({"rc-lab-1", "rc-lab-2", "rc-th-1", "rc-th-2"} <= receipt_ids)
        self.assertEqual([c["item_id"] for c in trace["conflicts"]], ["transport-handover"])

        trace = rm.trace_blocker(self.state, REF, "TRANSPORT", NOW)
        self.assertEqual([o["override_id"] for o in trace["overrides"]], ["ov-002"])

    def test_reminder_not_repeated_across_midnight_for_same_leg(self):
        missing = rm.missing_items(
            self.checklist, REF, self.state["receipts"],
            self.state["substitutes"], self.state["overrides"], AFTER_OVERRIDE,
        )
        sent = set(self.state["reminders"])  # 23:40 已对 patient-consent 催办
        due = rm.due_reminders(missing, REF, "leg-0922-01", sent)
        self.assertEqual(due, [rm.reminder_key(REF, "transport-handover", "leg-0922-01")])
        # 跨午夜后同一运输段仍不重复；新运输段才重新催办
        self.assertNotIn(rm.reminder_key(REF, "patient-consent", "leg-0922-01"), due)
        next_leg = rm.due_reminders(missing, REF, "leg-0923-02", sent)
        self.assertIn(rm.reminder_key(REF, "patient-consent", "leg-0923-02"), next_leg)


if __name__ == "__main__":
    unittest.main()
