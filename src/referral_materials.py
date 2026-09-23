"""转诊资料到件闭环的领域约定。

在班次与交班事件（referral_nurse_roster）之上，补充按转诊类型维护的
材料清单、到件归并、更正留痕、冲突暂停、限时放行与跨班接管的交换字段
和派生规则。所有函数均为纯函数：不读写外部状态，时间一律使用带时区的
ISO 8601 字符串，方便跨机构交换与重放核对。
"""

from __future__ import annotations

from datetime import datetime

from src.referral_nurse_roster import EVENT_KINDS as BASE_EVENT_KINDS
from src.referral_nurse_roster import REQUIRED_FIELDS

MATERIAL_EVENT_KINDS = [
    "CHECKLIST_PUBLISHED",       # 按转诊类型发布带生效期的材料清单版本
    "REFERRAL_CHECKLIST_BOUND",  # 转诊出发时锁定清单版本，升级不影响已出发患者
    "MATERIAL_RECEIVED",         # 材料经某渠道到件，携带内容摘要
    "MATERIAL_CORRECTED",        # 已签发材料更正，旧版保留并标记被取代
    "MATERIAL_READ",             # 接收方读取某版本材料的回执
    "SUBSTITUTE_LINKED",         # 替代材料关联到清单缺项
    "CONFLICT_FLAGGED",          # 多渠道内容冲突，暂停依赖它的环节
    "CONFLICT_RESOLVED",         # 冲突解除，恢复被暂停的环节
    "PLAN_RECOMPUTED",           # 授权/床位窗口/急诊级别变化后重算可执行方案
    "OVERRIDE_GRANTED",          # 针对明确缺项的限时人工放行
    "OVERRIDE_EXPIRED",          # 放行到期自动收回
    "REMINDER_SENT",             # 缺件催办，按 转诊+缺项+运输段 去重
]

EVENT_KINDS = BASE_EVENT_KINDS + MATERIAL_EVENT_KINDS

# 关联到一次转诊的材料类别：患者授权、来源机构签章、检查状态、
# 运输交接、接收科室确认（替代材料通过 SUBSTITUTE_LINKED 关联）。
MATERIAL_CATEGORIES = (
    "PATIENT_CONSENT",
    "SOURCE_SEAL",
    "EXAM_STATUS",
    "TRANSPORT_HANDOVER",
    "RECEIVING_CONFIRMATION",
)

# 准备环节 -> 依赖的材料类别。内容冲突只暂停依赖该类别的环节，
# 其他准备继续进行。
STEP_DEPENDENCIES = {
    "TRANSPORT": ("PATIENT_CONSENT",),
    "BED": ("RECEIVING_CONFIRMATION",),
    "CLINICAL_INTAKE": ("EXAM_STATUS", "SOURCE_SEAL", "TRANSPORT_HANDOVER"),
}

PLAN_STEPS = tuple(STEP_DEPENDENCIES)

PAYLOAD_FIELDS = {
    "CHECKLIST_PUBLISHED": ("checklist_id", "referral_type", "version", "effective_from", "items"),
    "REFERRAL_CHECKLIST_BOUND": ("referral_id", "checklist_id", "version"),
    "MATERIAL_RECEIVED": ("receipt_id", "referral_id", "item_id", "channel", "digest"),
    "MATERIAL_CORRECTED": ("receipt_id", "referral_id", "item_id", "supersedes", "digest"),
    "MATERIAL_READ": ("receipt_id", "reader"),
    "SUBSTITUTE_LINKED": ("referral_id", "item_id", "substitute_receipt_id"),
    "CONFLICT_FLAGGED": ("referral_id", "item_id", "paused_steps"),
    "CONFLICT_RESOLVED": ("referral_id", "item_id"),
    "PLAN_RECOMPUTED": ("referral_id", "trigger", "plan"),
    "OVERRIDE_GRANTED": ("override_id", "referral_id", "item_id", "expires_at"),
    "OVERRIDE_EXPIRED": ("override_id",),
    "REMINDER_SENT": ("referral_id", "item_id", "leg_id"),
}


def _parse(iso: str) -> datetime:
    return datetime.fromisoformat(iso)


def validate_event(record: dict) -> list[str]:
    """检查闭环事件是否具备可交换的最小字段（含班次/交班基础事件）。"""
    problems = [name for name in REQUIRED_FIELDS if name not in record]
    kind = record.get("kind")
    if kind not in EVENT_KINDS:
        problems.append("kind")
        return problems
    required = PAYLOAD_FIELDS.get(kind)
    if required:
        payload = record.get("payload")
        if not isinstance(payload, dict):
            problems.append("payload")
        else:
            problems += [f"payload.{name}" for name in required if name not in payload]
    return problems


