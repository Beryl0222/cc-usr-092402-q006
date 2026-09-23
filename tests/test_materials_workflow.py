import json
import unittest
from pathlib import Path

from src.referral_materials import Ledger

SAMPLE = Path(__file__).parents[1] / "data" / "referral_materials_sample.jsonl"

T0 = "2026-09-22T08:00:00+08:00"


def evt(kind, subject, at, **payload):
    return {
        "event_id": f"{kind.lower()}-{subject}-{at}",
        "kind": kind,
        "occurred_at": at,
        "subject_id": subject,
        "payload": payload,
    }


def checklist_items(extra=()):
    return [
        {"item_key": "patient_auth", "label": "知情授权书", "category": "PATIENT_AUTHORIZATION",
         "gates": ["TRANSPORT_DISPATCH", "CLINICAL_INTAKE"]},
        {"item_key": "ct_scan", "label": "CT 影像", "category": "EXAM_RESULT",
         "requires_seal": True, "accepts_substitute": True,
         "gates": ["TRANSPORT_DISPATCH", "CLINICAL_INTAKE"]},
        {"item_key": "labs", "label": "检验报告", "category": "EXAM_RESULT",
         "requires_seal": True, "gates": ["CLINICAL_INTAKE"]},
        {"item_key": "transfer_summary", "label": "转诊小结", "category": "CLINICAL_DOCUMENT",
         "requires_seal": True, "gates": ["BED_RESERVATION"]},
        {"item_key": "transport_record", "label": "运输交接单", "category": "TRANSPORT_HANDOVER",
         "gates": ["CLINICAL_INTAKE"]},
        {"item_key": "receiving_confirm", "label": "接收科室确认", "category": "RECEIVING_CONFIRMATION",
         "gates": []},
        *extra,
    ]


def publish(version="2026.09", at="2026-09-22T08:05:00+08:00", **overrides):
    payload = {
        "referral_type": "INTER_HOSPITAL_ICU",
        "version": version,
        "effective_from": "2026-09-01T00:00:00+08:00",
        "items": checklist_items(),
    }
    payload.update(overrides)
    return evt("CHECKLIST_VERSION_PUBLISHED", f"INTER_HOSPITAL_ICU:{version}", at, **payload)


def bind(referral="ref-1", at="2026-09-22T09:00:00+08:00", version="2026.09", conditions=None):
    payload = {"referral_type": "INTER_HOSPITAL_ICU", "checklist_version": version}
    if conditions is not None:
        payload["conditions"] = conditions
    return evt("REFERRAL_CHECKLIST_BOUND", referral, at, **payload)


CATEGORIES = {
    "patient_auth": "PATIENT_AUTHORIZATION",
    "ct_scan": "EXAM_RESULT",
    "labs": "EXAM_RESULT",
    "transfer_summary": "CLINICAL_DOCUMENT",
    "transport_record": "TRANSPORT_HANDOVER",
    "receiving_confirm": "RECEIVING_CONFIRMATION",
}


def material(receipt_id, item_key, at, referral="ref-1", **overrides):
    payload = {
        "receipt_id": receipt_id,
        "item_key": item_key,
        "category": CATEGORIES.get(item_key, "CLINICAL_DOCUMENT"),
        "channel": "PORTAL",
        "digest": f"sha256:{receipt_id}",
        "sealed": True,
    }
    if payload["category"] == "EXAM_RESULT":
        payload["exam_state"] = "COMPLETE"
    if payload["category"] == "PATIENT_AUTHORIZATION":
        payload["auth_expires_at"] = "2027-01-01T00:00:00+08:00"
    payload.update(overrides)
    return evt("MATERIAL_RECEIVED", referral, at, **payload)


def base_ledger():
    ledger = Ledger()
    problems = ledger.apply_all([
        evt("SHIFT_OPENED", "shift-A", T0, shift_id="shift-A"),
        publish(),
        bind(),
    ])
    assert not problems, problems
    return ledger


def plan_step(ledger, referral_id, step, now):
    plan = ledger.compute_plan(referral_id, now)
    return next(s for s in plan["steps"] if s["step"] == step)


