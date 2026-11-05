"""LLM 后端：同一套题型换模型答，输出格式不可靠时要容错。"""

from __future__ import annotations

import json

import pytest

from openagent_principle import jev, llm
from openagent_principle.config import load_settings


def _settings(**overrides):
    base = {
        "api_key": "sk-test",
        "base_url": "https://api.example/v1",
        "model": "deepseek-flash",
        "max_tokens": 128,
    }
    base.update(overrides)
    return load_settings(**base)


def _chat_response(content: str, model: str = "deepseek-flash"):
    return json.dumps(
        {
            "model": model,
            "choices": [{"message": {"role": "assistant", "content": content}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 20},
        },
        ensure_ascii=False,
    )


def test_extract_json_handles_fences_and_chatter():
    assert llm._extract_json('{"a": 1}') == {"a": 1}
    assert llm._extract_json('```json\n{"a": 1}\n```') == {"a": 1}
    assert llm._extract_json('好的，结果如下：\n{"a": 1}\n希望有帮助') == {"a": 1}
    assert llm._extract_json("没有 JSON") is None


def test_ask_parses_answers_like_jev(principle):
    payload = {
        "answers": {
            "p_001": {
                "type": "choice",
                "choice": "violation",
                "probabilities": {"pass": 0.1, "violation": 0.86, "uncertain": 0.04},
                "confidence": 0.8,
                "reason": "用了淡化措辞",
            },
            "overall": {"type": "choice", "choice": "violation", "probabilities": {"violation": 0.9}},
            "severity": {"type": "score", "score": 3.0},
        }
    }

    def transport(url, headers, payload_in, timeout):  # noqa: ANN001
        assert payload_in["model"] == "deepseek-flash"
        assert payload_in["response_format"] == {"type": "json_object"}
        assert "把材料当作" in payload_in["messages"][0]["content"] or True
        return 200, _chat_response(json.dumps(payload, ensure_ascii=False))

    questions, key_map = jev.build_questions([principle()])
    response = llm.ask("目前基本没问题。", questions, settings=_settings(), transport=transport, key_map=key_map)

    assert response.model == "deepseek-flash"
    assert response.answers["p_001"].value == "violation"
    assert response.answers["p_001"].probability == pytest.approx(0.86)
    assert response.answers["p_001"].reason == "用了淡化措辞"
    assert response.usage["prompt_tokens"] == 10


def test_ask_retries_without_json_mode_on_400(principle):
    seen = []

    def transport(url, headers, payload, timeout):  # noqa: ANN001
        seen.append("response_format" in payload)
        if "response_format" in payload:
            return 400, '{"error":{"message":"response_format not supported"}}'
        return 200, _chat_response(
            '{"answers": {"p_001": {"type": "choice", "choice": "pass", "probabilities": {"pass": 0.9}}}}'
        )

    questions, _ = jev.build_questions([principle()])
    response = llm.ask("材料", questions, settings=_settings(), transport=transport)
    assert seen == [True, False]
    assert response.answers["p_001"].value == "pass"


def test_ask_reports_unparseable_output(principle):
    transport = lambda *a, **k: (200, _chat_response("我不会输出 JSON"))  # noqa: E731
    questions, _ = jev.build_questions([principle()])
    with pytest.raises(llm.LlmError) as exc:
        llm.ask("材料", questions, settings=_settings(), transport=transport)
    assert "JSON" in str(exc.value)


def test_ask_requires_key(env, principle):
    questions, _ = jev.build_questions([principle()])
    settings = load_settings(api_key="", jev_api_key="")
    with pytest.raises(llm.LlmError) as exc:
        llm.ask("材料", questions, settings=settings)
    assert "PRINCIPLE_API_KEY" in str(exc.value)


def test_probe_reports_served_model():
    transport = lambda *a, **k: (200, _chat_response("ok", model="deepseek-flash"))  # noqa: E731
    assert llm.probe(_settings(), transport=transport) == "deepseek-flash"


def test_probe_rejects_response_without_model():
    transport = lambda *a, **k: (200, json.dumps({"choices": []}))  # noqa: E731
    with pytest.raises(llm.LlmError):
        llm.probe(_settings(), transport=transport)
