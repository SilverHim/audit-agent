"""复核决定的登记表: 允许决定矩阵 (ReviewQueue, ALLOWED) 与字段规则 (REVIEW_FIELDS), 第 7 节."""

from typing import Any

import pytest

from backend.contracts.errors import DecisionRejected
from backend.contracts.models import (
    Decision,
    DisputeResolution,
    MisstatementUpdate,
    NeitherAction,
    ReviewRecord,
    TaskKind,
)
from backend.orchestration.decisions import (
    REVIEW_FIELDS,
    UPDATE_BY_HUMAN,
    ReviewFields,
    check_review_fields,
    review_fields,
    update_decisions,
)
from backend.services.review_queue import ALLOWED
from tests.fakes import ENGAGEMENT, NUMBAT_CUTOFF_DRAFT
from tests.test_decisions import Started
from tests.test_rerun import failed_at_p0, pending_task
from tests.test_review_decisions import record
from tests.test_review_queue import QUEUE, make_task

EID = ENGAGEMENT.id
# 改为登记表之前的必填表与只允许字段 (逐条等价的基准)
OLD_REQUIRED: dict[Decision, list[str]] = {
    Decision.confirm: [],
    Decision.override: ["reason", "override_conclusion"],
    Decision.misstatement: ["reason", "misstatement_draft"],
    Decision.rerun: ["reason"],
    Decision.manual_result: ["reason", "manual_result"],
    Decision.dispute_resolution: ["reason", "dispute_resolution"],
}
OLD_ALLOWED: dict[Decision, set[str]] = {
    Decision.confirm: set(),
    Decision.override: {"override_conclusion"},
    Decision.misstatement: {"misstatement_draft"},
    Decision.rerun: {"rerun_params"},
    Decision.manual_result: {"manual_result"},
    Decision.dispute_resolution: {"dispute_resolution", "neither_action", "override_conclusion", "misstatement_draft"},
}
# 各专用字段的一个非空值
FILLED: dict[str, Any] = {
    "override_conclusion": "人工结论",
    "misstatement_draft": NUMBAT_CUTOFF_DRAFT,
    "rerun_params": {"window_days": 25},
    "manual_result": {"conclusion": "人工执行的结果"},
    "dispute_resolution": DisputeResolution.neither,
    "neither_action": NeitherAction.human_conclusion,
    # 需求 R156: 模板没有载荷类别时任何决定都不能带 (第 1 轮审核 R156-03); 有类别时见 test_update_required_where_human
    "misstatement_update": MisstatementUpdate(
        misstatement_id="M-D-1", base_version=1, kind="classification", origin="current_period"
    ),
}


# ── 允许决定矩阵 ─────────────────────────────────────────


def test_allowed_has_one_row_per_task_kind() -> None:
    assert set(ALLOWED) == set(TaskKind)
    assert {
        TaskKind.normal: {Decision.confirm, Decision.override, Decision.misstatement},
        TaskKind.unattributed: {Decision.override, Decision.misstatement},
        TaskKind.failed: {Decision.rerun},
        TaskKind.dispute: {Decision.dispute_resolution},
        TaskKind.manual: {Decision.manual_result},  # 需求 R92: 人工程序的任务只能提交结果
    } == ALLOWED


@pytest.mark.parametrize("kind", [TaskKind.manual, TaskKind.dispute])
def test_manual_result_added_only_to_failed_tasks(kind: TaskKind) -> None:
    """REV-03 的模板允许 manual_result, 带判断对象; 只有 failed 任务另加 manual_result."""
    assert QUEUE.allowed(make_task("REV-03", kind, subject_id="C-001")) == ALLOWED[kind]
    assert Decision.manual_result in QUEUE.allowed(make_task("REV-03", TaskKind.failed, subject_id="C-001"))