class EnRoutePainPointsTest(unittest.TestCase):
    """接收护士最怕的场景：影像仍在上传、授权刚过期、报告只是未签章草稿。"""

    def test_uploading_expired_and_unsigned_are_distinct_issues(self):
        ledger = base_ledger()
        ledger.apply_all([
            material("r-auth", "patient_auth", "2026-09-22T09:10:00+08:00",
                     auth_expires_at="2026-09-22T20:00:00+08:00"),
            material("r-ct", "ct_scan", "2026-09-22T09:20:00+08:00",
                     exam_state="UPLOADING", sealed=False),
            material("r-lab", "labs", "2026-09-22T09:30:00+08:00", sealed=False),
            material("r-sum", "transfer_summary", "2026-09-22T09:40:00+08:00"),
            material("r-tr", "transport_record", "2026-09-22T19:50:00+08:00"),
        ])
        now = "2026-09-22T20:30:00+08:00"  # 患者已在途，授权刚刚过期
        statuses = ledger.item_statuses("ref-1", now)
        self.assertEqual(statuses["patient_auth"]["status"], "AUTH_EXPIRED")
        self.assertEqual(statuses["ct_scan"]["status"], "EXAM_INCOMPLETE")
        self.assertEqual(statuses["labs"]["status"], "SEAL_MISSING")
        self.assertEqual(plan_step(ledger, "ref-1", "TRANSPORT_DISPATCH", now)["state"], "BLOCKED")
        self.assertEqual(plan_step(ledger, "ref-1", "CLINICAL_INTAKE", now)["state"], "BLOCKED")
        self.assertEqual(plan_step(ledger, "ref-1", "BED_RESERVATION", now)["state"], "READY")


class ChecklistPinningTest(unittest.TestCase):
    """清单升级不能把已出发患者变成没有记录。"""

    def test_upgrade_does_not_rebind_inflight_referral(self):
        ledger = base_ledger()
        ledger.apply(material("r-sum", "transfer_summary", "2026-09-22T10:00:00+08:00"))
        extra = {"item_key": "pathogen_screen", "label": "病原学筛查", "category": "EXAM_RESULT",
                 "requires_seal": True, "gates": ["CLINICAL_INTAKE"]}
        self.assertEqual(ledger.apply(publish(
            version="2026.10", at="2026-09-23T00:00:00+08:00",
            effective_from="2026-09-23T00:00:00+08:00",
            items=checklist_items(extra=[extra]))), [])
        ref = ledger.referrals["ref-1"]
        self.assertEqual(ref.checklist_version, "2026.09")
        statuses = ledger.item_statuses("ref-1", "2026-09-23T01:00:00+08:00")
        self.assertNotIn("pathogen_screen", statuses)
        self.assertEqual(statuses["transfer_summary"]["status"], "OK")
        plan = ledger.compute_plan("ref-1", "2026-09-23T01:00:00+08:00")
        self.assertEqual(plan["checklist_version"], "2026.09")

    def test_explicit_rebind_requires_current_version_and_reason(self):
        ledger = base_ledger()
        ledger.apply(publish(version="2026.10", at="2026-09-23T00:00:00+08:00",
                             effective_from="2026-09-23T00:00:00+08:00"))
        bad = evt("REFERRAL_CHECKLIST_REBOUND", "ref-1", "2026-09-23T01:00:00+08:00",
                  from_version="2026.08", to_version="2026.10", reason="升级到新版清单")
        self.assertEqual(ledger.apply(bad), ["from_version"])
        good = evt("REFERRAL_CHECKLIST_REBOUND", "ref-1", "2026-09-23T01:00:00+08:00",
                   from_version="2026.09", to_version="2026.10", reason="升级到新版清单")
        self.assertEqual(ledger.apply(good), [])
        self.assertEqual(ledger.referrals["ref-1"].checklist_version, "2026.10")

    def test_bind_requires_existing_effective_checklist(self):
        ledger = Ledger()
        ledger.apply(evt("SHIFT_OPENED", "shift-A", T0, shift_id="shift-A"))
        # 清单不存在时不能绑定
        self.assertEqual(ledger.apply(bind()), ["checklist_version"])
        # 清单尚未生效时不能绑定
        ledger.apply(publish(effective_from="2026-10-01T00:00:00+08:00"))
        self.assertEqual(ledger.apply(bind()), ["effective_from"])
        # 同一版本不可重复发布
        self.assertEqual(ledger.apply(publish()), ["version"])

    def test_duplicate_bind_rejected(self):
        ledger = base_ledger()
        self.assertEqual(ledger.apply(bind()), ["subject_id"])


