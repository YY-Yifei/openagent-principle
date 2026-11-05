"""规则层：机器可判的那一半判断。"""

from __future__ import annotations

import pytest

from openagent_principle.models import Principle, Rule
from openagent_principle.rules import evaluate, evaluate_rule, rule_verdict


@pytest.mark.parametrize(
    ("kind", "value", "text", "passed"),
    [
        ("must_contain_any", ["改进", "措施"], "改进：加评审", True),
        ("must_contain_any", ["改进", "措施"], "下次注意", False),
        ("must_contain_all", ["改进", "责任人"], "改进：加评审", False),
        ("must_contain_all", ["改进", "责任人"], "改进：加评审，责任人张三", True),
        ("must_not_contain_any", ["基本没问题"], "目前基本没问题", False),
        ("must_not_contain_any", ["基本没问题"], "存在 2 个高优缺陷", True),
        ("must_not_contain_any", ["ABC"], "abc 出现", False),  # 短语匹配不区分大小写
        ("must_match", r"\d{4}-\d{2}-\d{2}", "截止 2026-11-05", True),
        ("must_match", r"\d{4}-\d{2}-\d{2}", "截止下周", False),
        ("must_not_match", r"TODO|FIXME", "TODO: 补测试", False),
        ("must_not_match", r"TODO|FIXME", "已完成", True),
        ("min_chars", 10, "太短", False),
        ("max_chars", 10, "太短", True),
    ],
)
def test_evaluate_rule(kind, value, text, passed):
    rule = Rule(kind=kind, value=value)
    rule.validate()
    assert evaluate_rule(text, rule).passed is passed


def test_evidence_quotes_the_match():
    rule = Rule(kind="must_not_contain_any", value=["基本没问题"])
    rule.validate()
    hit = evaluate_rule("这次延期了三天，不过目前基本没问题，客户还不知道。", rule)
    assert hit.violated
    assert hit.evidence and "【基本没问题】" in hit.evidence[0]


def test_unknown_rule_type_is_skipped_not_failed():
    # 保存时会被拦截；万一有人手改了文件，判断链不能因此断掉
    hit = evaluate_rule("任意文本", Rule(kind="nonsense", value="x"))
    assert hit.passed and "跳过" in hit.detail


def test_rule_verdict_hit_is_confident(principle):
    violated, confidence, evidence, details, reasoning = rule_verdict(
        principle(), "目前基本没问题。"
    )
    assert violated is True
    assert confidence >= 0.75
    assert evidence
    assert details and reasoning


def test_rule_verdict_pass_is_honest_about_limits(principle):
    violated, confidence, evidence, _details, _reasoning = rule_verdict(
        principle(), "存在 2 个高优缺陷，若周五不修将影响 30% 用户。"
    )
    assert violated is False
    assert confidence == pytest.approx(0.6)  # 规则只能证明「没犯这几条」
    assert evidence == []


def test_rule_verdict_without_rules_has_no_opinion():
    violated, confidence, evidence, details, _ = rule_verdict(
        Principle(id="P-002", title="t", statement="s"), "任意材料"
    )
    assert violated is None and confidence == 0.0 and evidence == [] and details == []


def test_evaluate_returns_one_hit_per_rule():
    p = Principle(
        id="P-003",
        title="t",
        statement="s",
        rules=[
            Rule(kind="must_contain_any", value="目标"),
            Rule(kind="max_chars", value=1000),
        ],
    )
    assert len(evaluate(p, "目标是降低延迟")) == 2


# ---------------------------------------------------------------------------
# 适用范围（复审 4.1）
# ---------------------------------------------------------------------------
def test_out_of_scope_material_is_not_a_violation():
    """范围外材料不该被硬判违规 —— 这是误报的最大来源。

    以前 `scope` 只写进模型提示，规则层完全不看它：一条「仅复盘」的原则
    会对一份天气报告判「缺少改进」。
    """
    p = Principle(
        id="P-001",
        title="复盘必须有改进",
        statement="s",
        severity=4,
        scope="复盘",
        scope_keywords=["复盘", "事故", "总结"],
        rules=[Rule(kind="must_contain_any", value=["改进"])],
    )
    violated, confidence, evidence, _details, reasoning = rule_verdict(p, "今天天气晴朗。")
    assert violated is None
    assert confidence == 0.0
    assert evidence == []
    assert "不在适用范围" in reasoning


def test_in_scope_material_still_judged():
    p = Principle(
        id="P-001",
        title="复盘必须有改进",
        statement="s",
        severity=4,
        scope="复盘",
        scope_keywords=["复盘", "事故", "总结"],
        rules=[Rule(kind="must_contain_any", value=["改进"])],
    )
    violated, _conf, _ev, _d, _r = rule_verdict(p, "本次事故复盘：排期太紧。")
    assert violated is True


def test_principle_without_scope_keywords_applies_everywhere():
    """老原则没有 scope_keywords —— 行为不变，一律适用。"""
    p = Principle(
        id="P-001",
        title="t",
        statement="s",
        rules=[Rule(kind="must_not_contain_any", value=["基本没问题"])],
    )
    assert p.applies_to("任意材料")
    assert rule_verdict(p, "目前基本没问题")[0] is True


# ---------------------------------------------------------------------------
# ReDoS 防护（复审 5.7）
# ---------------------------------------------------------------------------
def test_catastrophic_regex_does_not_hang():
    """灾难性回溯的正则必须被超时截断，而不是卡住判断链。

    `(a+)+$` 只有 6 个字符 —— 长度上限防不住它。
    用子进程隔离：超时直接 terminate，不会留下后台线程烧 CPU。
    """
    import time

    from openagent_principle.rules import REGEX_TIMEOUT

    rule = Rule(kind="must_match", value=r"(a+)+$")
    rule.validate()
    text = "a" * 32 + "!"
    started = time.monotonic()
    hit = evaluate_rule(text, rule)
    elapsed = time.monotonic() - started
    assert elapsed < REGEX_TIMEOUT + 3.0, f"正则跑了 {elapsed:.1f}s，超时机制没生效"
    # 超时不能当成「没匹配」—— 必须如实说「这条没判」
    assert "超时" in hit.detail


def test_normal_regex_still_works():
    """超时机制不能误伤正常正则。"""
    rule = Rule(kind="must_match", value=r"\d{4}-\d{2}-\d{2}")
    rule.validate()
    hit = evaluate_rule("截止 2026-11-05", rule)
    assert hit.passed
    assert "2026-11-05" in hit.detail
