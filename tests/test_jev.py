"""JEV 协议：题型构造、答案解析、请求行为（全部离线）。"""

from __future__ import annotations

import json

import pytest

from openagent_principle import jev
from openagent_principle.config import load_settings
from conftest import fake_transport


def _settings(**overrides):
    base = {
        "jev_api_key": "test-key",
        "jev_base_url": "https://jev.example",
        "jev_path": "/systemone/v1/systemone",
        "jev_timeout": 1.0,
    }
    base.update(overrides)
    return load_settings(**base)


def test_build_questions_has_one_choice_per_principle(principle):
    """每条原则一道 choice（pass / violation / uncertain），不再是二态的 noul。

    二态题型没有「不确定」这个出口 —— 模型想说的「证据不够」无处表达，
    只能被压成一个概率再由调用方切阈值。三态由题型直接承载。
    """
    questions, key_map = jev.build_questions([principle(), principle(id="P-002", title="另一条")])
    assert questions["p_001"]["type"] == "choice"
    assert questions["p_002"]["type"] == "choice"
    assert set(questions["p_001"]["criteria"]) == {"pass", "violation", "uncertain"}
    assert questions["overall"]["type"] == "choice"
    assert questions["severity"]["type"] == "score"
    assert key_map == {"p_001": "P-001", "p_002": "P-002"}
    # 材料是数据的提醒必须出现在每条说明里
    assert jev.DATA_NOT_INSTRUCTIONS in questions["p_001"]["instructions"]
    assert "不粉饰风险" in questions["p_001"]["instructions"]


def test_principle_instruction_does_not_push_uncertain_toward_no(principle):
    """说明里不能出现「信息不足就回答否」—— 那会把「不知道」伪装成「没事」。"""
    text = jev.principle_instruction(principle())
    assert "信息不足以下判断时，回答「否」" not in text
    assert "选 uncertain" in text


def test_question_keys_are_unique_even_for_weird_ids():
    from openagent_principle.models import Principle

    weird = [
        Principle(id="P-1", title="a", statement="s"),
        Principle(id="P_1", title="b", statement="s"),
    ]
    questions, key_map = jev.build_questions(weird)
    assert len(key_map) == 2
    assert set(key_map.values()) == {"P-1", "P_1"}


def test_validate_questions_rejects_bad_shapes():
    with pytest.raises(ValueError):
        jev.validate_questions({})
    with pytest.raises(ValueError):
        jev.validate_questions({"q": {"type": "bogus"}})
    with pytest.raises(ValueError):
        jev.validate_questions({"q": {"type": "choice"}})
    with pytest.raises(ValueError):
        jev.validate_questions({"q": {"type": "score", "criteria": []}})


def test_parse_answers_matches_real_response_shape():
    """形状照抄 JEV 的真实返回（choice + score 两种题型）。"""
    raw = {
        "team": {"type": "choice", "choice": "billing", "probabilities": {"billing": 0.84, "technical": 0.16}, "confidence": 0.76},
        "frustration": {
            "type": "score",
            "score": 1.09,
            "legend": {"0": "平静", "1": "不满", "2": "非常愤怒"},
            "probabilities": {"0": 0, "1": 0.91, "2": 0.09},
            "confidence": 0.86,
        },
    }
    answers = jev.parse_answers(raw)
    assert answers["team"].value == "billing"
    assert answers["team"].probability == pytest.approx(0.84)
    assert answers["frustration"].value == pytest.approx(1.09)
    assert answers["frustration"].legend["1"] == "不满"
    assert answers["frustration"].confidence == pytest.approx(0.86)


def test_parse_answers_skips_noul():
    """`noul` 已从判断层移除 —— 解析层不再认它，坏掉的题直接跳过。"""
    answers = jev.parse_answers(
        {
            "answers": {
                "p_001": {"type": "noul", "noul": 0.8},
                "p_002": {"type": "choice", "choice": "violation", "confidence": 0.7, "reason": "出现了「基本没问题」"},
                "p_003": {"type": "unknown"},
            }
        }
    )
    assert list(answers) == ["p_002"]
    assert answers["p_002"].reason.startswith("出现了")


def test_parse_answers_tolerates_whole_response_and_bad_items():
    answers = jev.parse_answers(
        {
            "answers": {
                "p_001": {"type": "choice", "choice": "violation", "confidence": 0.7, "reason": "出现了「基本没问题」"},
                "p_002": {"type": "choice"},
                "p_003": {"type": "unknown"},
            }
        }
    )
    assert list(answers) == ["p_001"]
    assert answers["p_001"].reason.startswith("出现了")