class CorrectionReadTest(unittest.TestCase):
    """已签发材料更正时留下旧版，并知道接收方是否读过。"""

    def test_correction_keeps_old_version_and_tracks_reads(self):
        ledger = base_ledger()
        ledger.apply(material("r-lab-1", "labs", "2026-09-22T10:00:00+08:00", sealed=False))
        self.assertEqual(
            ledger.item_status("ref-1", "labs", "2026-09-22T10:05:00+08:00")["status"],
            "SEAL_MISSING")
        ledger.apply(evt("MATERIAL_READ_RECORDED", "ref-1", "2026-09-22T10:10:00+08:00",
                         receipt_id="r-lab-1", reader_role="接收护士"))
        ledger.apply(material("r-lab-2", "labs", "2026-09-22T10:30:00+08:00",
                              supersedes="r-lab-1"))
        status = ledger.item_status("ref-1", "labs", "2026-09-22T10:35:00+08:00")
        self.assertEqual(status["status"], "OK")
        self.assertEqual(status["history"], ["r-lab-1"])
        self.assertTrue(status["correction_unread"])
        ledger.apply(evt("MATERIAL_READ_RECORDED", "ref-1", "2026-09-22T10:40:00+08:00",
                         receipt_id="r-lab-2", reader_role="接收医生"))
        status = ledger.item_status("ref-1", "labs", "2026-09-22T10:45:00+08:00")
        self.assertFalse(status["correction_unread"])
        self.assertEqual(status["read_by"], ["接收医生"])

    def test_supersedes_must_point_to_same_referral_and_item(self):
        ledger = base_ledger()
        ledger.apply(material("r-lab-1", "labs", "2026-09-22T10:00:00+08:00"))
        bad = material("r-ct-1", "ct_scan", "2026-09-22T10:10:00+08:00", supersedes="r-lab-1")
        self.assertEqual(ledger.apply(bad), ["supersedes"])
        ghost = material("r-ct-2", "ct_scan", "2026-09-22T10:10:00+08:00", supersedes="r-none")
        self.assertEqual(ledger.apply(ghost), ["supersedes"])


class MultiChannelMergeTest(unittest.TestCase):
    """相同材料从多个渠道到达可按摘要归并。"""

    def test_same_digest_from_multiple_channels_merges(self):
        ledger = base_ledger()
        ledger.apply(material("r-sum-1", "transfer_summary", "2026-09-22T11:00:00+08:00",
                              digest="sha256:d501", channel="PORTAL"))
        ledger.apply(material("r-sum-2", "transfer_summary", "2026-09-22T11:05:00+08:00",
                              digest="sha256:d501", channel="FAX"))
        status = ledger.item_status("ref-1", "transfer_summary", "2026-09-22T11:10:00+08:00")
        self.assertEqual(status["status"], "OK")
        self.assertEqual(status["channels"], ["FAX", "PORTAL"])
        self.assertEqual(status["receipt_ids"], ["r-sum-1", "r-sum-2"])
        self.assertEqual(status["digest"], "sha256:d501")


