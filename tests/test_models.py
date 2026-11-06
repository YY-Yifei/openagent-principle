"""数据模型：校验、序列化、三态判断的边界。"""

from __future__ import annotations

import pytest

from openagent_principle.models import (
    Judgement,
    ModelError,
    Principle,
    Verdict,
)


def test_principle_requires_title_and_statement():
    with pytest.raises(ModelError):
        Principle(id="P-001", title="", statement="x").validate()
    with pytest.raises(ModelError):
        Principle(id="P-001", title="t", statement="").validate()
    with pytest.raises(ModelError):
        Principle(id="P-001", title="t", statement="s", severity=9).validate()


def test_principle_roundtrip(principle):
    original = principle()
    restored = Principle.from_dict(original.to_dict())
    assert restored == original


def test_principle_full_text_mentions_key_fields(principle):
    text = principle().full_text()
    for needle in ("P-001", "不粉饰风险", "判定标准", "严重度"):
        assert needle in text


def test_verdict_labels():
    assert Verdict("P-001", "t", True).label == "违反"
    assert Verdict("P-001", "t", False).label == "未违反"
    assert Verdict("P-001", "t", None).label == "存疑"


def test_judgement_orders_violations_by_severity():
    judgement = Judgement(
        case_id="C-1",
        subject="s",
        material_chars=1,
        overall="violation",
        score=0.8,
        severity=5,
        verdicts=[
            Verdict("P-001", "轻", True, severity=2),
            Verdict("P-002", "重", True, severity=5),
            Verdict("P-003", "没事", False, severity=5),
            Verdict("P-004", "存疑", None, severity=3),
        ],
        backend="jev",
    )
    assert [v.principle_id for v in judgement.violations] == ["P-002", "P-001"]
    assert [v.principle_id for v in judgement.uncertain] == ["P-004"]


def test_judgement_roundtrip_keeps_three_states():
    judgement = Judgement(
        case_id="C-2",
        subject="s",
        material_chars=3,
        overall="uncertain",
        score=0.0,
        severity=0,
        verdicts=[Verdict("P-001", "t", None, 0.5, 0.2)],
        backend="jev",
        model="jev-1.13.0",
        notes=["备注"],
    )
    restored = Judgement.from_dict(judgement.to_dict())
    assert restored.verdicts[0].violated is None
    assert restored.notes == ["备注"]
