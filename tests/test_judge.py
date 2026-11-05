"""编排层：后端选择、原则筛选、合并策略、整条判断链路。"""

from __future__ import annotations

import json

import pytest

from openagent_principle import jev
from openagent_principle.config import load_settings
from openagent_principle.judge import (
    Judge,
    JudgeError,
    choose_backend,
    merge,
    select_principles,
)
from openagent_principle.models import Principle, Verdict
from openagent_principle.rules import rule_verdict
from openagent_principle.store import PrincipleStore


def _settings(**overrides):
    return load_settings(**overrides)


def _model_verdict(violated: bool | None, probability: float = 0.8, confidence: float = 0.8):
    return Verdict("P-001", "t", violated, probability, confidence, 4, [], "模型说的")


# --------------------------------------------------------------- 后端选择
def test_auto_without_keys_falls_back_to_rules(env):
    backend, notes = choose_backend(_settings())
    assert backend == "rules"
    assert any("规则" in note for note in notes)


def test_auto_prefers_jev(env):
    backend, _ = choose_backend(
        _settings(jev_api_key="k", jev_base_url="https://jev.example", api_key="sk")
    )
    assert backend == "jev"


def test_auto_skips_jev_without_base_url(env):
    """没有默认网关地址：只给 key 不算配好，auto 应该落到 llm。"""
    backend, _ = choose_backend(_settings(jev_api_key="k", api_key="sk"))
    assert backend == "llm"


def test_auto_uses_llm_when_jev_missing(env):
    backend, notes = choose_backend(_settings(api_key="sk"))
    assert backend == "llm"
    assert any("LLM" in note for note in notes)


def test_explicit_backend_wins_even_without_key(env):
    backend, notes = choose_backend(_settings(), "jev")
    assert backend == "jev"
    assert any("JEV_API_KEY" in note for note in notes)


def test_unknown_backend_is_auto(env):
    backend, notes = choose_backend(_settings(api_key="sk"), "magic")
    assert backend == "llm"
    assert any("auto" in note for note in notes)


# --------------------------------------------------------------- 原则筛选
def test_select_explicit_ids_reports_missing(store, principle):
    store.save(principle())
    picked, notes = select_principles(store, ["P-001", "P-404"], settings=_settings())
    assert [p.id for p in picked] == ["P-001"]
    assert any("P-404" in note for note in notes)


def test_select_auto_sorts_by_severity_and_caps(store, principle):
    store.save(principle(severity=2))
    store.save(principle(id="P-002", title="红线", statement="s", severity=5))
    store.save(principle(id="P-003", title="中", statement="s", severity=3))
    picked, notes = select_principles(store, settings=_settings(max_principles=2))
    assert [p.id for p in picked] == ["P-002", "P-003"]
    assert any("只判最严重" in note for note in notes)


def test_select_empty_library_is_explained(store):
    picked, notes = select_principles(store, settings=_settings())
    assert picked == []
    assert any("空" in note for note in notes)


# ------------------------------------------------------------------- 合并
# 架构：最终语义判断由 JEV 裁决；规则层只提供确定性匹配与原文证据。
def _report(principle, text="", *, in_scope=True, hits=None):
    """构造一个 RuleReport（不跑真实规则，测试用）。"""
    from openagent_principle.rules import RuleHit, RuleReport

    return RuleReport(
        principle_id=principle.id,
        in_scope=in_scope,
        hits=hits or [],
        scope_note="" if in_scope else "材料不在适用范围",
    )


def _hit(rule, passed, detail="", evidence=None):
    from openagent_principle.rules import RuleHit

    return RuleHit(rule=rule, passed=passed, detail=detail, evidence=evidence or [])