class ConflictTest(unittest.TestCase):
    """内容冲突只暂停依赖它的环节，且只能裁定、不能放行。"""

    def _conflicted_ledger(self):
        ledger = base_ledger()
        ledger.apply_all([
            material("r-auth", "patient_auth", "2026-09-22T09:10:00+08:00"),
            material("r-lab", "labs", "2026-09-22T09:30:00+08:00"),
            material("r-sum", "transfer_summary", "2026-09-22T09:40:00+08:00"),
            material("r-tr", "transport_record", "2026-09-22T09:50:00+08:00"),
            material("r-ct-1", "ct_scan", "2026-09-22T10:05:00+08:00", digest="sha256:a"),
            material("r-ct-2", "ct_scan", "2026-09-22T10:10:00+08:00",
                     digest="sha256:b", channel="ESB"),
        ])
        return ledger

    def test_conflict_pauses_only_dependent_steps(self):
        ledger = self._conflicted_ledger()
        now = "2026-09-22T10:15:00+08:00"
        self.assertEqual(ledger.item_status("ref-1", "ct_scan", now)["status"], "CONFLICTED")
        plan = ledger.compute_plan("ref-1", now)
        states = {s["step"]: s["state"] for s in plan["steps"]}
        self.assertEqual(states["TRANSPORT_DISPATCH"], "PAUSED")
        self.assertEqual(states["CLINICAL_INTAKE"], "PAUSED")
        self.assertEqual(states["BED_RESERVATION"], "READY")

    def test_conflict_cannot_be_waived_only_resolved(self):
        ledger = self._conflicted_ledger()
        waiver = evt("WAIVER_GRANTED", "ref-1", "2026-09-22T10:20:00+08:00",
                     waiver_id="wvr-1", item_keys=["ct_scan"], granted_by="协调员甲",
                     reason="先发车", expires_at="2026-09-22T12:00:00+08:00")
        self.assertEqual(ledger.apply(waiver), ["item_keys"])
        bad = evt("MATERIAL_CONFLICT_RESOLVED", "ref-1", "2026-09-22T10:25:00+08:00",
                  item_key="ct_scan", winning_receipt_id="r-sum",
                  resolved_by="协调员甲", reason="以正式版为准")
        self.assertEqual(ledger.apply(bad), ["winning_receipt_id"])
        good = evt("MATERIAL_CONFLICT_RESOLVED", "ref-1", "2026-09-22T10:25:00+08:00",
                   item_key="ct_scan", winning_receipt_id="r-ct-1",
                   resolved_by="协调员甲", reason="以 PACS 正式版为准")
        self.assertEqual(ledger.apply(good), [])
        self.assertEqual(
            ledger.item_status("ref-1", "ct_scan", "2026-09-22T10:30:00+08:00")["status"], "OK")

    def test_correction_chain_after_resolution_still_ok(self):
        ledger = self._conflicted_ledger()
        ledger.apply(evt("MATERIAL_CONFLICT_RESOLVED", "ref-1", "2026-09-22T10:25:00+08:00",
                         item_key="ct_scan", winning_receipt_id="r-ct-1",
                         resolved_by="协调员甲", reason="以 PACS 正式版为准"))
        ledger.apply(material("r-ct-3", "ct_scan", "2026-09-22T10:40:00+08:00",
                              digest="sha256:c", supersedes="r-ct-1"))
        self.assertEqual(
            ledger.item_status("ref-1", "ct_scan", "2026-09-22T10:45:00+08:00")["status"], "OK")
        ledger.apply(material("r-ct-4", "ct_scan", "2026-09-22T10:50:00+08:00",
                              digest="sha256:d", channel="ESB"))
        self.assertEqual(
            ledger.item_status("ref-1", "ct_scan", "2026-09-22T10:55:00+08:00")["status"],
            "CONFLICTED")


class ReplanTest(unittest.TestCase):
    """授权、床位窗口或急诊级别变化后，系统重新给出可执行方案。"""

    def test_condition_changes_recompute_plan(self):
        ledger = Ledger()
        problems = ledger.apply_all([
            evt("SHIFT_OPENED", "shift-A", T0, shift_id="shift-A"),
            publish(),
            bind(conditions={
                "bed_window": {"start": "2026-09-22T20:00:00+08:00",
                               "end": "2026-09-23T06:00:00+08:00"},
                "emergency_level": "URGENT",
                "patient_consent": "ACTIVE",
            }),
            material("r-auth", "patient_auth", "2026-09-22T09:10:00+08:00"),
            material("r-ct", "ct_scan", "2026-09-22T09:20:00+08:00"),
            material("r-lab", "labs", "2026-09-22T09:30:00+08:00"),
            material("r-sum", "transfer_summary", "2026-09-22T09:40:00+08:00"),
            material("r-tr", "transport_record", "2026-09-22T09:50:00+08:00"),
        ])
        self.assertEqual(problems, {})
        plan = ledger.compute_plan("ref-1", "2026-09-23T05:00:00+08:00")
        self.assertIsNone(plan["first_blocked_step"])
        plan = ledger.compute_plan("ref-1", "2026-09-23T07:00:00+08:00")
        bed = plan_step(ledger, "ref-1", "BED_RESERVATION", "2026-09-23T07:00:00+08:00")
        self.assertEqual(bed["state"], "BLOCKED")
        self.assertEqual(bed["reasons"][0]["issue"], "BED_WINDOW_CLOSED")
        self.assertEqual(plan_step(ledger, "ref-1", "TRANSPORT_DISPATCH",
                                   "2026-09-23T07:00:00+08:00")["state"], "READY")
        ledger.apply(evt("REFERRAL_CONDITION_CHANGED", "ref-1", "2026-09-23T07:05:00+08:00",
                         changes={"bed_window": {"start": "2026-09-22T20:00:00+08:00",
                                                 "end": "2026-09-23T12:00:00+08:00"},
                                  "emergency_level": "STAT"},
                         reason="床位窗口顺延，急诊级别上调"))
        plan = ledger.compute_plan("ref-1", "2026-09-23T07:10:00+08:00")
        self.assertIsNone(plan["first_blocked_step"])
        self.assertEqual(plan["emergency_level"], "STAT")
        ledger.apply(evt("REFERRAL_CONDITION_CHANGED", "ref-1", "2026-09-23T07:20:00+08:00",
                         changes={"patient_consent": "WITHDRAWN"}, reason="患者撤回知情授权"))
        plan = ledger.compute_plan("ref-1", "2026-09-23T07:25:00+08:00")
        self.assertEqual(plan["executable"], [])
        self.assertTrue(all(
            any(r.get("issue") == "CONSENT_WITHDRAWN" for r in s["reasons"])
            for s in plan["steps"]))


