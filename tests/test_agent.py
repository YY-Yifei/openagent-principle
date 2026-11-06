"""Agent 装配：两层工具箱、能力边界、预算护栏（全部离线，不需要网络）。

0.3.0 的验收重点：**它不再是「只能判原则」的 agent**。
所以这里的用例主要钉三件事：
1. 默认（full）真的装上了通用能力 —— 能读、能写、能跑、能搜；
2. 收窄是**显式**的，且收得干净（domain 只有 11 个域工具，readonly 没有写入与 shell）；
3. 预算默认**不限**，只在显式配置时才拦 —— 拦也要拦得只拦计费工具。
"""

from __future__ import annotations

import asyncio

import pytest

pytest.importorskip("agentscope")

from openagent_principle.agent import (  # noqa: E402
    DOMAIN_TOOLS,
    READ_ONLY_DOMAIN,
    ToolBudget,
    build_agent,
    build_toolkit,
    describe,
    resolve_toolset,
    tool_names,
)
from openagent_principle.config import load_settings  # noqa: E402


def _schemas(settings=None) -> dict[str, dict]:
    toolkit = build_toolkit(settings or load_settings(api_key="sk-test"))
    return {s["function"]["name"]: s["function"] for s in asyncio.run(toolkit.get_tool_schemas())}


# ---------------------------------------------------------------------------
# 能力层：默认全能力
# ---------------------------------------------------------------------------
def test_default_toolset_is_full(env):
    settings = load_settings(api_key="sk-test")
    assert settings.toolset == "full"
    assert resolve_toolset(settings) == "full"


def test_full_toolkit_has_general_capabilities(env):
    """读、写、找、跑 —— 少一样它就不能独立干活。"""
    names = set(_schemas(load_settings(api_key="sk-test")))
    assert set(DOMAIN_TOOLS) <= names, "原则层必须完整"
    assert {"Read", "Write", "Edit", "Glob", "search_files", "list_dir"} <= names
    assert names & {"Bash", "PowerShell"}, "至少要有一个能执行命令的 shell"


def test_tool_names_matches_real_schemas(env):
    settings = load_settings(api_key="sk-test")
    assert set(tool_names(settings)) == set(_schemas(settings))


def test_schema_keeps_parameters_from_signature(env):
    schemas = _schemas(load_settings(api_key="sk-test"))
    properties = schemas["judge_material"]["parameters"]["properties"]
    assert set(properties) == {"material", "source", "principle_ids"}
    assert schemas["judge_material"]["description"].startswith("判断一段材料是否违反原则")


# ---------------------------------------------------------------------------
# 收窄：显式、干净
# ---------------------------------------------------------------------------
def test_domain_toolset_is_principles_only(env):
    """domain = 全部 10 个域工具，不装通用层（OCR 复审发现回归）。

    之前误写成 `read_only_only=toolset != "full"`，把 domain 也砍成 5 个只读工具，
    `judge_material` / `save_principle` 凭空消失 —— 那是能力回归，不是「只判原则」。
    """
    settings = load_settings(api_key="sk-test", toolset="domain")
    names = set(_schemas(settings))
    assert names == set(DOMAIN_TOOLS), "domain 应装全部域工具"
    assert "judge_material" in names and "save_principle" in names
    # 不装通用层
    assert not names & {"Read", "Glob", "Write", "Edit", "Bash", "PowerShell"}


def test_tool_names_matches_build_toolkit(env):
    """banner 报的工具名必须与实际装的一致（OCR 复审发现）。"""
    for toolset in ("full", "readonly", "domain"):
        settings = load_settings(api_key="sk-test", toolset=toolset)
        assert set(tool_names(settings)) == set(_schemas(settings)), toolset


def test_readonly_toolset_drops_write_and_shell(env):
    settings = load_settings(api_key="sk-test", toolset="readonly")
    names = set(_schemas(settings))
    assert set(READ_ONLY_DOMAIN) <= names
    assert {"Read", "Glob", "search_files", "list_dir"} <= names
    assert not names & {"Write", "Edit", "Bash", "PowerShell"}, "只读模式不许有写入与执行"
    # 复审 5.1：只读模式也不许有写域工具
    assert not names & {"save_principle", "delete_principle", "judge_material",
                        "judge_file", "record_case_feedback"}