def test_merge_jev_pass_wins_over_rule_hit(principle):
    """**核心架构测试**：规则未通过，但 JEV 判 pass —— 以 JEV 为准。

    以前规则命中是「硬证据，不可推翻」。现在规则只是输入，JEV 是裁决者。
    冲突必须留痕（reasoning 里写明），但不改判。
    """
    from openagent_principle.models import Rule

    p = principle()
    rule = Rule(kind="must_not_contain_any", value=["基本没问题"])
    report = _report(p, hits=[_hit(rule, False, "出现了禁止短语", ["…【基本没问题】…"])])
    verdict = merge(p, report, _model_verdict(False, 0.9, 0.85), backend="jev")
    assert verdict.violated is False, "JEV 说 pass，规则不能推翻"
    assert verdict.source == "model"
    assert "规则层有未通过项" in verdict.reasoning
    assert any("基本没问题" in e for e in verdict.evidence), "规则证据仍要保留"


def test_merge_jev_violation_wins_over_rule_pass(principle):
    """反向冲突：规则全部通过，但 JEV 判 violation —— 仍以 JEV 为准。"""
    from openagent_principle.models import Rule

    p = principle()
    rule = Rule(kind="must_contain_any", value=["改进"])
    report = _report(p, hits=[_hit(rule, True, "包含：改进")])
    verdict = merge(p, report, _model_verdict(True, 0.88, 0.8), backend="jev")
    assert verdict.violated is True
    assert "规则层全部通过" in verdict.reasoning


def test_merge_rule_pass_with_uncertain_model_stays_uncertain(principle):
    """规则通过不能把 JEV 的「存疑」改写成「未违反」。"""
    from openagent_principle.models import Rule

    p = principle()
    rule = Rule(kind="must_contain_any", value=["改进"])
    report = _report(p, hits=[_hit(rule, True, "包含：改进")])
    verdict = merge(p, report, _model_verdict(None, 0.5, 0.3), backend="jev")
    assert verdict.violated is None
    assert verdict.source == "model"


def test_merge_contradictory_pass_becomes_uncertain(principle):
    """选了 pass 但概率只有 0.1 —— 模型自相矛盾，降为存疑。"""
    verdict = merge(principle(), _report(principle()), _model_verdict(False, 0.1, 0.9))
    assert verdict.violated is None
    assert "不一致" in verdict.reasoning


def test_merge_contradictory_violation_becomes_uncertain(principle):
    """对称检查：选了 violation 但概率只有 0.49，同样降为存疑。"""
    verdict = merge(principle(), _report(principle()), _model_verdict(True, 0.49, 0.24))
    assert verdict.violated is None


def test_merge_rules_backend_lets_rules_decide(principle):
    """**显式 rules 模式**：JEV 没答案时，规则可以给出最终结论。"""
    from openagent_principle.models import Rule

    p = principle()
    rule = Rule(kind="must_not_contain_any", value=["基本没问题"])
    report = _report(p, hits=[_hit(rule, False, "出现了禁止短语", ["…【基本没问题】…"])])
    verdict = merge(p, report, None, backend="rules")
    assert verdict.violated is True
    assert verdict.source == "rule"
    assert verdict.probability == 0.0, "规则层不编造概率"
    assert verdict.confidence > 0.75


def test_merge_non_rules_backend_without_model_is_uncertain(principle):
    """**后端降级**：jev/llm 没给答案时，规则不能自己定论 —— 必须存疑。"""
    from openagent_principle.models import Rule

    p = principle()
    rule = Rule(kind="must_not_contain_any", value=["基本没问题"])
    report = _report(p, hits=[_hit(rule, False, "出现了禁止短语", ["…【基本没问题】…"])])
    verdict = merge(p, report, None, backend="jev")
    assert verdict.violated is None, "语义判断缺席时不能由规则定论"
    assert "语义判断缺席" in verdict.reasoning
    assert verdict.evidence, "规则证据仍要保留供复核"


def test_merge_out_of_scope_is_not_applicable(principle):
    """范围外材料：这条原则不适用 → 未违反（不是存疑）。

    标成存疑会污染整体结论：一份完全合规的材料，只要有一条范围外原则，
    就永远拿不到 pass。
    """
    p = principle()
    report = _report(p, in_scope=False)
    verdict = merge(p, report, None, backend="rules")
    assert verdict.violated is False
    assert "不适用" in verdict.reasoning