class WaiverTest(unittest.TestCase):
    """人工放行只能针对明确缺项且到期自动收回。"""

    def _ledger_missing_transport(self):
        ledger = base_ledger()
        ledger.apply_all([
            material("r-auth", "patient_auth", "2026-09-22T09:10:00+08:00"),
            material("r-ct", "ct_scan", "2026-09-22T09:20:00+08:00"),
            material("r-lab", "labs", "2026-09-22T09:30:00+08:00"),
            material("r-sum", "transfer_summary", "2026-09-22T09:40:00+08:00"),
        ])
        return ledger

    def test_waiver_covers_missing_item_and_auto_expires(self):
        ledger = self._ledger_missing_transport()
        self.assertEqual(
            plan_step(ledger, "ref-1", "CLINICAL_INTAKE", "2026-09-22T23:00:00+08:00")["state"],
            "BLOCKED")
        self.assertEqual(ledger.apply(evt(
            "WAIVER_GRANTED", "ref-1", "2026-09-22T23:05:00+08:00",
            waiver_id="wvr-1", item_keys=["transport_record"], granted_by="协调员甲",
            reason="救护车已在途，交接单随车后补",
            expires_at="2026-09-23T02:00:00+08:00")), [])
        self.assertEqual(
            ledger.item_status("ref-1", "transport_record", "2026-09-22T23:10:00+08:00")["status"],
            "WAIVED")
        self.assertEqual(
            plan_step(ledger, "ref-1", "CLINICAL_INTAKE", "2026-09-22T23:10:00+08:00")["state"],
            "READY")
        # 到期自动收回：不需要任何新事件
        self.assertEqual(
            ledger.item_status("ref-1", "transport_record", "2026-09-23T02:30:00+08:00")["status"],
            "MISSING")
        self.assertEqual(
            plan_step(ledger, "ref-1", "CLINICAL_INTAKE", "2026-09-23T02:30:00+08:00")["state"],
            "BLOCKED")

    def test_waiver_only_for_explicit_missing_items(self):
        ledger = base_ledger()
        ledger.apply(material("r-lab", "labs", "2026-09-22T10:00:00+08:00"))
        self.assertEqual(ledger.apply(evt(
            "WAIVER_GRANTED", "ref-1", "2026-09-22T10:05:00+08:00",
            waiver_id="wvr-ok", item_keys=["labs"], granted_by="协调员甲",
            reason="x", expires_at="2026-09-22T12:00:00+08:00")), ["item_keys"])
        self.assertEqual(ledger.apply(evt(
            "WAIVER_GRANTED", "ref-1", "2026-09-22T10:05:00+08:00",
            waiver_id="wvr-free", item_keys=["receiving_confirm"], granted_by="协调员甲",
            reason="x", expires_at="2026-09-22T12:00:00+08:00")), ["item_keys"])
        self.assertEqual(ledger.apply(evt(
            "WAIVER_GRANTED", "ref-1", "2026-09-22T10:05:00+08:00",
            waiver_id="wvr-past", item_keys=["transport_record"], granted_by="协调员甲",
            reason="x", expires_at="2026-09-22T09:00:00+08:00")), ["expires_at"])

    def test_manual_revoke(self):
        ledger = base_ledger()
        ledger.apply(evt("WAIVER_GRANTED", "ref-1", "2026-09-22T10:05:00+08:00",
                         waiver_id="wvr-1", item_keys=["transport_record"],
                         granted_by="协调员甲", reason="先放行",
                         expires_at="2026-09-23T02:00:00+08:00"))
        self.assertEqual(
            ledger.item_status("ref-1", "transport_record", "2026-09-22T10:10:00+08:00")["status"],
            "WAIVED")
        self.assertEqual(ledger.apply(evt(
            "WAIVER_REVOKED", "ref-1", "2026-09-22T11:00:00+08:00",
            waiver_id="wvr-1", cause="MANUAL")), [])
        self.assertEqual(
            ledger.item_status("ref-1", "transport_record", "2026-09-22T11:05:00+08:00")["status"],
            "MISSING")
        self.assertEqual(ledger.apply(evt(
            "WAIVER_REVOKED", "ref-1", "2026-09-22T11:10:00+08:00",
            waiver_id="wvr-1", cause="MANUAL")), ["waiver_id"])


