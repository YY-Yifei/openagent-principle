"""配置层：非法输入必须退回默认，而不是让程序起不来。"""

from __future__ import annotations

import pytest

from openagent_principle.config import (
    DEFAULT_BASE_URL,
    DEFAULT_JEV_BASE_URL,
    DEFAULT_JEV_PATH,
    DEFAULT_MODEL,
    env_float,
    env_int,
    first_env,
    load_settings,
)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("7", 7), (" 7 ", 7), ("abc", 5), ("", 5), ("-1", 5), ("0", 5), (None, 5)],
)
def test_env_int_falls_back_on_bad_input(monkeypatch, raw, expected):
    if raw is None:
        monkeypatch.delenv("X_INT", raising=False)
    else:
        monkeypatch.setenv("X_INT", raw)
    assert env_int("X_INT", 5) == expected


def test_env_int_allows_zero_when_minimum_is_zero(monkeypatch):
    monkeypatch.setenv("X_INT", "0")
    assert env_int("X_INT", 9, minimum=0) == 0


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("0.7", 0.7), ("abc", 0.3), ("9", 0.3), ("-1", 0.3), ("", 0.3)],
)
def test_env_float_clamps(monkeypatch, raw, expected):
    monkeypatch.setenv("X_FLOAT", raw)
    assert env_float("X_FLOAT", 0.3) == pytest.approx(expected)


def test_first_env_takes_first_non_empty(monkeypatch):
    monkeypatch.setenv("A_KEY", "")
    monkeypatch.setenv("B_KEY", "b")
    monkeypatch.setenv("C_KEY", "c")
    assert first_env("A_KEY", "B_KEY", "C_KEY") == "b"
    assert first_env("MISSING", "ALSO_MISSING") == ""


def test_defaults_when_nothing_configured(env):
    settings = load_settings()
    assert settings.api_key == ""
    assert settings.base_url == DEFAULT_BASE_URL
    assert settings.model == DEFAULT_MODEL
    assert settings.judge_backend == "auto"
    assert settings.jev_base_url == DEFAULT_JEV_BASE_URL
    assert settings.jev_path == DEFAULT_JEV_PATH
    assert settings.jev_configured is False
    assert settings.llm_configured is False
    assert settings.home_path.name == "home"  # 夹具指到 tmp_path/home


def test_key_fallbacks_are_explicit(env, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "ds")
    settings = load_settings()
    assert settings.api_key == "ds"

    monkeypatch.setenv("PRINCIPLE_API_KEY", "own")
    assert load_settings().api_key == "own"  # 自己的名字优先


def test_jev_key_falls_back_to_openai(env, monkeypatch):
    # 有些部署把 JEV token 就放在 OPENAI_API_KEY 里
    monkeypatch.setenv("OPENAI_API_KEY", "company-token")
    monkeypatch.setenv("JEV_BASE_URL", "https://jev.example")
    settings = load_settings()
    assert settings.jev_api_key == "company-token"
    assert settings.jev_configured is True


def test_jev_not_configured_without_base_url(env, monkeypatch):
    """没有默认网关地址：只给 key 不算配好，必须显式给 JEV_BASE_URL。"""
    monkeypatch.setenv("JEV_API_KEY", "k")
    settings = load_settings()
    assert settings.jev_base_url == ""
    assert settings.jev_configured is False


def test_unknown_backend_falls_back_to_auto(env, monkeypatch):
    monkeypatch.setenv("PRINCIPLE_JUDGE_BACKEND", "magic")
    assert load_settings().judge_backend == "auto"


def test_overrides_beat_environment(env, monkeypatch):
    monkeypatch.setenv("PRINCIPLE_MODEL", "from-env")
    settings = load_settings(model="from-arg", temperature=0.9)
    assert settings.model == "from-arg"
    assert settings.temperature == pytest.approx(0.9)


def test_home_expands_user(env, monkeypatch):
    monkeypatch.setenv("PRINCIPLE_HOME", "~/.somewhere")
    assert str(load_settings().home_path).startswith(str(load_settings().home_path.expanduser()))


def test_stream_can_be_disabled(env, monkeypatch):
    monkeypatch.setenv("OPENAGENT_STREAM", "0")
    assert load_settings().stream is False