def test_allow_shell_false_downgrades_full_to_readonly(env):
    settings = load_settings(api_key="sk-test", allow_shell=False)
    assert resolve_toolset(settings) == "readonly"
    names = set(_schemas(settings))
    assert "Bash" not in names and "PowerShell" not in names and "Write" not in names


def test_unknown_toolset_falls_back_to_readonly(env, monkeypatch):
    """安全配置写错必须往「更严」的方向失败（复审 4.4）。

    以前回退 `full` —— 拼错一个字母就重新获得写文件和跑命令的能力。
    """
    monkeypatch.setenv("PRINCIPLE_TOOLSET", "typo")
    settings = load_settings(api_key="sk-test")
    # config 层仍把非法值归一成 full（保持 Settings 字段合法），
    # 但 resolve_toolset 必须把它降成 readonly。
    assert resolve_toolset(settings) == "readonly"
    names = set(_schemas(settings))
    assert "Write" not in names and "Bash" not in names and "PowerShell" not in names


def test_toolset_from_env(env, monkeypatch):
    monkeypatch.setenv("PRINCIPLE_TOOLSET", "readonly")
    assert load_settings(api_key="sk-test").toolset == "readonly"


# ---------------------------------------------------------------------------
# 预算：默认不限
# ---------------------------------------------------------------------------
def test_budget_defaults_to_unlimited(env):
    budget = ToolBudget()
    assert budget.max_judge == 0
    for _ in range(50):
        assert budget.take("judge_material") is True
    assert "不限" in budget.report()


def test_budget_only_counts_metered_tools():
    """读文件 / 抓网页 / 跑命令不该被计数 —— 本地资源不心疼。"""
    budget = ToolBudget(max_judge=0)
    assert budget.take("Bash") is True
    assert budget.take("Read") is True
    assert budget.judges == 0 and budget.reads == 0


def test_budget_exhausts_when_configured():
    budget = ToolBudget(max_judge=2, max_fetch=1, max_read=1)
    assert budget.take("judge_material") is True
    assert budget.take("judge_file") is True  # 同一个预算桶
    assert budget.take("judge_material") is False
    message = budget.exhausted("judge_material")
    assert "上限" in message and "不计费" in message
    assert "PRINCIPLE_MAX_JUDGE_CALLS=0" in message, "要告诉模型怎么解除"


def test_budget_reset():
    budget = ToolBudget(max_judge=1)
    budget.take("judge_material")
    budget.reset()
    assert budget.take("judge_material") is True


def test_judge_budget_from_env(env, monkeypatch):
    monkeypatch.setenv("PRINCIPLE_MAX_JUDGE_CALLS", "3")
    settings = load_settings(api_key="sk-test")
    assert settings.max_judge_calls == 3
    assert build_agent(settings).tool_budget.max_judge == 3


def test_zero_budget_env_means_unlimited(env, monkeypatch):
    """`0` 必须被当成「不限」，而不是被 `or` 吃掉退回默认 —— 这是最容易写错的边界。"""
    monkeypatch.setenv("PRINCIPLE_MAX_JUDGE_CALLS", "0")
    assert load_settings(api_key="sk-test").max_judge_calls == 0


# ---------------------------------------------------------------------------
# 装配结果
# ---------------------------------------------------------------------------
def test_build_agent_attaches_budget_and_bypass(env):
    settings = load_settings(api_key="sk-test", model="deepseek-flash")
    agent = build_agent(settings)
    assert agent.tool_budget.max_judge == settings.max_judge_calls
    assert agent.state.permission_context.mode.value == "bypass"
    assert "openagent-principle" in agent._system_prompt
    assert agent.toolset == "full"
    assert agent.workspace == str(settings.workspace_path)


def test_system_prompt_promises_general_ability(env):
    settings = load_settings(api_key="sk-test")
    agent = build_agent(settings)
    prompt = agent._system_prompt
    for word in ("通用 agent", "Bash", "Edit", "原则"):
        assert word in prompt
    assert "只做四件事" not in prompt, "旧提示词把它的能力写死成四件事"


def test_describe_mentions_backend_home_and_toolset(env):
    settings = load_settings()
    text = describe(settings)
    assert "判断后端" in text
    assert str(settings.home_path) in text
    assert "full" in text


def test_describe_says_backend_unavailable_without_keys(env):
    """没配任何模型时，自述要如实说「不可用」，而不是抛异常。"""
    text = describe(load_settings())
    assert "不可用" in text