# ---------------------------------------------------------------- 清单版本

def checklist_at(checklists: list[dict], referral_type: str, at: str) -> dict | None:
    """返回某转诊类型在 at 时刻生效的清单版本；无生效版本时返回 None。"""
    moment = _parse(at)
    candidates = [
        c for c in checklists
        if c["referral_type"] == referral_type
        and _parse(c["effective_from"]) <= moment
        and (c.get("effective_to") is None or moment < _parse(c["effective_to"]))
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda c: (c["version"], _parse(c["effective_from"])))


def find_checklist(checklists: list[dict], checklist_id: str, version: int) -> dict:
    for checklist in checklists:
        if checklist["checklist_id"] == checklist_id and checklist["version"] == version:
            return checklist
    raise KeyError(f"unknown checklist {checklist_id} v{version}")


def bind_checklist(referral: dict, checklists: list[dict], departed_at: str) -> dict:
    """出发时绑定清单版本。

    已绑定的转诊保持原版本：清单升级不能把已出发患者变成没有记录，
    其缺件口径仍按出发时的清单版本核对。
    """
    bound = referral.get("checklist")
    if bound is not None:
        return bound
    chosen = checklist_at(checklists, referral["referral_type"], departed_at)
    if chosen is None:
        raise ValueError("no effective checklist for referral type at departure")
    return {
        "checklist_id": chosen["checklist_id"],
        "version": chosen["version"],
        "bound_at": departed_at,
    }


# ---------------------------------------------------------------- 到件与更正

def receipt_problems(receipt: dict, item: dict | None, now: str) -> list[str]:
    """列出到件作为某清单项的当前问题；空列表表示可用。"""
    problems = []
    if receipt.get("superseded_by"):
        problems.append("superseded")
    if receipt.get("draft"):
        problems.append("draft")
    if item and item.get("requires_seal") and not receipt.get("sealed"):
        problems.append("unsealed")
    expires_at = receipt.get("expires_at")
    if expires_at is not None and _parse(expires_at) <= _parse(now):
        problems.append("expired")
    return problems


def apply_correction(receipts: list[dict], correction: dict) -> list[dict]:
    """登记更正：旧版保留并标记被谁取代，历史不丢。"""
    updated = [
        {**r, "superseded_by": correction["receipt_id"]}
        if r["receipt_id"] == correction["supersedes"] else r
        for r in receipts
    ]
    updated.append(correction)
    return updated


def merge_receipts(receipts: list[dict]) -> dict:
    """按内容摘要归并多渠道到件。

    相同 (referral_id, item_id, digest) 的到件合并为一条并记录所有渠道；
    同一清单项存在多个未作废摘要时判定为内容冲突。
    返回 {"materials": [...], "conflicts": [...]}。
    """
    current = [r for r in receipts if not r.get("superseded_by")]
    groups: dict[tuple, list[dict]] = {}
    for receipt in current:
        key = (receipt["referral_id"], receipt["item_id"], receipt["digest"])
        groups.setdefault(key, []).append(receipt)
    materials = []
    for (referral_id, item_id, digest), group in sorted(groups.items()):
        materials.append({
            "referral_id": referral_id,
            "item_id": item_id,
            "digest": digest,
            "channels": sorted({r["channel"] for r in group}),
            "receipt_ids": sorted(r["receipt_id"] for r in group),
            "latest": max(group, key=lambda r: (r["received_at"], r["receipt_id"])),
        })
    by_item: dict[tuple, list[dict]] = {}
    for material in materials:
        by_item.setdefault((material["referral_id"], material["item_id"]), []).append(material)
    conflicts = [
        {
            "referral_id": referral_id,
            "item_id": item_id,
            "digests": sorted(m["digest"] for m in group),
        }
        for (referral_id, item_id), group in sorted(by_item.items())
        if len(group) > 1
    ]
    return {"materials": materials, "conflicts": conflicts}


def paused_steps(conflicts: list[dict], item_categories: dict[str, str]) -> list[str]:
    """内容冲突只暂停依赖该类别材料的环节，其他准备继续进行。"""
    paused = set()
    for conflict in conflicts:
        category = item_categories.get(conflict["item_id"])
        for step, dependencies in STEP_DEPENDENCIES.items():
            if category in dependencies:
                paused.add(step)
    return sorted(paused)