class HandoverTest(unittest.TestCase):
    """跨班人员在交接页直接接管未完成责任。"""

    def test_handover_transfers_open_responsibilities(self):
        ledger = base_ledger()
        key = "ref-1:transport_record:MISSING"
        before = ledger.open_responsibilities("2026-09-22T23:50:00+08:00")
        owners = {r["issue_key"]: r["owner_shift"] for r in before}
        self.assertEqual(owners[key], "shift-A")
        ledger.apply(evt("SHIFT_OPENED", "shift-B", "2026-09-23T00:00:00+08:00",
                         shift_id="shift-B"))
        self.assertEqual(ledger.apply(evt(
            "HANDOVER_ACCEPTED", "shift-B", "2026-09-23T00:05:00+08:00",
            shift_id="shift-B", accepted_by="护士乙", taken_over=[key])), [])
        after = ledger.open_responsibilities("2026-09-23T00:10:00+08:00")
        entry = next(r for r in after if r["issue_key"] == key)
        self.assertEqual(entry["owner_shift"], "shift-B")
        self.assertEqual(entry["since"], "2026-09-22T09:00:00+08:00")

    def test_handover_rejects_unknown_shift_or_closed_issue(self):
        ledger = base_ledger()
        ledger.apply(evt("SHIFT_OPENED", "shift-B", "2026-09-23T00:00:00+08:00",
                         shift_id="shift-B"))
        self.assertEqual(ledger.apply(evt(
            "HANDOVER_ACCEPTED", "shift-B", "2026-09-23T00:05:00+08:00",
            shift_id="shift-B", accepted_by="护士乙",
            taken_over=["ref-1:nothing:MISSING"])), ["taken_over"])
        self.assertEqual(ledger.apply(evt(
            "HANDOVER_ACCEPTED", "shift-C", "2026-09-23T00:06:00+08:00",
            shift_id="shift-C", accepted_by="护士丙", taken_over=[])), ["shift_id"])


class PatientViewTest(unittest.TestCase):
    """患者看到一致的缺件和下一步。"""

    def test_patient_view_matches_internal_open_issues(self):
        ledger = base_ledger()
        ledger.apply(material("r-lab", "labs", "2026-09-22T10:00:00+08:00", sealed=False))
        now = "2026-09-22T10:30:00+08:00"
        view = ledger.patient_view("ref-1", now)
        internal = {i["item_key"] for i in ledger.open_issues(now, referral_id="ref-1")}
        self.assertEqual({m["item_key"] for m in view["missing"]}, internal)
        self.assertEqual(view["missing"][0]["item_key"], "patient_auth")
        self.assertIn("补交", view["next_step"])

    def test_next_step_text_follows_first_issue(self):
        ledger = base_ledger()
        ledger.apply_all([
            material("r-auth", "patient_auth", "2026-09-22T09:10:00+08:00"),
            material("r-ct", "ct_scan", "2026-09-22T09:20:00+08:00"),
            material("r-sum", "transfer_summary", "2026-09-22T09:40:00+08:00"),
            material("r-tr", "transport_record", "2026-09-22T09:50:00+08:00"),
            material("r-lab", "labs", "2026-09-22T10:00:00+08:00", sealed=False),
        ])
        view = ledger.patient_view("ref-1", "2026-09-22T10:30:00+08:00")
        self.assertEqual(view["missing"][0]["issue"], "SEAL_MISSING")
        self.assertIn("签章", view["next_step"])