def test_registered_decisions_apply_to_their_kind_only(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(ALLOWED, TaskKind.manual, frozenset({Decision.manual_result}))
    assert QUEUE.allowed(make_task("DOC-04", TaskKind.manual, subject_id=None)) == {Decision.manual_result}
    assert QUEUE.allowed(make_task("DOC-04", TaskKind.failed, subject_id="D-001")) == {Decision.rerun}


# ── 字段规则 ─────────────────────────────────────────


# 按任务类型登记的行: 人工程序的结果另须执行日期 (需求 R92, 其校验见 test_manual)
KIND_ROWS = {(TaskKind.manual, Decision.manual_result)}


def test_generic_rows_equal_old_tables() -> None:
    """现有 6 种决定各有一条通用行, 必填与只允许字段同改表之前; 按任务类型的行只有 KIND_ROWS."""
    assert set(REVIEW_FIELDS) == {(None, d) for d in Decision} | KIND_ROWS
    for decision in Decision:
        rules = REVIEW_FIELDS[(None, decision)]
        assert (list(rules.required), set(rules.allowed)) == (OLD_REQUIRED[decision], OLD_ALLOWED[decision])


@pytest.mark.parametrize("kind", list(TaskKind))
def test_every_kind_falls_back_to_generic_row(kind: TaskKind) -> None:
    """没有按任务类型登记的 (任务类型, 决定) 都用该决定的通用行, 登记了的用自己的行."""
    for decision in Decision:
        expected = REVIEW_FIELDS[(kind, decision) if (kind, decision) in KIND_ROWS else (None, decision)]
        assert review_fields(kind, decision) is expected


def test_every_allowed_decision_has_field_rules() -> None:
    for kind, decisions in ALLOWED.items():
        for decision in decisions | {Decision.manual_result}:
            assert isinstance(review_fields(kind, decision), ReviewFields)


@pytest.mark.parametrize("decision", list(Decision))
def test_check_fields_same_as_old_tables(decision: Decision) -> None:
    """每种决定: 只填必填的通过; 缺任一必填报 MISSING_FIELD; 填了不允许的专用字段报 FIELD_NOT_ALLOWED."""
    required = {name: FILLED.get(name, "复核意见") for name in OLD_REQUIRED[decision]}
    if decision is Decision.dispute_resolution:  # 仲裁取 a6_upheld: 不另填 neither 的两项
        required["dispute_resolution"] = DisputeResolution.a6_upheld
    base = record("T-1", decision, **({"reason": None} | required))
    for kind in (k for k in TaskKind if (k, decision) not in KIND_ROWS):  # 按任务类型登记的行另测
        check_review_fields(base, kind)
        for name in OLD_REQUIRED[decision]:
            with pytest.raises(DecisionRejected) as e:
                check_review_fields(base.model_copy(update={name: None}), kind)
            assert (e.value.code, e.value.message) == ("MISSING_FIELD", name)
        for name in sorted(set(FILLED) - OLD_ALLOWED[decision]):
            with pytest.raises(DecisionRejected) as e:
                check_review_fields(base.model_copy(update={name: FILLED[name]}), kind)
            assert (e.value.code, e.value.message) == ("FIELD_NOT_ALLOWED", name)


def test_registered_row_applies_to_its_kind_only(monkeypatch: pytest.MonkeyPatch) -> None:
    """临时登记 (manual, manual_result): 只要理由, 可附 rerun_params. 只对 manual 任务生效, failed 任务仍用通用行."""
    rules = ReviewFields(("reason",), frozenset({"manual_result", "rerun_params"}))
    monkeypatch.setitem(REVIEW_FIELDS, (TaskKind.manual, Decision.manual_result), rules)
    decision: ReviewRecord = record("T-1", Decision.manual_result, rerun_params={"window_days": 25})
    assert review_fields(TaskKind.manual, Decision.manual_result) is rules
    check_review_fields(decision, TaskKind.manual)
    with pytest.raises(DecisionRejected) as e:
        check_review_fields(decision, TaskKind.failed)
    assert (e.value.code, e.value.message) == ("MISSING_FIELD", "manual_result")


async def test_submit_checks_fields_by_task_kind(monkeypatch: pytest.MonkeyPatch) -> None:
    """DecisionSubmit 按任务类型取字段规则: 登记 (failed, rerun) 须填 rerun_params 后, 不带它的 rerun 被拒;
    登记在 normal 上的同一行不影响 failed 任务."""
    runner, store, _ = await failed_at_p0()
    s = Started(runner, store, runner.deps.engagement_store)  # type: ignore[arg-type]
    task = await pending_task(store)
    assert task.kind is TaskKind.failed
    strict = ReviewFields(("reason", "rerun_params"), frozenset({"rerun_params"}))
    decision = record(task.id, Decision.rerun, id="D-ACC04")

    monkeypatch.setitem(REVIEW_FIELDS, (TaskKind.failed, Decision.rerun), strict)
    with pytest.raises(DecisionRejected) as e:
        await s.submit.submit(EID, decision)
    assert (e.value.code, e.value.message) == ("MISSING_FIELD", "rerun_params")

    monkeypatch.delitem(REVIEW_FIELDS, (TaskKind.failed, Decision.rerun))
    monkeypatch.setitem(REVIEW_FIELDS, (TaskKind.normal, Decision.rerun), strict)
    await s.submit.submit(EID, decision)


def test_update_required_where_human_replaces_the_ai() -> None:
    """R159: 模板带错报更新载荷类别时, 只有以人工判断取代 AI 判断的四种 (任务类型, 决定) 在原规则上另加必填
    misstatement_update; 其余 (confirm, 对判断认定错报, 仲裁等) 规则不变, 带了照旧 FIELD_NOT_ALLOWED. 没有类别时
    同原表."""
    replacing = {
        (TaskKind.normal, Decision.override),
        (TaskKind.unattributed, Decision.override),
        (TaskKind.unattributed, Decision.misstatement),
        (TaskKind.failed, Decision.manual_result),
    }
    assert replacing == UPDATE_BY_HUMAN
    for kind in TaskKind:
        for decision in Decision:
            base, rules = review_fields(kind, decision), review_fields(kind, decision, "verification")
            added = {"misstatement_update"} if (kind, decision) in UPDATE_BY_HUMAN else set()
            assert (set(rules.required), set(rules.allowed)) == (set(base.required) | added, set(base.allowed) | added)
    confirm = record("T-1", misstatement_update=FILLED["misstatement_update"])
    with pytest.raises(DecisionRejected) as e:
        check_review_fields(confirm, TaskKind.normal, "classification")
    assert (e.value.code, e.value.message) == ("FIELD_NOT_ALLOWED", "misstatement_update")


@pytest.mark.parametrize(
    ("kind", "expected"),
    [
        (TaskKind.normal, [Decision.override]),
        (TaskKind.unattributed, [Decision.misstatement, Decision.override]),
        (TaskKind.failed, [Decision.manual_result]),
        (TaskKind.dispute, []),
        (TaskKind.manual, []),
    ],
)
def test_update_decisions_follow_the_field_rules(kind: TaskKind, expected: list[Decision]) -> None:
    """R163 第 1 轮审核 R163-04: 复核队列告诉前端的须带载荷的决定, 为允许的决定 (failed 另加 manual_result) 中按
    review_fields 必填 misstatement_update 的, 保持给定顺序; 模板没有载荷类别时为空."""
    allowed = sorted(ALLOWED[kind] | ({Decision.manual_result} if kind is TaskKind.failed else set()))
    needing = [d for d in allowed if "misstatement_update" in review_fields(kind, d, "classification").required]
    assert update_decisions(kind, allowed, "classification") == needing == expected
    assert update_decisions(kind, allowed, None) == []