def test_merge_out_of_scope_without_model_is_not_applicable(principle):
    """非 rules 模式同理：范围外不是「判不了」，是「不归我管」。"""
    p = principle()
    report = _report(p, in_scope=False)
    verdict = merge(p, report, None, backend="jev")
    assert verdict.violated is False
    assert "不适用" in verdict.reasoning


def test_merge_out_of_scope_ignores_model_verdict(principle):
    """**范围外时模型说什么都不采纳**（OCR 复审发现）。

    以前范围检查只在 `model_verdict is None` 分支里做。模型若对一条范围外的
    原则给了答案（它可能没读懂 scope 提示），直接采纳会把「不适用」变成
    违反/通过 —— 确定性判断不该被概率覆盖。
    """
    p = principle()
    report = _report(p, in_scope=False)
    verdict = merge(p, report, _model_verdict(True, 0.95, 0.9), backend="jev")
    assert verdict.violated is False, "范围外不该被模型判成违反"
    assert "不适用" in verdict.reasoning
    assert "已忽略" in verdict.reasoning


def test_verdict_from_answer_pass_without_probabilities_is_not_uncertain(principle):
    """**LLM 不给 probabilities 时，明确的 pass 不能被降成存疑**（OCR 复审发现）。

    `Answer.probability` 是「所选选项的概率」，不是「违反的概率」。
    以前 fallback 写成 `pass: 0.0`，`merge` 的「< 0.6 即自相矛盾」检查
    就把它降成存疑 —— 把「明确通过」误判成「不知道」。
    """
    from openagent_principle import jev
    from openagent_principle.judge import _verdict_from_answer

    p = principle()
    answer = jev.Answer(key="p_001", type="choice", value="pass", probabilities={}, confidence=0.0)
    verdict = _verdict_from_answer(p, answer)
    assert verdict.violated is False
    assert verdict.probability == 1.0, "pass 的所选选项概率应为 1.0"
    merged = merge(p, _report(p), verdict, backend="llm")
    assert merged.violated is False, "不能被误降为存疑"


def test_verdict_from_answer_violation_without_probabilities_still_violates(principle):
    """对称检查：violation 不给 probabilities 时仍是违反。"""
    from openagent_principle import jev
    from openagent_principle.judge import _verdict_from_answer

    p = principle()
    answer = jev.Answer(key="p_001", type="choice", value="violation", probabilities={}, confidence=0.0)
    verdict = _verdict_from_answer(p, answer)
    assert verdict.violated is True
    assert verdict.probability == 1.0


def test_merge_without_rules_follows_model(principle):
    verdict = merge(principle(), _report(principle()), _model_verdict(True, 0.9, 0.8))
    assert verdict.violated is True
    assert verdict.source == "model"


def test_merge_without_anything_is_uncertain(principle):
    verdict = merge(principle(rules=[]), _report(principle()), None, backend="rules")
    assert verdict.violated is None


def test_merge_downgrades_self_contradicting_model_verdict(principle):
    """模型选了 violation，但自报概率 < 0.6 —— 自相矛盾，降成存疑。"""
    for probability, confidence in ((0.49, 0.24), (0.51, 0.26), (0.59, 0.4)):
        verdict = merge(
            principle(rules=[]), _report(principle()), _model_verdict(True, probability, confidence)
        )
        assert verdict.violated is None, probability
        assert "不一致" in verdict.reasoning
        assert verdict.probability == pytest.approx(probability)


def test_merge_keeps_confident_model_violation(principle):
    verdict = merge(principle(rules=[]), _report(principle()), _model_verdict(True, 0.85, 0.77))
    assert verdict.violated is True