def test_ask_posts_expected_payload_and_parses(principle):
    captured = {}

    def transport(url, headers, payload, timeout):  # noqa: ANN001
        captured.update({"url": url, "headers": headers, "payload": payload, "timeout": timeout})
        return 200, json.dumps(
            {
                "model": "jev-1.13.0",
                "answers": {
                    "p_001": {"type": "choice", "choice": "violation", "probabilities": {"pass": 0.05, "violation": 0.91, "uncertain": 0.04}, "confidence": 0.88},
                    "overall": {"type": "choice", "choice": "violation", "confidence": 0.8},
                    "severity": {"type": "score", "score": 3.4, "confidence": 0.7},
                },
                "usage": {"cost": 0.00002},
            },
            ensure_ascii=False,
        )

    questions, _ = jev.build_questions([principle()])
    response = jev.ask("目前基本没问题。", questions, settings=_settings(), transport=transport)

    assert captured["url"] == "https://jev.example/systemone/v1/systemone"
    assert captured["payload"]["model"] == "jev-1.13.0"
    assert captured["payload"]["state"] == "目前基本没问题。"
    assert captured["headers"]["Authorization"] == "Bearer test-key"
    assert response.model == "jev-1.13.0"
    assert response.answers["p_001"].value == "violation"
    assert response.answers["p_001"].probability == pytest.approx(0.91)
    assert response.usage["cost"] == pytest.approx(0.00002)


def test_ask_requires_key(env, principle):
    questions, _ = jev.build_questions([principle()])
    settings = load_settings(jev_api_key="", api_key="", jev_base_url="https://jev.example")
    with pytest.raises(jev.JevError) as exc:
        jev.ask("材料", questions, settings=settings)
    assert "JEV_API_KEY" in str(exc.value)


def test_ask_401_is_not_retried(principle):
    calls = []

    def transport(url, headers, payload, timeout):  # noqa: ANN001
        calls.append(1)
        return 401, '{"error":{"message":"invalid key"}}'

    questions, _ = jev.build_questions([principle()])
    with pytest.raises(jev.JevError) as exc:
        jev.ask("材料", questions, settings=_settings(), transport=transport, retries=2)
    assert len(calls) == 1
    assert exc.value.status == 401
    assert "invalid key" in exc.value.message


def test_ask_retries_then_succeeds(principle):
    calls = []

    def transport(url, headers, payload, timeout):  # noqa: ANN001
        calls.append(1)
        if len(calls) == 1:
            return 500, "boom"
        return 200, '{"answers":{"p_001":{"type":"choice","choice":"pass","probabilities":{"pass":0.8,"violation":0.2}}}}'

    questions, _ = jev.build_questions([principle()])
    response = jev.ask("材料", questions, settings=_settings(), transport=transport, retries=2)
    assert len(calls) == 2
    assert response.answers["p_001"].value == "pass"


def test_ask_rejects_empty_state(principle):
    questions, _ = jev.build_questions([principle()])
    with pytest.raises(jev.JevError):
        jev.ask("   ", questions, settings=_settings())


def test_ask_reports_missing_answers(principle):
    questions, _ = jev.build_questions([principle()])
    transport = fake_transport(200, '{"model":"jev","answers":{}}')
    with pytest.raises(jev.JevError) as exc:
        jev.ask("材料", questions, settings=_settings(), transport=transport)
    assert "answers" in str(exc.value)


def test_ask_reports_non_json(principle):
    questions, _ = jev.build_questions([principle()])
    transport = fake_transport(200, "<html>gateway</html>")
    with pytest.raises(jev.JevError):
        jev.ask("材料", questions, settings=_settings(), transport=transport)


def test_ask_retries_non_json_200_then_succeeds(principle):
    """200 但响应不是 JSON（网关吐 HTML 错误页）要重试，而不是直接降级。"""
    calls = []

    def transport(url, headers, payload, timeout):  # noqa: ANN001
        calls.append(1)
        if len(calls) == 1:
            return 200, "<html>gateway</html>"
        return 200, '{"answers":{"p_001":{"type":"choice","choice":"pass","probabilities":{"pass":0.8}}}}'

    questions, _ = jev.build_questions([principle()])
    response = jev.ask("材料", questions, settings=_settings(), transport=transport)
    assert len(calls) == 2
    assert response.answers["p_001"].value == "pass"


def test_ask_retries_missing_answers_200_then_succeeds(principle):
    """200 但 answers 为空 / 解析不出答案，同样按可重试处理。"""
    calls = []

    def transport(url, headers, payload, timeout):  # noqa: ANN001
        calls.append(1)
        if len(calls) == 1:
            return 200, '{"model":"jev","answers":{}}'
        if len(calls) == 2:
            return 200, '{"answers":{"p_001":{"type":"noul","probabilities":{"x":0.5}}}}'
        return 200, '{"answers":{"p_001":{"type":"choice","choice":"violation","probabilities":{"violation":0.9}}}}'

    questions, _ = jev.build_questions([principle()])
    response = jev.ask("材料", questions, settings=_settings(), transport=transport)
    assert len(calls) == 3
    assert response.answers["p_001"].value == "violation"


def test_ask_default_retries_is_three(principle):
    """默认重试 3 次 = 最多 4 次请求；耗尽后抛可重试的 JevError。"""
    assert jev.DEFAULT_RETRIES == 3
    calls = []

    def transport(url, headers, payload, timeout):  # noqa: ANN001
        calls.append(1)
        return 200, "<html>gateway</html>"

    questions, _ = jev.build_questions([principle()])
    with pytest.raises(jev.JevError) as exc:
        jev.ask("材料", questions, settings=_settings(), transport=transport)
    assert len(calls) == 4
    assert exc.value.retryable is True
    assert "不是 JSON" in exc.value.message
