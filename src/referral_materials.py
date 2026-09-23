"""转诊资料到件闭环的领域约定与纯 Python 投影。

在 ``referral_nurse_roster`` 的班次/交班事件之上补充材料到件闭环：

- 按转诊类型维护、带生效期的材料清单版本；转诊绑定即定版，清单升级不影响在途转诊；
- 材料回执把患者授权、来源签章、检查状态、运输交接、接收确认与替代材料关联到一次转诊；
- 更正留旧版、已读有记录；同摘要的多渠道回执归并；内容冲突只暂停依赖它的环节；
- 授权、床位窗口、急诊级别变化后随时重算可执行方案；
- 人工放行只针对明确缺项、到期自动收回；
- 交接、患者视图、提醒去重与阻塞点反查都由同一事件流投影得出。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

REQUIRED_FIELDS = ("event_id", "kind", "occurred_at", "subject_id", "payload")

# 准备环节：发车、床位、临床接诊。清单条目用 gates 声明自己卡住哪些环节。
STEPS = ("TRANSPORT_DISPATCH", "BED_RESERVATION", "CLINICAL_INTAKE")

CATEGORIES = (
    "PATIENT_AUTHORIZATION",
    "EXAM_RESULT",
    "CLINICAL_DOCUMENT",
    "TRANSPORT_HANDOVER",
    "RECEIVING_CONFIRMATION",
    "SUBSTITUTE",
)

# 清单条目只能是实体材料；SUBSTITUTE 只出现在回执上，指向被替代的条目。
TEMPLATE_CATEGORIES = tuple(c for c in CATEGORIES if c != "SUBSTITUTE")

EXAM_STATES = ("UPLOADING", "COMPLETE")

# 明确缺项（可限时放行）与内容冲突（只能裁定，不可放行）分开。
DEFICIENCY_ISSUES = ("MISSING", "SEAL_MISSING", "AUTH_EXPIRED", "EXAM_INCOMPLETE")
CONFLICT_ISSUE = "CONFLICTED"
OPEN_ISSUES = DEFICIENCY_ISSUES + (CONFLICT_ISSUE,)

EVENT_KINDS = (
    "SHIFT_OPENED",
    "CHECKLIST_VERSION_PUBLISHED",
    "REFERRAL_CHECKLIST_BOUND",
    "REFERRAL_CHECKLIST_REBOUND",
    "MATERIAL_RECEIVED",
    "MATERIAL_READ_RECORDED",
    "MATERIAL_CONFLICT_RESOLVED",
    "WAIVER_GRANTED",
    "WAIVER_REVOKED",
    "REFERRAL_CONDITION_CHANGED",
    "HANDOVER_ACCEPTED",
)

CONDITION_KEYS = ("bed_window", "emergency_level", "patient_consent")

NEXT_STEP_TEXT = {
    "MISSING": "请补交缺失材料",
    "SEAL_MISSING": "等待来源机构补盖签章",
    "AUTH_EXPIRED": "请重新签署知情授权",
    "EXAM_INCOMPLETE": "等待检查影像上传完成",
    "CONFLICTED": "等待协调员核对冲突材料",
}


def _parse_ts(value):
    """解析带时区的 ISO 时间；不合法返回 None。"""
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed


def _as_dt(value):
    if isinstance(value, datetime):
        return value
    parsed = _parse_ts(value)
    if parsed is None:
        raise ValueError(f"时间格式不合法: {value!r}")
    return parsed


def _is_text(value):
    return isinstance(value, str) and bool(value.strip())


def _missing(payload, keys):
    return [key for key in keys if key not in payload]


def _check_items(items):
    """清单条目：键唯一、类别合法、环节名合法、布尔标记合法。"""
    if not isinstance(items, list) or not items:
        return ["items"]
    seen = set()
    for entry in items:
        if not isinstance(entry, dict) or not _is_text(entry.get("item_key")):
            return ["items"]
        if entry["item_key"] in seen:
            return ["items"]
        seen.add(entry["item_key"])
        if entry.get("category") not in TEMPLATE_CATEGORIES:
            return ["items"]
        gates = entry.get("gates", [])
        if not isinstance(gates, list) or any(g not in STEPS for g in gates):
            return ["items"]
        for flag in ("requires_seal", "accepts_substitute"):
            if flag in entry and not isinstance(entry[flag], bool):
                return ["items"]
    return []


def _check_conditions(changes, allow_empty=False):
    if not isinstance(changes, dict) or (not changes and not allow_empty):
        return ["changes"]
    for key, value in changes.items():
        if key not in CONDITION_KEYS:
            return ["changes"]
        if key == "bed_window":
            if not isinstance(value, dict):
                return ["changes"]
            start, end = _parse_ts(value.get("start")), _parse_ts(value.get("end"))
            if start is None or end is None or not start < end:
                return ["changes"]
        elif key == "patient_consent":
            if value not in ("ACTIVE", "WITHDRAWN"):
                return ["changes"]
        elif not _is_text(value):
            return ["changes"]
    return []


def _check_checklist_payload(payload):
    problems = _missing(payload, ("referral_type", "version", "effective_from", "items"))
    if problems:
        return problems
    if not _is_text(payload["referral_type"]) or not _is_text(payload["version"]):
        return ["version"]
    start = _parse_ts(payload["effective_from"])
    if start is None:
        return ["effective_from"]
    effective_to = payload.get("effective_to")
    if effective_to is not None:
        parsed = _parse_ts(effective_to)
        if parsed is None or parsed <= start:
            return ["effective_to"]
    return _check_items(payload["items"])


def _check_bind_payload(payload):
    problems = _missing(payload, ("referral_type", "checklist_version"))
    if problems:
        return problems
    if not _is_text(payload["referral_type"]) or not _is_text(payload["checklist_version"]):
        return ["checklist_version"]
    conditions = payload.get("conditions")
    if conditions is not None:
        return _check_conditions(conditions, allow_empty=True)
    return []


def _check_rebind_payload(payload):
    problems = _missing(payload, ("from_version", "to_version", "reason"))
    if problems:
        return problems
    if not _is_text(payload["from_version"]) or not _is_text(payload["to_version"]):
        return ["to_version"]
    if not _is_text(payload["reason"]):
        return ["reason"]
    return []


def _check_material_payload(payload):
    problems = _missing(payload, ("receipt_id", "item_key", "category", "channel", "digest", "sealed"))
    if problems:
        return problems
    for key in ("receipt_id", "item_key", "channel", "digest"):
        if not _is_text(payload[key]):
            return [key]
    if payload["category"] not in CATEGORIES:
        return ["category"]
    if not isinstance(payload["sealed"], bool):
        return ["sealed"]
    if payload["category"] == "SUBSTITUTE":
        if not _is_text(payload.get("substitutes_for")):
            return ["substitutes_for"]
    elif "substitutes_for" in payload:
        return ["substitutes_for"]
    if payload["category"] == "PATIENT_AUTHORIZATION" and _parse_ts(payload.get("auth_expires_at")) is None:
        return ["auth_expires_at"]
    if payload["category"] == "EXAM_RESULT" and payload.get("exam_state") not in EXAM_STATES:
        return ["exam_state"]
    if "supersedes" in payload and not _is_text(payload["supersedes"]):
        return ["supersedes"]
    return []


def _check_read_payload(payload):
    problems = _missing(payload, ("receipt_id", "reader_role"))
    if problems:
        return problems
    if not _is_text(payload["receipt_id"]):
        return ["receipt_id"]
    if not _is_text(payload["reader_role"]):
        return ["reader_role"]
    return []


def _check_resolution_payload(payload):
    problems = _missing(payload, ("item_key", "winning_receipt_id", "resolved_by", "reason"))
    if problems:
        return problems
    for key in ("item_key", "winning_receipt_id", "resolved_by", "reason"):
        if not _is_text(payload[key]):
            return [key]
    return []


def _check_waiver_payload(payload):
    problems = _missing(payload, ("waiver_id", "item_keys", "granted_by", "reason", "expires_at"))
    if problems:
        return problems
    if not _is_text(payload["waiver_id"]):
        return ["waiver_id"]
    keys = payload["item_keys"]
    if not isinstance(keys, list) or not keys or any(not _is_text(k) for k in keys):
        return ["item_keys"]
    if not _is_text(payload["granted_by"]) or not _is_text(payload["reason"]):
        return ["reason"]
    if _parse_ts(payload["expires_at"]) is None:
        return ["expires_at"]
    return []


def _check_revoke_payload(payload):
    problems = _missing(payload, ("waiver_id", "cause"))
    if problems:
        return problems
    if not _is_text(payload["waiver_id"]):
        return ["waiver_id"]
    if payload["cause"] not in ("MANUAL", "EXPIRED"):
        return ["cause"]
    return []


def _check_condition_payload(payload):
    problems = _missing(payload, ("changes", "reason"))
    if problems:
        return problems
    if not _is_text(payload["reason"]):
        return ["reason"]
    return _check_conditions(payload["changes"])


def _check_handover_payload(payload):
    problems = _missing(payload, ("shift_id", "accepted_by", "taken_over"))
    if problems:
        return problems
    if not _is_text(payload["shift_id"]):
        return ["shift_id"]
    if not _is_text(payload["accepted_by"]):
        return ["accepted_by"]
    taken = payload["taken_over"]
    if not isinstance(taken, list) or any(not _is_text(k) for k in taken):
        return ["taken_over"]
    return []


def _check_shift_payload(payload):
    if not _is_text(payload.get("shift_id")):
        return ["shift_id"]
    return []


_PAYLOAD_CHECKS = {
    "SHIFT_OPENED": _check_shift_payload,
    "CHECKLIST_VERSION_PUBLISHED": _check_checklist_payload,
    "REFERRAL_CHECKLIST_BOUND": _check_bind_payload,
    "REFERRAL_CHECKLIST_REBOUND": _check_rebind_payload,
    "MATERIAL_RECEIVED": _check_material_payload,
    "MATERIAL_READ_RECORDED": _check_read_payload,
    "MATERIAL_CONFLICT_RESOLVED": _check_resolution_payload,
    "WAIVER_GRANTED": _check_waiver_payload,
    "WAIVER_REVOKED": _check_revoke_payload,
    "REFERRAL_CONDITION_CHANGED": _check_condition_payload,
    "HANDOVER_ACCEPTED": _check_handover_payload,
}


def validate_event(record: dict) -> list[str]:
    """检查事件是否具备可交换的最小字段与对应 kind 的载荷约定。"""
    problems = _missing(record, REQUIRED_FIELDS)
    if problems:
        return problems
    if record["kind"] not in EVENT_KINDS:
        return ["kind"]
    if _parse_ts(record["occurred_at"]) is None:
        return ["occurred_at"]
    payload = record["payload"]
    if not isinstance(payload, dict):
        return ["payload"]
    return _PAYLOAD_CHECKS[record["kind"]](payload)


@dataclass
class Receipt:
    """一次材料到件回执；更正通过 supersedes 串成链，旧版不删除。"""

    receipt_id: str
    referral_id: str
    item_key: str
    target_item_key: str
    category: str
    channel: str
    digest: str
    sealed: bool
    auth_expires_at: str | None
    exam_state: str | None
    supersedes: str | None
    received_at: str


@dataclass
class ReferralState:
    referral_id: str
    referral_type: str
    checklist_version: str
    bound_at: str
    conditions: dict = field(default_factory=dict)
    receipts: list = field(default_factory=list)
    version_history: list = field(default_factory=list)
    condition_history: list = field(default_factory=list)


class Ledger:
    """按事件顺序重放，投影出转诊资料到件闭环的全部只读视图。

    所有评估都以调用方传入的 ``now`` 为准，因此限时放行到期、授权过期、
    床位窗口关闭都会随时间自动生效，无需额外事件。
    """

    def __init__(self):
        self.events = []
        self.checklists = {}
        self.referrals = {}
        self.receipts_by_id = {}
        self.reads = {}
        self.resolutions = {}
        self.waivers = {}
        self.shifts = {}
        self.current_shift = None
        self.issue_index = {}
        self.handovers = []

    # ---- 写入 ----------------------------------------------------------

    def apply(self, record):
        """校验并追加一个事件；返回问题列表，非空表示事件被拒绝且状态未变。"""
        problems = validate_event(record)
        if problems:
            return problems
        problems = self._check_state(record)
        if problems:
            return problems
        self._mutate(record)
        self._refresh_issues(record["occurred_at"])
        return []

    def apply_all(self, records):
        problems = {}
        for record in records:
            found = self.apply(record)
            if found:
                problems[record.get("event_id")] = found
        return problems

    def _check_state(self, record):
        kind = record["kind"]
        payload = record["payload"]
        subject = record["subject_id"]
        at = record["occurred_at"]
        if kind == "SHIFT_OPENED":
            return []
        if kind == "CHECKLIST_VERSION_PUBLISHED":
            key = (payload["referral_type"], payload["version"])
            return ["version"] if key in self.checklists else []
        if kind == "REFERRAL_CHECKLIST_BOUND":
            if subject in self.referrals:
                return ["subject_id"]
            template = self.checklists.get((payload["referral_type"], payload["checklist_version"]))
            if template is None:
                return ["checklist_version"]
            if not self._effective(template, at):
                return ["effective_from"]
            return []
        if kind == "REFERRAL_CHECKLIST_REBOUND":
            ref = self.referrals.get(subject)
            if ref is None:
                return ["subject_id"]
            if payload["from_version"] != ref.checklist_version:
                return ["from_version"]
            template = self.checklists.get((ref.referral_type, payload["to_version"]))
            if template is None or not self._effective(template, at):
                return ["to_version"]
            return []
        if kind == "MATERIAL_RECEIVED":
            return self._check_material_state(subject, payload)
        if kind == "MATERIAL_READ_RECORDED":
            receipt = self.receipts_by_id.get(payload["receipt_id"])
            if receipt is None or receipt.referral_id != subject:
                return ["receipt_id"]
            return []
        if kind == "MATERIAL_CONFLICT_RESOLVED":
            ref = self.referrals.get(subject)
            if ref is None:
                return ["subject_id"]
            try:
                status = self.item_status(subject, payload["item_key"], at)
            except KeyError:
                return ["item_key"]
            if status["status"] != CONFLICT_ISSUE:
                return ["item_key"]
            _, current = self._receipts_for(ref, payload["item_key"], _as_dt(at))
            if payload["winning_receipt_id"] not in {r.receipt_id for r in current}:
                return ["winning_receipt_id"]
            return []
        if kind == "WAIVER_GRANTED":
            return self._check_waiver_state(subject, payload, at)
        if kind == "WAIVER_REVOKED":
            waiver = self.waivers.get(payload["waiver_id"])
            if waiver is None or waiver["referral_id"] != subject or waiver["revoked_at"] is not None:
                return ["waiver_id"]
            return []
        if kind == "REFERRAL_CONDITION_CHANGED":
            return [] if subject in self.referrals else ["subject_id"]
        if kind == "HANDOVER_ACCEPTED":
            if payload["shift_id"] not in self.shifts:
                return ["shift_id"]
            open_keys = {issue["issue_key"] for issue in self.open_issues(at)}
            if any(key not in open_keys for key in payload["taken_over"]):
                return ["taken_over"]
            return []
        return []

    def _check_material_state(self, subject, payload):
        ref = self.referrals.get(subject)
        if ref is None:
            return ["subject_id"]
        if payload["receipt_id"] in self.receipts_by_id:
            return ["receipt_id"]
        target_key = payload.get("substitutes_for") or payload["item_key"]
        try:
            item = self._template_item(ref, target_key)
        except KeyError:
            return ["item_key"]
        if payload["category"] == "SUBSTITUTE":
            if not item.get("accepts_substitute"):
                return ["substitutes_for"]
        elif payload["category"] != item["category"]:
            return ["category"]
        supersedes = payload.get("supersedes")
        if supersedes is not None:
            old = self.receipts_by_id.get(supersedes)
            if old is None or old.referral_id != subject or old.target_item_key != target_key:
                return ["supersedes"]
        return []

    def _check_waiver_state(self, subject, payload, at):
        ref = self.referrals.get(subject)
        if ref is None:
            return ["subject_id"]
        if payload["waiver_id"] in self.waivers:
            return ["waiver_id"]
        if _parse_ts(payload["expires_at"]) <= _as_dt(at):
            return ["expires_at"]
        for item_key in payload["item_keys"]:
            try:
                item = self._template_item(ref, item_key)
            except KeyError:
                return ["item_keys"]
            if not item.get("gates"):
                return ["item_keys"]
            if self.item_status(subject, item_key, at)["status"] not in DEFICIENCY_ISSUES:
                return ["item_keys"]
        return []

    def _mutate(self, record):
        kind = record["kind"]
        payload = record["payload"]
        subject = record["subject_id"]
        at = record["occurred_at"]
        self.events.append(record)
        if kind == "SHIFT_OPENED":
            self.shifts[payload["shift_id"]] = at
            self.current_shift = payload["shift_id"]
        elif kind == "CHECKLIST_VERSION_PUBLISHED":
            self.checklists[(payload["referral_type"], payload["version"])] = {
                "referral_type": payload["referral_type"],
                "version": payload["version"],
                "effective_from": payload["effective_from"],
                "effective_to": payload.get("effective_to"),
                "items": [dict(item) for item in payload["items"]],
                "published_at": at,
            }
        elif kind == "REFERRAL_CHECKLIST_BOUND":
            conditions = dict(payload.get("conditions", {}))
            self.referrals[subject] = ReferralState(
                referral_id=subject,
                referral_type=payload["referral_type"],
                checklist_version=payload["checklist_version"],
                bound_at=at,
                conditions=conditions,
                version_history=[(at, payload["checklist_version"])],
                condition_history=[(at, conditions)],
            )
        elif kind == "REFERRAL_CHECKLIST_REBOUND":
            ref = self.referrals[subject]
            ref.checklist_version = payload["to_version"]
            ref.version_history.append((at, payload["to_version"]))
        elif kind == "MATERIAL_RECEIVED":
            receipt = Receipt(
                receipt_id=payload["receipt_id"],
                referral_id=subject,
                item_key=payload["item_key"],
                target_item_key=payload.get("substitutes_for") or payload["item_key"],
                category=payload["category"],
                channel=payload["channel"],
                digest=payload["digest"],
                sealed=payload["sealed"],
                auth_expires_at=payload.get("auth_expires_at"),
                exam_state=payload.get("exam_state"),
                supersedes=payload.get("supersedes"),
                received_at=at,
            )
            self.referrals[subject].receipts.append(receipt)
            self.receipts_by_id[receipt.receipt_id] = receipt
        elif kind == "MATERIAL_READ_RECORDED":
            self.reads.setdefault(payload["receipt_id"], []).append(
                {"reader_role": payload["reader_role"], "read_at": at}
            )
        elif kind == "MATERIAL_CONFLICT_RESOLVED":
            ref = self.referrals[subject]
            _, current = self._receipts_for(ref, payload["item_key"], _as_dt(at))
            winner = self.receipts_by_id[payload["winning_receipt_id"]]
            losing = sorted(r.receipt_id for r in current if r.digest != winner.digest)
            self.resolutions.setdefault((subject, payload["item_key"]), []).append({
                "winning_receipt_id": payload["winning_receipt_id"],
                "losing_receipt_ids": losing,
                "resolved_by": payload["resolved_by"],
                "reason": payload["reason"],
                "resolved_at": at,
            })
        elif kind == "WAIVER_GRANTED":
            self.waivers[payload["waiver_id"]] = {
                "waiver_id": payload["waiver_id"],
                "referral_id": subject,
                "item_keys": list(payload["item_keys"]),
                "granted_by": payload["granted_by"],
                "reason": payload["reason"],
                "granted_at": at,
                "expires_at": payload["expires_at"],
                "revoked_at": None,
            }
        elif kind == "WAIVER_REVOKED":
            self.waivers[payload["waiver_id"]]["revoked_at"] = at
        elif kind == "REFERRAL_CONDITION_CHANGED":
            ref = self.referrals[subject]
            ref.conditions.update(payload["changes"])
            ref.condition_history.append((at, dict(payload["changes"])))
        elif kind == "HANDOVER_ACCEPTED":
            for issue_key in payload["taken_over"]:
                self.issue_index[issue_key]["owner_shift"] = payload["shift_id"]
            self.handovers.append(record)

    # ---- 材料状态 ------------------------------------------------------

    @staticmethod
    def _effective(template, at):
        moment = _as_dt(at)
        if _parse_ts(template["effective_from"]) > moment:
            return False
        effective_to = template.get("effective_to")
        return effective_to is None or _parse_ts(effective_to) > moment

    def _template_item(self, ref, item_key, version=None):
        template = self.checklists[(ref.referral_type, version or ref.checklist_version)]
        for item in template["items"]:
            if item["item_key"] == item_key:
                return item
        raise KeyError(item_key)

    @staticmethod
    def _version_at(ref, now_dt):
        version = ref.version_history[0][1]
        for at, found in ref.version_history:
            if _as_dt(at) <= now_dt:
                version = found
        return version

    @staticmethod
    def _conditions_at(ref, now_dt):
        conditions = {}
        for at, changes in ref.condition_history:
            if _as_dt(at) <= now_dt:
                conditions.update(changes)
        return conditions

    def _receipts_for(self, ref, item_key, now_dt=None):
        """返回 (全部回执, 当前回执)。

        当前 = 未被更正链淘汰、且未被当时已生效的冲突裁定判负。裁定只排除
        当时在场的判负回执，之后到达的回执重新参与评估，可能形成新的冲突。
        传入 now_dt 时只看待定时刻之前到达的回执与裁定。
        """
        receipts = [r for r in ref.receipts if r.target_item_key == item_key]
        if now_dt is not None:
            receipts = [r for r in receipts if _as_dt(r.received_at) <= now_dt]
        superseded = {r.supersedes for r in receipts if r.supersedes}
        losing = set()
        for resolution in self.resolutions.get((ref.referral_id, item_key), []):
            if now_dt is not None and _as_dt(resolution["resolved_at"]) > now_dt:
                continue
            losing.update(resolution["losing_receipt_ids"])
        current = [
            r for r in receipts
            if r.receipt_id not in superseded and r.receipt_id not in losing
        ]
        return receipts, current

    @staticmethod
    def _assess(item, receipt, now_dt):
        if receipt.category != "SUBSTITUTE":
            if item["category"] == "EXAM_RESULT" and receipt.exam_state != "COMPLETE":
                return "EXAM_INCOMPLETE"
            if item["category"] == "PATIENT_AUTHORIZATION":
                expires = _parse_ts(receipt.auth_expires_at)
                if expires is not None and expires <= now_dt:
                    return "AUTH_EXPIRED"
        if item.get("requires_seal") and not receipt.sealed:
            return "SEAL_MISSING"
        return "OK"

    def _active_waiver(self, referral_id, item_key, now_dt):
        for record in self.waivers.values():
            if (
                record["referral_id"] == referral_id
                and item_key in record["item_keys"]
                and _as_dt(record["granted_at"]) <= now_dt
                and (record["revoked_at"] is None or _as_dt(record["revoked_at"]) > now_dt)
                and _parse_ts(record["expires_at"]) > now_dt
            ):
                return {
                    "waiver_id": record["waiver_id"],
                    "granted_by": record["granted_by"],
                    "reason": record["reason"],
                    "expires_at": record["expires_at"],
                }
        return None

    def item_status(self, referral_id, item_key, now):
        """单个清单条目在 now 时刻的状态。

        同摘要的多渠道回执归并为一份材料；不同摘要且未经更正链串起来的
        回执构成冲突；冲突只能裁定，不能被放行覆盖。
        """
        now_dt = _as_dt(now)
        ref = self.referrals[referral_id]
        version = self._version_at(ref, now_dt)
        item = self._template_item(ref, item_key, version)
        receipts, current = self._receipts_for(ref, item_key, now_dt)
        digests = {r.digest for r in current}
        if len(digests) > 1:
            status = CONFLICT_ISSUE
        elif not current:
            status = "MISSING"
        else:
            latest = max(current, key=lambda r: (_as_dt(r.received_at), r.receipt_id))
            status = self._assess(item, latest, now_dt)
        waiver = self._active_waiver(referral_id, item_key, now_dt)
        if status in DEFICIENCY_ISSUES and waiver is not None:
            status = "WAIVED"
        current_ids = {r.receipt_id for r in current}
        read_by = sorted(
            {
                rd["reader_role"]
                for rid in current_ids
                for rd in self.reads.get(rid, [])
                if _as_dt(rd["read_at"]) <= now_dt
            }
        )
        history = sorted(r.receipt_id for r in receipts if r.receipt_id not in current_ids)
        return {
            "item_key": item_key,
            "label": item.get("label", item_key),
            "status": status,
            "digest": next(iter(digests)) if len(digests) == 1 else None,
            "channels": sorted({r.channel for r in current}),
            "receipt_ids": sorted(current_ids),
            "history": history,
            "read_by": read_by,
            "correction_unread": bool(history) and not read_by,
            "waiver": waiver,
        }

    def item_statuses(self, referral_id, now):
        now_dt = _as_dt(now)
        ref = self.referrals[referral_id]
        template = self.checklists[(ref.referral_type, self._version_at(ref, now_dt))]
        return {
            item["item_key"]: self.item_status(referral_id, item["item_key"], now)
            for item in template["items"]
        }

    # ---- 方案与视图 ----------------------------------------------------

    def compute_plan(self, referral_id, now):
        """按当前状态重算可执行方案；条件变化后调用即得新方案。"""
        now_dt = _as_dt(now)
        ref = self.referrals[referral_id]
        version = self._version_at(ref, now_dt)
        template = self.checklists[(ref.referral_type, version)]
        statuses = self.item_statuses(referral_id, now)
        conditions = self._conditions_at(ref, now_dt)
        steps = []
        for step in STEPS:
            reasons = []
            waived = []
            for item in template["items"]:
                if step not in item.get("gates", []):
                    continue
                st = statuses[item["item_key"]]["status"]
                if st in OPEN_ISSUES:
                    reasons.append({"item_key": item["item_key"], "issue": st})
                elif st == "WAIVED":
                    waived.append(item["item_key"])
            window = conditions.get("bed_window")
            if step == "BED_RESERVATION" and window and _parse_ts(window["end"]) <= now_dt:
                reasons.append({"issue": "BED_WINDOW_CLOSED", "bed_window": window})
            if conditions.get("patient_consent") == "WITHDRAWN":
                reasons.append({"issue": "CONSENT_WITHDRAWN"})
            if not reasons:
                state = "READY"
            elif all(r.get("issue") == CONFLICT_ISSUE for r in reasons):
                state = "PAUSED"
            else:
                state = "BLOCKED"
            steps.append({"step": step, "state": state, "reasons": reasons, "waived": waived})
        first_blocked = next((s["step"] for s in steps if s["state"] != "READY"), None)
        return {
            "referral_id": referral_id,
            "checklist_version": version,
            "generated_at": now_dt.isoformat(),
            "emergency_level": conditions.get("emergency_level"),
            "steps": steps,
            "executable": [s["step"] for s in steps if s["state"] == "READY"],
            "first_blocked_step": first_blocked,
        }

    def patient_view(self, referral_id, now):
        """患者侧视图：与内部缺件同一份投影，保证看到的缺件与下一步一致。"""
        statuses = self.item_statuses(referral_id, now)
        missing = [
            {"item_key": key, "label": st["label"], "issue": st["status"]}
            for key, st in statuses.items()
            if st["status"] in OPEN_ISSUES
        ]
        if missing:
            first = missing[0]
            next_step = f"{NEXT_STEP_TEXT[first['issue']]}：{first['label']}"
        else:
            next_step = "资料已到齐，等待接收科室确认"
        return {"referral_id": referral_id, "missing": missing, "next_step": next_step}

    def open_issues(self, now, referral_id=None):
        """当前未决事项；issue_key 不含日期与班次，跨午夜保持稳定。"""
        result = []
        for rid in sorted(self.referrals):
            if referral_id and rid != referral_id:
                continue
            for item_key, st in self.item_statuses(rid, now).items():
                if st["status"] in OPEN_ISSUES:
                    result.append({
                        "issue_key": f"{rid}:{item_key}:{st['status']}",
                        "referral_id": rid,
                        "item_key": item_key,
                        "issue": st["status"],
                    })
        return result

    def _refresh_issues(self, at):
        for issue in self.open_issues(at):
            if issue["issue_key"] not in self.issue_index:
                self.issue_index[issue["issue_key"]] = {
                    **issue,
                    "first_seen": at,
                    "owner_shift": self.current_shift,
                }

    def open_responsibilities(self, now):
        """交接页用的未完成责任：谁名下、从什么时候开始。"""
        result = []
        for issue in self.open_issues(now):
            meta = self.issue_index.get(issue["issue_key"], {})
            result.append({
                **issue,
                "since": meta.get("first_seen"),
                "owner_shift": meta.get("owner_shift"),
            })
        return sorted(result, key=lambda i: (i["since"] or "", i["issue_key"]))

    def pending_reminders(self, now, already_sent=()):
        """待催办事项：只针对卡住环节的缺项；同一未决事项只催一次。

        issue_key 不含日期，运输跨午夜、跨班次都不会产生重复催办。
        """
        sent = set(already_sent)
        reminders = []
        for issue in self.open_issues(now):
            item = self._template_item(self.referrals[issue["referral_id"]], issue["item_key"])
            if not item.get("gates"):
                continue
            if issue["issue_key"] in sent:
                continue
            reminders.append(issue)
        return reminders

    def explain_blocker(self, referral_id, step, now):
        """从阻塞点反查：清单版本、材料回执（含已读）与限时放行依据。"""
        if step not in STEPS:
            raise ValueError(f"未知环节: {step!r}")
        now_dt = _as_dt(now)
        ref = self.referrals[referral_id]
        version = self._version_at(ref, now_dt)
        template = self.checklists[(ref.referral_type, version)]
        statuses = self.item_statuses(referral_id, now)
        plan = self.compute_plan(referral_id, now)
        entry = next(s for s in plan["steps"] if s["step"] == step)
        reasons = []
        for item in template["items"]:
            if step not in item.get("gates", []):
                continue
            st = statuses[item["item_key"]]
            if st["status"] == "OK":
                continue
            receipts, current = self._receipts_for(ref, item["item_key"], now_dt)
            current_ids = {r.receipt_id for r in current}
            reasons.append({
                "item_key": item["item_key"],
                "issue": st["status"],
                "checklist_version": version,
                "receipts": [
                    {
                        "receipt_id": r.receipt_id,
                        "digest": r.digest,
                        "channel": r.channel,
                        "sealed": r.sealed,
                        "current": r.receipt_id in current_ids,
                        "read_by": [
                            rd["reader_role"]
                            for rd in self.reads.get(r.receipt_id, [])
                            if _as_dt(rd["read_at"]) <= now_dt
                        ],
                    }
                    for r in receipts
                ],
                "waivers": [
                    {
                        "waiver_id": w["waiver_id"],
                        "granted_by": w["granted_by"],
                        "reason": w["reason"],
                        "granted_at": w["granted_at"],
                        "expires_at": w["expires_at"],
                        "revoked_at": w["revoked_at"],
                        "active": (
                            (w["revoked_at"] is None or _as_dt(w["revoked_at"]) > now_dt)
                            and _parse_ts(w["expires_at"]) > now_dt
                        ),
                    }
                    for w in self.waivers.values()
                    if w["referral_id"] == referral_id
                    and item["item_key"] in w["item_keys"]
                    and _as_dt(w["granted_at"]) <= now_dt
                ],
                "resolutions": [
                    {
                        "winning_receipt_id": r["winning_receipt_id"],
                        "resolved_by": r["resolved_by"],
                        "reason": r["reason"],
                        "resolved_at": r["resolved_at"],
                    }
                    for r in self.resolutions.get((referral_id, item["item_key"]), [])
                    if _as_dt(r["resolved_at"]) <= now_dt
                ],
            })
        reasons.extend(r for r in entry["reasons"] if "item_key" not in r)
        return {
            "referral_id": referral_id,
            "step": step,
            "state": entry["state"],
            "checklist_version": version,
            "reasons": reasons,
        }