def correction_followups(receipts: list[dict], reads: list[dict]) -> list[dict]:
    """更正留痕：旧版保留，且能知道接收方是否读过新旧版本。"""
    read_ids = {read["receipt_id"] for read in reads}
    followups = []
    for old in receipts:
        new_id = old.get("superseded_by")
        if not new_id:
            continue
        followups.append({
            "item_id": old["item_id"],
            "old_receipt_id": old["receipt_id"],
            "old_version_read": old["receipt_id"] in read_ids,
            "new_receipt_id": new_id,
            "new_version_read": new_id in read_ids,
        })
    return followups


# ---------------------------------------------------------------- 缺项与放行

def active_overrides(overrides: list[dict], now: str) -> list[dict]:
    """未收回且未到期的放行；到期自动收回，无需人工干预。"""
    moment = _parse(now)
    return [
        o for o in overrides
        if not o.get("expired") and o.get("expires_at") and _parse(o["expires_at"]) > moment
    ]


def _best_problems(candidates: list[dict], item: dict, now: str) -> list[str] | None:
    if not candidates:
        return ["absent"]
    ranked = sorted((receipt_problems(r, item, now) for r in candidates), key=len)
    return ranked[0] or None


def missing_items(
    checklist: dict,
    referral_id: str,
    receipts: list[dict],
    substitutes: list[dict],
    overrides: list[dict],
    now: str,
) -> list[dict]:
    """计算清单缺项，是协调员视图与患者视图共同的缺件来源。

    覆盖依次来自：本项可用到件、关联的替代材料、未到期的人工放行。
    内容冲突中的清单项按缺项处理，原因记为 conflicted。
    """
    by_id = {r["receipt_id"]: r for r in receipts}
    scoped = [r for r in receipts if r["referral_id"] == referral_id]
    conflicted = {c["item_id"] for c in merge_receipts(scoped)["conflicts"]}
    waived = {
        o["item_id"] for o in active_overrides(overrides, now)
        if o["referral_id"] == referral_id
    }
    missing = []
    for item in checklist["items"]:
        if not item.get("required", True):
            continue
        item_id = item["item_id"]
        if item_id in waived:
            continue
        if item_id in conflicted:
            missing.append({"item_id": item_id, "category": item["category"], "reasons": ["conflicted"]})
            continue
        candidates = [r for r in scoped if r["item_id"] == item_id]
        if item.get("substitutable", True):
            for link in substitutes:
                if link["referral_id"] == referral_id and link["item_id"] == item_id:
                    substitute = by_id.get(link["substitute_receipt_id"])
                    if substitute is not None:
                        candidates.append(substitute)
        problems = _best_problems(candidates, item, now)
        if problems is not None:
            missing.append({"item_id": item_id, "category": item["category"], "reasons": problems})
    return missing


def grant_override(missing: list[dict], grant: dict, now: str) -> dict:
    """人工放行：只能针对明确缺项，且必须带未来的到期时间。"""
    if grant["item_id"] not in {m["item_id"] for m in missing}:
        raise ValueError("override target must be an explicit missing item")
    if _parse(grant["expires_at"]) <= _parse(now):
        raise ValueError("override expiry must be in the future")
    return {
        "override_id": grant["override_id"],
        "referral_id": grant["referral_id"],
        "item_id": grant["item_id"],
        "granted_by": grant["granted_by"],
        "reason": grant.get("reason", ""),
        "granted_at": now,
        "expires_at": grant["expires_at"],
    }


# ---------------------------------------------------------------- 方案与视图

def next_actions(missing: list[dict], conflicts: list[dict]) -> list[dict]:
    """缺件与冲突对应的下一步动作，患者与协调员看到同一份。"""
    conflicted = {c["item_id"] for c in conflicts}
    actions = [{"action": "RESOLVE_CONFLICT", "item_id": c["item_id"]} for c in conflicts]
    actions += [
        {"action": "COLLECT", "item_id": m["item_id"], "reasons": m["reasons"]}
        for m in missing
        if m["item_id"] not in conflicted
    ]
    return actions


def _bed_window_open(context: dict, now: str) -> bool:
    # 危重急诊不等床位窗口；其余情况按窗口判定。
    if context.get("emergency_level") == "CRITICAL":
        return True
    window = context.get("bed_window")
    if not window:
        return True
    moment = _parse(now)
    return _parse(window["start"]) <= moment < _parse(window["end"])