# --------------------------------------------------------------- 整条链路
def test_judge_with_rules_backend_flags_violation(store, principle):
    store.save(principle())
    judge = Judge(_settings(), store)
    judgement = judge.judge("目前基本没问题，客户还没发现。", backend="rules")
    assert judgement.overall == "violation"
    assert judgement.backend == "rules"
    assert judgement.severity == 4
    assert judgement.violations and judgement.violations[0].principle_id == "P-001"
    assert "基本没问题" in judgement.violations[0].evidence[0]
    # 案例落盘了
    assert store.list_cases()[0].case_id == judgement.case_id


def test_judge_with_rules_backend_passes_clean_material(store, principle):
    store.save(principle())
    judgement = Judge(_settings(), store).judge(
        "存在 2 个高优缺陷，若周五前不修复将影响 30% 用户下单。", backend="rules"
    )
    assert judgement.overall == "pass"
    assert judgement.score == 0.0


def test_judge_principle_without_rules_is_uncertain_locally(store):
    store.save(Principle(id="P-001", title="坦诚", statement="要说实话"))
    judgement = Judge(_settings(), store).judge("任意材料", backend="rules")
    assert judgement.overall == "uncertain"
    assert judgement.verdicts[0].violated is None
    assert any("无法判断" in v.reasoning for v in judgement.verdicts)


def test_judge_empty_library_is_uncertain_with_hint(store):
    judgement = Judge(_settings(), store).judge("任意材料", backend="rules")
    assert judgement.overall == "uncertain"
    assert judgement.verdicts == []
    assert any("空" in note for note in judgement.notes)


def test_judge_truncates_material_and_says_so(store, principle):
    store.save(principle())
    judge = Judge(_settings(max_material_chars=20), store)
    judgement = judge.judge("基本没问题" + "啊" * 100, backend="rules")
    assert judgement.material_chars == 20
    assert any("只判前 20" in note for note in judgement.notes)


def test_judge_falls_back_to_rules_when_jev_fails(store, principle):
    """**后端降级**：JEV 挂了、LLM 也没配 → 降级 rules，规则给出结论。

    这是「显式 rules 模式」之外唯一允许规则定论的情况 —— 已经没有语义判断
    能力了，给「存疑」不如给「规则能确定的部分」并明确标注降级。
    """
    store.save(principle())
    settings = _settings(jev_api_key="k")
    transport = lambda *a, **k: (500, "gateway down")  # noqa: E731
    judgement = Judge(settings, store).judge(
        "目前基本没问题。", backend="jev", transport=transport, save=False
    )
    assert judgement.backend == "rules"
    assert judgement.overall == "violation"
    assert any("jev 后端失败" in note for note in judgement.notes)
    assert any("降级" in note or "语义判断缺席" in note for note in judgement.notes)


def test_judge_jev_failure_with_llm_configured_degrades_to_llm(store, principle):
    """JEV 挂了但 LLM 配好 → 降级 llm，不是 rules。"""
    store.save(principle())
    settings = _settings(jev_api_key="k", api_key="sk")

    def transport(url, headers, payload, timeout):  # noqa: ANN001
        if "systemone" in url:
            return 500, "gateway down"
        return 200, json.dumps(
            {
                "model": "deepseek-flash",
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "answers": {
                                        "p_001": {
                                            "type": "choice",
                                            "choice": "violation",
                                            "probabilities": {"pass": 0.05, "violation": 0.9},
                                        }
                                    }
                                }
                            )
                        }
                    }
                ],
            }
        )

    judgement = Judge(settings, store).judge(
        "目前基本没问题。", backend="jev", transport=transport, save=False
    )
    assert judgement.backend == "llm"
    assert any("降级到 llm" in note for note in judgement.notes)


