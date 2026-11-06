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
from openagent_principle.store import PrincipleStore


def _settings(**overrides):
    return load_settings(**overrides)


def _model_verdict(violated: bool | None, probability: float = 0.8, confidence: float = 0.8):
    return Verdict("P-001", "t", violated, probability, confidence, 4, [], "模型说的")


# --------------------------------------------------------------- 后端选择
def test_auto_without_keys_raises(env):
    """没有模型就判不了 —— 明确报错，不拿一个假装在判的降级糊弄人。"""
    with pytest.raises(JudgeError) as excinfo:
        choose_backend(_settings())
    assert "没有可用的判断后端" in str(excinfo.value)


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
# 架构：判断权完全在模型；程序只做矛盾检测与存疑处理。
def test_merge_adopts_model_pass(principle):
    verdict = merge(principle(), _model_verdict(False, 0.9, 0.85), backend="jev")
    assert verdict.violated is False
    assert verdict.source == "model"


def test_merge_adopts_model_violation(principle):
    verdict = merge(principle(), _model_verdict(True, 0.88, 0.8), backend="jev")
    assert verdict.violated is True
    assert verdict.source == "model"


def test_merge_keeps_model_uncertain(principle):
    """模型说存疑，程序不能把它改写成「未违反」。"""
    verdict = merge(principle(), _model_verdict(None, 0.5, 0.3), backend="jev")
    assert verdict.violated is None
    assert verdict.source == "model"


def test_merge_contradictory_pass_becomes_uncertain(principle):
    """选了 pass 但概率只有 0.1 —— 模型自相矛盾，降为存疑。"""
    verdict = merge(principle(), _model_verdict(False, 0.1, 0.9))
    assert verdict.violated is None
    assert "不一致" in verdict.reasoning


def test_merge_contradictory_violation_becomes_uncertain(principle):
    """对称检查：选了 violation 但概率只有 0.49，同样降为存疑。"""
    verdict = merge(principle(), _model_verdict(True, 0.49, 0.24))
    assert verdict.violated is None


def test_merge_without_model_answer_is_uncertain(principle):
    """**后端降级**：模型没给答案时，程序不能自己定论 —— 必须存疑。"""
    verdict = merge(principle(), None, backend="jev")
    assert verdict.violated is None
    assert "语义判断缺席" in verdict.reasoning


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
    merged = merge(p, verdict, backend="llm")
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


def test_merge_downgrades_self_contradicting_model_verdict(principle):
    """模型选了 violation，但自报概率 < 0.6 —— 自相矛盾，降成存疑。"""
    for probability, confidence in ((0.49, 0.24), (0.51, 0.26), (0.59, 0.4)):
        verdict = merge(principle(), _model_verdict(True, probability, confidence))
        assert verdict.violated is None, probability
        assert "不一致" in verdict.reasoning
        assert verdict.probability == pytest.approx(probability)


def test_merge_keeps_confident_model_violation(principle):
    verdict = merge(principle(), _model_verdict(True, 0.85, 0.77))
    assert verdict.violated is True


# --------------------------------------------------------------- 整条链路
def _jev_transport(answers: dict, model: str = "jev-1.13.0"):
    """固定返回给定 answers 的传输层。"""

    def _send(url, headers, payload, timeout):  # noqa: ANN001, ARG001
        return 200, json.dumps({"model": model, "answers": answers})

    return _send


def test_judge_with_jev_flags_violation(store, principle):
    store.save(principle())
    transport = _jev_transport(
        {
            "p_001": {
                "type": "choice",
                "choice": "violation",
                "probabilities": {"pass": 0.05, "violation": 0.9},
                "confidence": 0.85,
            },
            "overall": {"type": "choice", "choice": "violation"},
        }
    )
    judgement = Judge(_settings(jev_api_key="k"), store).judge(
        "目前基本没问题，客户还没发现。", backend="jev", transport=transport
    )
    assert judgement.overall == "violation"
    assert judgement.backend == "jev"
    assert judgement.severity == 4
    assert judgement.violations and judgement.violations[0].principle_id == "P-001"
    # 案例落盘了
    assert store.list_cases()[0].case_id == judgement.case_id


