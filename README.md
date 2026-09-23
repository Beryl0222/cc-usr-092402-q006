# 转诊管家班次协调

本项目用于整理转诊管家班次协调领域中的事件名称、交换字段与脱敏样例，方便业务、运营和研发人员在同一套术语下讨论后续服务。资料只包含领域约定，不包含真实个人信息、生产连接或外部账号。

## 目录

- `src/referral_nurse_roster.py`：班次/交班事件的种类与最小字段校验。
- `src/referral_materials.py`：转诊资料到件闭环的事件目录、载荷约定与纯 Python 投影。
- `data/sample.json`：用于核对班次事件格式的虚构事件。
- `data/referral_materials_sample.jsonl`：覆盖完整到件闭环的虚构事件流（每行一个事件）。
- `tests/`：保证样例与领域约定保持一致，并锁定闭环行为。

## 转诊资料到件闭环（src/referral_materials.py）

在班次与交班事件之上，按转诊类型维护带生效期的材料清单，把患者授权、来源机构签章、检查状态、运输交接、接收科室确认和替代材料关联到一次转诊，并从同一事件流投影出方案、患者视图、交接责任与催办。

### 事件目录

| kind | 含义 |
| --- | --- |
| `SHIFT_OPENED` | 班次开始，登记当前值班（交接着落的归属方） |
| `CHECKLIST_VERSION_PUBLISHED` | 发布某转诊类型的清单版本，带生效期，版本不可变 |
| `REFERRAL_CHECKLIST_BOUND` | 转诊绑定清单版本（绑定即定版），可带初始条件 |
| `REFERRAL_CHECKLIST_REBOUND` | 显式换版，必须给出原因；清单升级不会自动改绑在途转诊 |
| `MATERIAL_RECEIVED` | 材料到件回执：类别、渠道、摘要、签章、检查状态、授权失效时间、更正链 |
| `MATERIAL_READ_RECORDED` | 接收方已读记录，按回执登记 |
| `MATERIAL_CONFLICT_RESOLVED` | 冲突裁定：指定胜出版本，其余判负留痕 |
| `WAIVER_GRANTED` | 限时人工放行：只针对明确缺项，必须带到期时间 |
| `WAIVER_REVOKED` | 放行收回（人工）；到期收回由时间自动生效，无需事件 |
| `REFERRAL_CONDITION_CHANGED` | 授权状态、床位窗口、急诊级别等条件变化 |
| `HANDOVER_ACCEPTED` | 接班方在交接页逐项接管未完成责任 |

### 闭环规则

- **清单定版**：转诊绑定哪个版本就按哪个版本评估；清单升级不影响在途转诊，换版只能显式 `REBOUND` 留痕。
- **更正留痕**：更正材料用 `supersedes` 串起旧版，旧版不删除；已读按回执记录，可识别"旧版已读、新版未读"（`correction_unread`）。
- **摘要归并**：同一摘要的多渠道回执归并为一份材料；不同摘要且不在更正链上的回执构成冲突。
- **冲突局部暂停**：冲突只暂停依赖该材料的环节（`PAUSED`），其余准备继续；冲突只能裁定，不能放行。
- **限时放行**：`WAIVER_GRANTED` 只接受当前明确缺项（缺失/未签章/授权过期/检查未完）且卡住环节的条目；到期自动收回，无需额外事件。
- **方案重算**：授权、床位窗口、急诊级别变化写入条件事件后，`compute_plan` 在任意时刻给出最新可执行方案。
- **一致视图**：患者视图与内部缺件来自同一投影；`explain_blocker` 可从阻塞点反查清单版本、材料回执（含已读）与限时放行依据。
- **提醒去重**：未决事项键不含日期与班次，运输跨午夜、跨班次不会重复催办；交接只改归属，不改事项键。

### 投影视图

- `item_statuses(referral_id, now)`：每个清单条目的状态（OK/WAIVED/MISSING/SEAL_MISSING/AUTH_EXPIRED/EXAM_INCOMPLETE/CONFLICTED）。
- `compute_plan(referral_id, now)`：发车、床位、接诊三环节的可执行方案与阻塞原因。
- `patient_view(referral_id, now)`：患者侧缺件清单与下一步。
- `open_responsibilities(now)` / `pending_reminders(now, sent)`：交接责任与待催办事项。
- `explain_blocker(referral_id, step, now)`：阻塞点反查。

所有视图都以传入的 `now` 求值：授权过期、放行到期、床位窗口关闭随时间自动生效，重放历史事件即可还原任意时刻的状态。

## 本地核对

```bash
python3 -m unittest discover -s tests
python3 -m compileall -q src tests
```