def recompute_plan(
    checklist: dict,
    referral_id: str,
    receipts: list[dict],
    substitutes: list[dict],
    overrides: list[dict],
    context: dict,
    now: str,
) -> dict:
    """授权、床位窗口或急诊级别变化后，重新给出可执行方案。

    context 携带 bed_window（{"start", "end"}，可空）与
    emergency_level（ROUTINE/URGENT/CRITICAL）。
    """
    categories = {i["item_id"]: i["category"] for i in checklist["items"]}
    scoped = [r for r in receipts if r["referral_id"] == referral_id]
    conflicts = merge_receipts(scoped)["conflicts"]
    missing = missing_items(checklist, referral_id, receipts, substitutes, overrides, now)
    missing_by_item = {m["item_id"]: m for m in missing}
    steps = []
    for step in PLAN_STEPS:
        dep_ids = {
            item_id for item_id, category in categories.items()
            if category in STEP_DEPENDENCIES[step]
        }
        step_conflicts = [c for c in conflicts if c["item_id"] in dep_ids]
        step_missing = [missing_by_item[i] for i in sorted(dep_ids) if i in missing_by_item]
        if step_conflicts:
            status, details = "paused", {"conflicts": step_conflicts}
        elif step_missing:
            status, details = "blocked", {"missing": step_missing}
        elif step == "BED" and not _bed_window_open(context, now):
            status, details = "waiting", {"reason": "outside_bed_window"}
        else:
            status, details = "ready", {}
        steps.append({"step": step, "status": status, "details": details})
    active = [o for o in active_overrides(overrides, now) if o["referral_id"] == referral_id]
    return {
        "referral_id": referral_id,
        "generated_at": now,
        "emergency_level": context.get("emergency_level", "ROUTINE"),
        "steps": steps,
        "missing": missing,
        "waived": [
            {"override_id": o["override_id"], "item_id": o["item_id"], "expires_at": o["expires_at"]}
            for o in active
        ],
        "next_actions": next_actions(missing, conflicts),
        "executable": all(s["status"] == "ready" for s in steps),
    }


def referral_status(state: dict, referral_id: str, now: str) -> dict:
    """单次转诊的闭环现状，供交接页、患者视图与反查共用。"""
    referral = state["referrals"][referral_id]
    binding = referral["checklist"]
    checklist = find_checklist(state["checklists"], binding["checklist_id"], binding["version"])
    receipts = [r for r in state["receipts"] if r["referral_id"] == referral_id]
    substitutes = [s for s in state["substitutes"] if s["referral_id"] == referral_id]
    overrides = [o for o in state["overrides"] if o["referral_id"] == referral_id]
    categories = {i["item_id"]: i["category"] for i in checklist["items"]}
    merged = merge_receipts(receipts)
    missing = missing_items(checklist, referral_id, receipts, substitutes, overrides, now)
    followups = correction_followups(receipts, state["reads"])
    return {
        "referral_id": referral_id,
        "checklist": binding,
        "missing": missing,
        "conflicts": merged["conflicts"],
        "paused_steps": paused_steps(merged["conflicts"], categories),
        "unread_corrections": [f for f in followups if not f["new_version_read"]],
        "active_overrides": active_overrides(overrides, now),
        "next_actions": next_actions(missing, merged["conflicts"]),
    }


def handover_view(state: dict, now: str) -> list[dict]:
    """交接页：只列出仍有未完成责任的转诊，跨班人员直接接管。"""
    board = []
    for referral_id in sorted(state["referrals"]):
        status = referral_status(state, referral_id, now)
        pending = (
            status["missing"] or status["conflicts"]
            or status["unread_corrections"] or status["active_overrides"]
        )
        if not pending:
            continue
        board.append({
            "referral_id": referral_id,
            "checklist": status["checklist"],
            "missing": status["missing"],
            "conflicts": status["conflicts"],
            "paused_steps": status["paused_steps"],
            "unread_corrections": status["unread_corrections"],
            "active_overrides": status["active_overrides"],
            "responsibility": state["responsibilities"].get(referral_id),
        })
    return board


def patient_view(state: dict, referral_id: str, now: str) -> dict:
    """患者视图：缺件与下一步与协调员视图同源，保证口径一致。"""
    status = referral_status(state, referral_id, now)
    actions = status["next_actions"]
    return {
        "referral_id": referral_id,
        "missing": status["missing"],
        "next_actions": actions,
        "next_step": actions[0] if actions else None,
    }


