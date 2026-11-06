"""结构化 API：给程序 / 其他 agent 调用的入口。

与 `tools.py` 的分工：那一层返回给模型读的中文报告、错误是 `[错误] …` 字符串；
这一层返回对象、错误是异常。同一个能力，两个消费者。
"""

from __future__ import annotations

import json

import pytest

from openagent_principle import api
from openagent_principle.config import load_settings
from openagent_principle.models import Principle


# --------------------------------------------------------------------- 原则
def test_list_principles_sorted_by_severity(store, principle):
    store.save(principle(severity=2))
    store.save(principle(id="P-002", title="红线", statement="s", severity=5))
    result = api.list_principles()
    assert [p.id for p in result] == ["P-002", "P-001"]
    assert all(isinstance(p, Principle) for p in result)


def test_list_principles_filters_by_tag(store, principle):
    store.save(principle(tags=["风险"]))
    store.save(principle(id="P-002", title="别的", statement="s", tags=["复盘"]))
    assert [p.id for p in api.list_principles(tag="风险")] == ["P-001"]


def test_get_principle_raises_not_found(store):
    with pytest.raises(api.NotFoundError):
        api.get_principle("P-404")


def test_save_principle_creates_and_returns_object(store):
    saved = api.save_principle(title="新原则", statement="要说实话", severity=4)
    assert saved.id == "P-001"
    assert saved.severity == 4
    assert store.get("P-001") is not None


def test_save_principle_update_only_touches_given_fields(store, principle):
    store.save(principle(criteria="原标准"))
    updated = api.save_principle(principle_id="P-001", title="新标题")
    assert updated.title == "新标题"
    assert updated.criteria == "原标准"
    assert updated.version == 2


def test_save_principle_unknown_id_raises(store):
    with pytest.raises(api.NotFoundError):
        api.save_principle(principle_id="P-404", title="x")


def test_delete_principle_archives_by_default(store, principle):
    store.save(principle())
    removed = api.delete_principle("P-001")
    assert removed.id == "P-001"
    assert store.get("P-001").archived is True


def test_delete_principle_hard_removes(store, principle):
    store.save(principle())
    api.delete_principle("P-001", hard=True)
    assert store.get("P-001") is None


# --------------------------------------------------------------------- 判断
def _jev_settings(monkeypatch):
    """配好 JEV key —— 没 key 时 `jev.ask` 直接抛错，根本走不到 transport。"""
    monkeypatch.setenv("JEV_API_KEY", "k")
    monkeypatch.setenv("JEV_BASE_URL", "https://jev.example")
    return load_settings()


def _jev_transport(answers: dict):
    def _send(url, headers, payload, timeout):  # noqa: ANN001, ARG001
        return 200, json.dumps({"model": "jev-1.13.0", "answers": answers})

    return _send


def _violation_answers() -> dict:
    return {
        "p_001": {
            "type": "choice",
            "choice": "violation",
            "probabilities": {"pass": 0.05, "violation": 0.9},
            "confidence": 0.85,
        },
        "overall": {"type": "choice", "choice": "violation"},
    }


def test_judge_returns_structured_judgement(store, principle, monkeypatch):
    store.save(principle())
    result = api.judge(
        "目前基本没问题。",
        backend="jev",
        transport=_jev_transport(_violation_answers()),
        settings=_jev_settings(monkeypatch),
    )
    assert result.overall == "violation"
    assert result.violations[0].principle_id == "P-001"


def test_judge_empty_material_raises_invalid_input(store):
    with pytest.raises(api.InvalidInputError):
        api.judge("   ", backend="jev")


def test_judge_file_missing_raises_not_found(store, tmp_path):
    with pytest.raises(api.NotFoundError):
        api.judge_file(tmp_path / "nope.md", backend="jev")


def test_judge_file_directory_raises_invalid_input(store, tmp_path):
    with pytest.raises(api.InvalidInputError):
        api.judge_file(tmp_path, backend="jev")


def test_judge_file_reads_and_judges(store, principle, tmp_path, monkeypatch):
    store.save(principle())
    target = tmp_path / "周报.md"
    target.write_text("目前基本没问题。", encoding="utf-8")
    result = api.judge_file(
        target,
        backend="jev",
        transport=_jev_transport(_violation_answers()),
        settings=_jev_settings(monkeypatch),
    )
    assert result.overall == "violation"
    assert result.subject == str(target)


# --------------------------------------------------------------- 案例与反馈
def test_case_roundtrip(store, principle, monkeypatch):
    store.save(principle())
    result = api.judge(
        "目前基本没问题。",
        backend="jev",
        transport=_jev_transport(_violation_answers()),
        settings=_jev_settings(monkeypatch),
    )
    fetched = api.get_case(result.case_id)
    assert fetched.case_id == result.case_id
    assert [c.case_id for c in api.list_cases()] == [result.case_id]


def test_get_case_unknown_raises(store):
    with pytest.raises(api.NotFoundError):
        api.get_case("C-00000000-000000-0000")


def test_record_feedback_unknown_case_raises(store):
    with pytest.raises(api.NotFoundError):
        api.record_feedback("C-00000000-000000-0000", agree=False)


def test_record_feedback_writes(store, principle, monkeypatch):
    store.save(principle())
    result = api.judge(
        "目前基本没问题。",
        backend="jev",
        transport=_jev_transport(_violation_answers()),
        settings=_jev_settings(monkeypatch),
    )
    record = api.record_feedback(result.case_id, agree=False, note="判宽了")
    assert record["agree"] is False
    assert record["note"] == "判宽了"


def test_stats_shape(store, principle):
    store.save(principle())
    data = api.stats()
    assert data["stats"]["active"] == 1
    assert isinstance(data["feedback"], list)