class ExplainBlockerTest(unittest.TestCase):
    """协调员从阻塞点反查清单版本、材料回执及限时放行依据。"""

    def test_trace_includes_version_receipts_reads_and_waiver(self):
        ledger = base_ledger()
        ledger.apply_all([
            material("r-auth", "patient_auth", "2026-09-22T09:10:00+08:00"),
            material("r-ct", "ct_scan", "2026-09-22T09:20:00+08:00"),
            material("r-sum", "transfer_summary", "2026-09-22T09:40:00+08:00"),
            material("r-tr", "transport_record", "2026-09-22T09:50:00+08:00"),
            material("r-lab-1", "labs", "2026-09-22T10:00:00+08:00", sealed=False),
            evt("MATERIAL_READ_RECORDED", "ref-1", "2026-09-22T10:10:00+08:00",
                receipt_id="r-lab-1", reader_role="接收护士"),
            evt("WAIVER_GRANTED", "ref-1", "2026-09-22T10:20:00+08:00",
                waiver_id="wvr-1", item_keys=["labs"], granted_by="协调员甲",
                reason="正式报告随车后补", expires_at="2026-09-22T12:00:00+08:00"),
        ])
        trace = ledger.explain_blocker("ref-1", "CLINICAL_INTAKE", "2026-09-22T10:30:00+08:00")
        self.assertEqual(trace["checklist_version"], "2026.09")
        self.assertEqual(trace["state"], "READY")
        reason = next(r for r in trace["reasons"] if r.get("item_key") == "labs")
        self.assertEqual(reason["issue"], "WAIVED")
        self.assertEqual(reason["receipts"][0]["receipt_id"], "r-lab-1")
        self.assertEqual(reason["receipts"][0]["read_by"], ["接收护士"])
        self.assertEqual(reason["waivers"][0]["waiver_id"], "wvr-1")
        self.assertTrue(reason["waivers"][0]["active"])
        self.assertEqual(reason["waivers"][0]["expires_at"], "2026-09-22T12:00:00+08:00")
        trace = ledger.explain_blocker("ref-1", "CLINICAL_INTAKE", "2026-09-22T13:00:00+08:00")
        self.assertEqual(trace["state"], "BLOCKED")
        reason = next(r for r in trace["reasons"] if r.get("item_key") == "labs")
        self.assertEqual(reason["issue"], "SEAL_MISSING")
        self.assertFalse(reason["waivers"][0]["active"])


class CrossMidnightReminderTest(unittest.TestCase):
    """跨午夜运输也不会重复催办。"""

    def test_reminder_not_duplicated_across_midnight_and_shifts(self):
        ledger = base_ledger()
        ledger.apply_all([
            material("r-auth", "patient_auth", "2026-09-22T09:10:00+08:00"),
            material("r-ct", "ct_scan", "2026-09-22T09:20:00+08:00"),
            material("r-lab", "labs", "2026-09-22T09:30:00+08:00"),
            material("r-sum", "transfer_summary", "2026-09-22T09:40:00+08:00"),
        ])
        key = "ref-1:transport_record:MISSING"
        late = "2026-09-22T23:55:00+08:00"
        first = ledger.pending_reminders(late)
        self.assertEqual([r["issue_key"] for r in first], [key])
        sent = {r["issue_key"] for r in first}
        ledger.apply(evt("SHIFT_OPENED", "shift-B", "2026-09-23T00:00:00+08:00",
                         shift_id="shift-B"))
        after_midnight = "2026-09-23T00:10:00+08:00"
        self.assertEqual(ledger.pending_reminders(after_midnight, sent), [])
        self.assertEqual(
            [r["issue_key"] for r in ledger.pending_reminders(after_midnight)], [key])
        ledger.apply(evt("HANDOVER_ACCEPTED", "shift-B", "2026-09-23T00:15:00+08:00",
                         shift_id="shift-B", accepted_by="护士乙", taken_over=[key]))
        self.assertEqual(ledger.pending_reminders("2026-09-23T00:20:00+08:00", sent), [])


class SubstituteTest(unittest.TestCase):
    """替代材料关联到一次转诊的对应条目。"""

    def test_substitute_satisfies_item_that_accepts_it(self):
        ledger = base_ledger()
        sub = material("r-sub-1", "bedside_echo", "2026-09-22T10:00:00+08:00",
                       category="SUBSTITUTE", substitutes_for="ct_scan")
        self.assertEqual(ledger.apply(sub), [])
        status = ledger.item_status("ref-1", "ct_scan", "2026-09-22T10:05:00+08:00")
        self.assertEqual(status["status"], "OK")

    def test_substitute_needs_seal_when_target_requires_it(self):
        ledger = base_ledger()
        sub = material("r-sub-1", "bedside_echo", "2026-09-22T10:00:00+08:00",
                       category="SUBSTITUTE", substitutes_for="ct_scan", sealed=False)
        ledger.apply(sub)
        self.assertEqual(
            ledger.item_status("ref-1", "ct_scan", "2026-09-22T10:05:00+08:00")["status"],
            "SEAL_MISSING")

    def test_substitute_rejected_when_item_does_not_accept(self):
        ledger = base_ledger()
        sub = material("r-sub-1", "rapid_lab", "2026-09-22T10:00:00+08:00",
                       category="SUBSTITUTE", substitutes_for="labs")
        self.assertEqual(ledger.apply(sub), ["substitutes_for"])