def trace_blocker(state: dict, referral_id: str, step: str, now: str) -> dict:
    """协调员从阻塞点反查：清单版本、材料回执与限时放行依据。"""
    status = referral_status(state, referral_id, now)
    checklist = find_checklist(
        state["checklists"], status["checklist"]["checklist_id"], status["checklist"]["version"]
    )
    dep_ids = {
        i["item_id"] for i in checklist["items"]
        if i["category"] in STEP_DEPENDENCIES[step]
    }
    return {
        "step": step,
        "checklist": status["checklist"],
        "receipts": [
            r for r in state["receipts"]
            if r["referral_id"] == referral_id and r["item_id"] in dep_ids
        ],
        "overrides": [o for o in status["active_overrides"] if o["item_id"] in dep_ids],
        "conflicts": [c for c in status["conflicts"] if c["item_id"] in dep_ids],
        "missing": [m for m in status["missing"] if m["item_id"] in dep_ids],
    }


# ---------------------------------------------------------------- 催办去重

def reminder_key(referral_id: str, item_id: str, leg_id: str) -> str:
    return f"{referral_id}|{item_id}|{leg_id}"


def due_reminders(missing: list[dict], referral_id: str, leg_id: str, sent_keys: set[str]) -> list[str]:
    """按 转诊+缺项+运输段 去重：同一运输段跨午夜也只催办一次。"""
    return [
        key
        for m in missing
        if (key := reminder_key(referral_id, m["item_id"], leg_id)) not in sent_keys
    ]


# ---------------------------------------------------------------- 事件重放

def replay(events: list[dict]) -> dict:
    """把事件流折叠为闭环状态，供各视图派生。

    CONFLICT_FLAGGED/CONFLICT_RESOLVED 与 PLAN_RECOMPUTED 属于通知类事件，
    权威状态由到件与上下文派生，重放时不重复记账。
    """
    state = {
        "checklists": [],
        "referrals": {},
        "receipts": [],
        "reads": [],
        "substitutes": [],
        "overrides": [],
        "reminders": set(),
        "responsibilities": {},
    }
    for event in events:
        kind = event["kind"]
        payload = event["payload"]
        occurred_at = event["occurred_at"]
        if kind == "CHECKLIST_PUBLISHED":
            state["checklists"].append(dict(payload))
        elif kind == "REFERRAL_CHECKLIST_BOUND":
            referral = state["referrals"].setdefault(payload["referral_id"], {})
            referral.setdefault("referral_type", payload.get("referral_type"))
            referral.setdefault("leg_id", payload.get("leg_id"))
            # 已绑定的转诊不随清单升级换版。
            referral.setdefault("checklist", {
                "checklist_id": payload["checklist_id"],
                "version": payload["version"],
                "bound_at": occurred_at,
            })
        elif kind in ("MATERIAL_RECEIVED", "MATERIAL_CORRECTED"):
            receipt = {
                "receipt_id": payload["receipt_id"],
                "referral_id": payload["referral_id"],
                "item_id": payload["item_id"],
                "channel": payload.get("channel", "unknown"),
                "digest": payload["digest"],
                "sealed": payload.get("sealed", False),
                "draft": payload.get("draft", False),
                "expires_at": payload.get("expires_at"),
                "received_at": occurred_at,
            }
            if kind == "MATERIAL_CORRECTED":
                receipt["supersedes"] = payload["supersedes"]
                state["receipts"] = apply_correction(state["receipts"], receipt)
            else:
                state["receipts"].append(receipt)
        elif kind == "MATERIAL_READ":
            state["reads"].append({
                "receipt_id": payload["receipt_id"],
                "reader": payload["reader"],
                "read_at": occurred_at,
            })
        elif kind == "SUBSTITUTE_LINKED":
            state["substitutes"].append(dict(payload))
        elif kind == "OVERRIDE_GRANTED":
            state["overrides"].append({**payload, "granted_at": occurred_at})
        elif kind == "OVERRIDE_EXPIRED":
            for override in state["overrides"]:
                if override["override_id"] == payload["override_id"]:
                    override["expired"] = True
        elif kind == "REMINDER_SENT":
            state["reminders"].add(
                reminder_key(payload["referral_id"], payload["item_id"], payload["leg_id"])
            )
        elif kind == "HANDOVER_ACCEPTED":
            state["responsibilities"][payload["referral_id"]] = {
                "accepted_by": payload["accepted_by"],
                "shift_id": payload["shift_id"],
                "accepted_at": occurred_at,
            }
    return state