def test_judge_with_jev_passes_clean_material(store, principle):
    store.save(principle())
    transport = _jev_transport(
        {
            "p_001": {
                "type": "choice",
                "choice": "pass",
                "probabilities": {"pass": 0.95, "violation": 0.05},
            },
            "overall": {"type": "choice", "choice": "pass"},
        }
    )
    judgement = Judge(_settings(jev_api_key="k"), store).judge(
        "存在 2 个高优缺陷，若周五前不修复将影响 30% 用户下单。",
        backend="jev",
        transport=transport,
    )
    assert judgement.overall == "pass"
    assert judgement.score == 0.0


def test_judge_without_any_backend_raises(store, principle):
    """没配任何模型 → 判断不可用，明确报错。"""
    store.save(principle())
    with pytest.raises(JudgeError) as excinfo:
        Judge(_settings(), store).judge("任意材料")
    assert "没有可用的判断后端" in str(excinfo.value)


def test_judge_empty_library_is_uncertain_with_hint(store):
    judgement = Judge(_settings(jev_api_key="k"), store).judge("任意材料", backend="jev")
    assert judgement.overall == "uncertain"
    assert judgement.verdicts == []
    assert any("空" in note for note in judgement.notes)


def test_judge_empty_library_wins_over_missing_backend(store):
    """空原则库是「先去加原则」的结论，不能被「没有可用的判断后端」盖掉（OCR 复审发现）。

    以前 `choose_backend` 在空库检查之前调用，没配 key 时用户看到的是后端错误，
    而真正该做的事是加原则。
    """
    judgement = Judge(_settings(), store).judge("任意材料")
    assert judgement.overall == "uncertain"
    assert any("空" in note for note in judgement.notes)
    assert not any("没有可用的判断后端" in note for note in judgement.notes)


def test_judge_truncates_material_and_says_so(store, principle):
    store.save(principle())
    judge = Judge(_settings(max_material_chars=20, jev_api_key="k"), store)
    judgement = judge.judge(
        "基本没问题" + "啊" * 100, backend="jev", transport=_jev_transport({})
    )
    assert judgement.material_chars == 20
    assert any("只判前 20" in note for note in judgement.notes)


def test_judge_marks_uncertain_when_jev_fails_and_no_llm(store, principle):
    """**后端降级**：JEV 挂了、LLM 也没配 → 语义判断缺席，逐条一律存疑。"""
    store.save(principle())
    settings = _settings(jev_api_key="k")
    transport = lambda *a, **k: (500, "gateway down")  # noqa: E731
    judgement = Judge(settings, store).judge(
        "目前基本没问题。", backend="jev", transport=transport, save=False
    )
    assert judgement.backend == "jev"
    assert judgement.overall == "uncertain"
    assert judgement.verdicts[0].violated is None
    assert any("jev 后端失败" in note for note in judgement.notes)
    assert any("语义判断缺席" in note for note in judgement.notes)


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
    assert "语义判断缺席" in judgement.verdicts[0].reasoning


def test_judge_rejects_empty_material(store):
    with pytest.raises(JudgeError):
        Judge(_settings(), store).judge("   ")


def test_merge_keeps_principle_severity(principle):
    p = principle()
    verdict = merge(p, _model_verdict(True, 0.9, 0.8))
    assert verdict.severity == p.severity


def test_build_questions_key_survives_store_roundtrip(store, principle):
    saved = store.save(principle())
    questions, key_map = jev.build_questions([saved])
    assert key_map["p_001"] == saved.id


def test_build_questions_embeds_criteria_and_examples(store, principle):
    """原则的判定标准与反例必须写进 instructions —— 这是给模型的确定性输入。"""
    saved = store.save(principle())
    questions, _ = jev.build_questions([saved])
    instructions = questions["p_001"]["instructions"]
    assert "判定标准" in instructions
    assert "基本没问题" in instructions
    assert "反例" in instructions