def test_judge_rule_hit_does_not_override_jev_pass(store):
    """**规则与 JEV 冲突**：规则命中但 JEV 判 pass —— 整体结论以 JEV 为准。"""
    from openagent_principle.models import Rule

    store.save(
        Principle(
            id="P-001",
            title="不粉饰",
            statement="不得用模糊措辞淡化风险",
            severity=3,
            rules=[Rule(kind="must_not_contain_any", value=["基本没问题"])],
        )
    )

    def transport(url, headers, payload, timeout):  # noqa: ANN001
        return 200, json.dumps(
            {
                "answers": {
                    "p_001": {
                        "type": "choice",
                        "choice": "pass",
                        "probabilities": {"pass": 0.9, "violation": 0.1},
                        "confidence": 0.85,
                    },
                    "overall": {"type": "choice", "choice": "pass"},
                }
            }
        )

    judgement = Judge(_settings(jev_api_key="k"), store).judge(
        "目前基本没问题。", backend="jev", transport=transport, save=False
    )
    assert judgement.verdicts[0].violated is False, "JEV 说 pass，规则不能推翻"
    assert judgement.overall == "pass"
    assert "规则层有未通过项" in judgement.verdicts[0].reasoning
    assert any("基本没问题" in e for e in judgement.verdicts[0].evidence)


def test_judge_rule_pass_does_not_block_jev_violation(store):
    """反向冲突：规则全部通过，JEV 判 violation —— 以 JEV 为准。"""
    from openagent_principle.models import Rule

    store.save(
        Principle(
            id="P-001",
            title="复盘要有改进",
            statement="复盘必须给改进项",
            severity=4,
            rules=[Rule(kind="must_contain_any", value=["改进"])],
        )
    )

    def transport(url, headers, payload, timeout):  # noqa: ANN001
        return 200, json.dumps(
            {
                "answers": {
                    "p_001": {
                        "type": "choice",
                        "choice": "violation",
                        "probabilities": {"pass": 0.1, "violation": 0.85},
                        "confidence": 0.8,
                    },
                    "overall": {"type": "choice", "choice": "violation"},
                }
            }
        )

    judgement = Judge(_settings(jev_api_key="k"), store).judge(
        "复盘：改进措施是下次注意。", backend="jev", transport=transport, save=False
    )
    assert judgement.verdicts[0].violated is True
    assert "规则层全部通过" in judgement.verdicts[0].reasoning


def test_judge_overall_follows_jev_when_it_disagrees_with_per_verdict(store):
    """**逐条与整体冲突**：JEV 逐条说 pass、整体说 violation —— 整体以 JEV 为准。"""
    store.save(Principle(id="P-001", title="坦诚", statement="要说实话"))

    def transport(url, headers, payload, timeout):  # noqa: ANN001
        return 200, json.dumps(
            {
                "answers": {
                    "p_001": {
                        "type": "choice",
                        "choice": "pass",
                        "probabilities": {"pass": 0.95, "violation": 0.05},
                    },
                    "overall": {"type": "choice", "choice": "violation"},
                }
            }
        )

    judgement = Judge(_settings(jev_api_key="k"), store).judge(
        "材料", backend="jev", transport=transport, save=False
    )
    assert judgement.overall == "violation", "整体以 JEV 的全局答案为准"
    assert any("不一致" in note for note in judgement.notes), "冲突必须留痕"


def test_judge_overall_derived_when_jev_gives_no_global_answer(store):
    """JEV 没给全局答案 → 由逐条推导，并写明原因。"""
    store.save(Principle(id="P-001", title="坦诚", statement="要说实话"))

    def transport(url, headers, payload, timeout):  # noqa: ANN001
        return 200, json.dumps(
            {
                "answers": {
                    "p_001": {
                        "type": "choice",
                        "choice": "violation",
                        "probabilities": {"pass": 0.05, "violation": 0.9},
                    }
                }
            }
        )

    judgement = Judge(_settings(jev_api_key="k"), store).judge(
        "材料", backend="jev", transport=transport, save=False
    )
    assert judgement.overall == "violation"
    assert any("没有给出全局结论" in note for note in judgement.notes)


