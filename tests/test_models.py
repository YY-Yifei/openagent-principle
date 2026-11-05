"""数据模型：校验、序列化、三态判断的边界。"""

from __future__ import annotations

import pytest

from openagent_principle.models import (
    Judgement,
    ModelError,
    Principle,
    Rule,
    Verdict,
    parse_rules,
)


def test_rule_validates_known_kind():
    rule = Rule(kind="must_not_contain_any", value="绝对没问题")
    rule.validate()
    assert rule.value == ["绝对没问题"]  # 单值被规范化成列表


@pytest.mark.parametrize(
    ("kind", "value"),
    [
        ("unknown_kind", "x"),
        ("must_not_contain_any", ""),
        ("must_not_contain_any", []),
        ("min_chars", "abc"),
        ("min_chars", "-1"),
        ("must_match", "(["),
    ],
)
def test_rule_rejects_bad_input(kind, value):
    with pytest.raises(ModelError):
        Rule(kind=kind, value=value).validate()


def test_parse_rules_accepts_json_and_dict():
    assert parse_rules("") == []
    rules = parse_rules('[{"kind":"must_contain_any","value":"改进,措施"}]')
    assert len(rules) == 1 and rules[0].kind == "must_contain_any"
    single = parse_rules({"kind": "max_chars", "value": 100})
    assert single[0].value == 100


def test_parse_rules_rejects_broken_json():
    with pytest.raises(ModelError):
        parse_rules("[{oops")


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
        backend="rules",
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
