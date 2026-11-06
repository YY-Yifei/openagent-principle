"""测试公共夹具：隔离环境变量 + 临时原则库。

为什么每个用例都要清环境变量：开发机上 `DEEPSEEK_API_KEY` / `OPENAI_API_KEY`
大概率是真的存在的。不清掉的话，「没配 key 时判断不可用」这类用例会
变成**真的发请求**，测试既慢又会随网络抖。
"""

from __future__ import annotations

import pytest

#: 本项目会读的全部环境变量 —— 测试里一律清掉，需要时再显式设。
ENV_KEYS = [
    "PRINCIPLE_API_KEY",
    "PRINCIPLE_BASE_URL",
    "PRINCIPLE_MODEL",
    "PRINCIPLE_CONTEXT_SIZE",
    "PRINCIPLE_TEMPERATURE",
    "PRINCIPLE_MAX_TOKENS",
    "PRINCIPLE_MAX_ITERS",
    "PRINCIPLE_JUDGE_BACKEND",
    "PRINCIPLE_HOME",
    "PRINCIPLE_MAX_PRINCIPLES_PER_JUDGE",
    "PRINCIPLE_MAX_MATERIAL_CHARS",
    "PRINCIPLE_MAX_JUDGE_CALLS",
    "PRINCIPLE_MAX_FETCH_CALLS",
    "PRINCIPLE_MAX_READ_CALLS",
    "PRINCIPLE_MAX_READ_CHARS",
    "PRINCIPLE_TOOLSET",
    "PRINCIPLE_WORKSPACE",
    "PRINCIPLE_ALLOW_SHELL",
    "PRINCIPLE_PERMISSION",
    "DEEPSEEK_API_KEY",
    "DEEPSEEK_BASE_URL",
    "OPENAI_API_KEY",
    "OPENAI_BASE_URL",
    "JEV_API_KEY",
    "JEV_BASE_URL",
    "JEV_MODEL",
    "JEV_PATH",
    "JEV_TIMEOUT",
    "OPENAGENT_STREAM",
]


@pytest.fixture
def env(tmp_path, monkeypatch):
    """清空相关环境变量，并把原则库指到临时目录。"""
    for key in ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("PRINCIPLE_HOME", str(tmp_path / "home"))
    return tmp_path


@pytest.fixture
def store(env):
    from openagent_principle.store import PrincipleStore

    return PrincipleStore(env / "home")


@pytest.fixture
def principle():
    """一条可判定的原则 —— 大多数用例的起点。"""

    def _make(**overrides):
        from openagent_principle.models import Principle

        data = {
            "id": "P-001",
            "title": "不粉饰风险",
            "statement": "描述问题时不得用无数据支撑的淡化措辞。",
            "criteria": "出现「基本没问题」「影响不大」这类话且没有数据支撑，算违反。",
            "examples": ["反例：目前基本没问题。"],
            "severity": 4,
            "tags": ["风险"],
        }
        data.update(overrides)
        return Principle(**data)

    return _make


def fake_transport(status: int, body: str):
    """固定返回的传输层：`(url, headers, payload, timeout) -> (status, body)`。"""

    def _send(url, headers, payload, timeout):  # noqa: ANN001, ARG001
        return status, body

    return _send