def test_judge_uses_model_answers_when_available(store):
    principle = Principle(id="P-001", title="坦诚", statement="要说实话", severity=4)
    store.save(principle)
    settings = _settings(jev_api_key="k")

    def transport(url, headers, payload, timeout):  # noqa: ANN001
        # 按请求里真实的 key 作答，证明 key_map 的映射是对的
        keys = list(payload["questions"])
        assert "p_001" in keys
        return 200, json.dumps(
            {
                "model": "jev-1.13.0",
                "answers": {
                    "p_001": {
                        "type": "choice",
                        "choice": "violation",
                        "probabilities": {"pass": 0.05, "violation": 0.82, "uncertain": 0.13},
                        "confidence": 0.77,
                    },
                    "overall": {"type": "choice", "choice": "violation", "confidence": 0.7},
                },
                "usage": {"cost": 0.001},
            }
        )

    judgement = Judge(settings, store).judge(
        "我保证这个方案百分之百没问题。", backend="jev", transport=transport, save=False
    )
    assert judgement.backend == "jev"
    assert judgement.model == "jev-1.13.0"
    assert judgement.verdicts[0].violated is True
    assert judgement.verdicts[0].probability == pytest.approx(0.82)
    assert judgement.verdicts[0].confidence == pytest.approx(0.77)
    assert judgement.usage["cost"] == pytest.approx(0.001)


def test_judge_choice_uncertain_is_not_forced_into_a_verdict(store):
    """模型说「不确定」时，结论就该是存疑 —— 不能被阈值掰成违反或没事。"""
    store.save(Principle(id="P-001", title="坦诚", statement="要说实话"))

    def transport(url, headers, payload, timeout):  # noqa: ANN001
        return 200, json.dumps(
            {
                "answers": {
                    "p_001": {
                        "type": "choice",
                        "choice": "uncertain",
                        "probabilities": {"pass": 0.2, "violation": 0.3, "uncertain": 0.5},
                        "confidence": 0.6,
                    }
                }
            }
        )

    judgement = Judge(_settings(jev_api_key="k"), store).judge(
        "改了一下配置文件的默认值。", backend="jev", transport=transport, save=False
    )
    assert judgement.overall == "uncertain"
    assert judgement.verdicts[0].violated is None
    assert judgement.verdicts[0].probability == pytest.approx(0.5)


def test_judge_rejects_noul_answers(store):
    """`noul` 已从判断层移除：解析层直接跳过它，模型层等于没给答案。

    结果必须是「无法判断」，而不是被阈值掰成违反或没事。
    """
    store.save(Principle(id="P-001", title="坦诚", statement="要说实话"))

    def transport(url, headers, payload, timeout):  # noqa: ANN001
        return 200, json.dumps({"answers": {"p_001": {"type": "noul", "noul": 0.91}}})

    judgement = Judge(_settings(jev_api_key="k"), store).judge(
        "材料", backend="jev", transport=transport, save=False
    )
    assert judgement.verdicts[0].violated is None
    assert judgement.overall == "uncertain"
    assert "无法判断" in judgement.verdicts[0].reasoning


def test_judge_rejects_empty_material(store):
    with pytest.raises(JudgeError):
        Judge(_settings(), store).judge("   ")


def test_rule_report_and_merge_agree_on_severity(principle):
    from openagent_principle.rules import rule_report

    p = principle()
    verdict = merge(p, rule_report(p, "基本没问题"), None, backend="rules")
    assert verdict.severity == p.severity


def test_build_questions_key_survives_store_roundtrip(store, principle):
    saved = store.save(principle())
    questions, key_map = jev.build_questions([saved])
    assert key_map["p_001"] == saved.id


def test_build_questions_embeds_rule_report(store, principle):
    """规则报告必须作为结构化输入写进 JEV 的 instructions。"""
    from openagent_principle.rules import rule_report

    saved = store.save(principle())
    report = rule_report(saved, "目前基本没问题。")
    questions, _ = jev.build_questions([saved], {saved.id: report})
    instructions = questions["p_001"]["instructions"]
    assert "【规则层" in instructions
    assert "基本没问题" in instructions
    assert "不等于" in instructions, "必须明确告诉 JEV：规则未通过 ≠ 违反"