class SampleStreamReplayTest(unittest.TestCase):
    """重放脱敏样例事件流，核对关键时间点的投影。"""

    @classmethod
    def setUpClass(cls):
        cls.ledger = Ledger()
        events = [
            json.loads(line)
            for line in SAMPLE.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        problems = cls.ledger.apply_all(events)
        assert not problems, problems

    def test_before_midnight_transport_record_is_the_only_reminder(self):
        now = "2026-09-22T23:55:00+08:00"
        reminders = self.ledger.pending_reminders(now)
        self.assertEqual([r["issue_key"] for r in reminders],
                         ["ref-101:transport_record:MISSING"])
        self.assertEqual(
            plan_step(self.ledger, "ref-101", "CLINICAL_INTAKE", now)["state"], "BLOCKED")
        labs = self.ledger.item_status("ref-101", "labs", now)
        self.assertEqual(labs["status"], "OK")
        self.assertTrue(labs["correction_unread"])
        view = self.ledger.patient_view("ref-101", now)
        internal = {i["item_key"] for i in self.ledger.open_issues(now, referral_id="ref-101")}
        self.assertEqual({m["item_key"] for m in view["missing"]}, internal)

    def test_cross_midnight_no_duplicate_reminder_and_handover_owner(self):
        key = "ref-101:transport_record:MISSING"
        now = "2026-09-23T00:10:00+08:00"
        self.assertEqual(self.ledger.pending_reminders(now, {key}), [])
        owners = {r["issue_key"]: r["owner_shift"]
                  for r in self.ledger.open_responsibilities(now)}
        self.assertEqual(owners[key], "shift-B")

    def test_waiver_covers_gap_and_is_traceable(self):
        now = "2026-09-23T00:30:00+08:00"
        self.assertEqual(
            self.ledger.item_status("ref-101", "transport_record", now)["status"], "WAIVED")
        self.assertEqual(
            plan_step(self.ledger, "ref-101", "CLINICAL_INTAKE", now)["state"], "READY")
        trace = self.ledger.explain_blocker("ref-101", "CLINICAL_INTAKE", now)
        reason = next(r for r in trace["reasons"] if r.get("item_key") == "transport_record")
        self.assertEqual(reason["checklist_version"], "2026.09")
        self.assertEqual(reason["waivers"][0]["waiver_id"], "wvr-101-1")
        self.assertTrue(reason["waivers"][0]["active"])

    def test_conflict_pauses_only_dependent_steps(self):
        now = "2026-09-23T01:05:00+08:00"
        self.assertEqual(
            self.ledger.item_status("ref-101", "ct_scan", now)["status"], "CONFLICTED")
        plan = self.ledger.compute_plan("ref-101", now)
        states = {s["step"]: s["state"] for s in plan["steps"]}
        self.assertEqual(states["TRANSPORT_DISPATCH"], "PAUSED")
        self.assertEqual(states["CLINICAL_INTAKE"], "PAUSED")
        self.assertEqual(states["BED_RESERVATION"], "READY")

    def test_after_resolution_and_intake_all_ready_on_pinned_version(self):
        now = "2026-09-23T03:00:00+08:00"
        plan = self.ledger.compute_plan("ref-101", now)
        self.assertIsNone(plan["first_blocked_step"])
        self.assertEqual(plan["checklist_version"], "2026.09")
        self.assertEqual(plan["emergency_level"], "STAT")
        statuses = self.ledger.item_statuses("ref-101", now)
        self.assertNotIn("pathogen_screen", statuses)
        self.assertFalse(statuses["labs"]["correction_unread"])
        self.assertEqual(self.ledger.pending_reminders(now), [])

    def test_auth_expiry_and_bed_window_close_recompute_plan(self):
        now = "2026-09-23T09:30:00+08:00"
        self.assertEqual(
            self.ledger.item_status("ref-101", "patient_auth", now)["status"], "AUTH_EXPIRED")
        plan = self.ledger.compute_plan("ref-101", now)
        states = {s["step"]: s["state"] for s in plan["steps"]}
        self.assertEqual(states["TRANSPORT_DISPATCH"], "BLOCKED")
        self.assertEqual(states["CLINICAL_INTAKE"], "BLOCKED")
        self.assertEqual(states["BED_RESERVATION"], "BLOCKED")
        view = self.ledger.patient_view("ref-101", now)
        self.assertEqual(view["missing"][0]["item_key"], "patient_auth")
        self.assertIn("重新签署知情授权", view["next_step"])


if __name__ == "__main__":
    unittest.main()
