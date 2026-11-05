"""命令行：子命令行为 + 退出码语义。"""

from __future__ import annotations

import json

import pytest

from openagent_principle.cli import (
    EXIT_PASS,
    EXIT_UNCERTAIN,
    EXIT_VIOLATION,
    main,
)


def test_add_list_show(env, capsys):
    assert main(["add", "不粉饰", "不得用无依据的淡化措辞", "--rule",
                 "must_not_contain_any:基本没问题", "--severity", "4"]) == EXIT_PASS
    out = capsys.readouterr().out
    assert "已保存 P-001" in out

    assert main(["list"]) == EXIT_PASS
    assert "P-001" in capsys.readouterr().out

    assert main(["show", "P-001"]) == EXIT_PASS
    assert "must_not_contain_any" in capsys.readouterr().out


def test_judge_exit_codes(env, tmp_path, capsys):
    main(["add", "不粉饰", "不得用无依据的淡化措辞",
          "--rule", "must_not_contain_any:基本没问题"])

    bad = tmp_path / "bad.md"
    bad.write_text("目前基本没问题。", encoding="utf-8")
    assert main(["judge", str(bad)]) == EXIT_VIOLATION
    assert "结论：违反" in capsys.readouterr().out

    good = tmp_path / "good.md"
    good.write_text("存在 2 个高优缺陷，若周五不修将影响 30% 用户。", encoding="utf-8")
    assert main(["judge", str(good)]) == EXIT_PASS

    # 没有材料 / 空材料 → 参数错误（2）
    assert main(["judge", "--text", "  "]) == EXIT_UNCERTAIN
    assert main(["judge", str(tmp_path / "missing.md")]) == EXIT_UNCERTAIN


def test_judge_text_and_json_output(env, capsys):
    main(["add", "不粉饰", "s", "--rule", "must_not_contain_any:基本没问题"])
    capsys.readouterr()
    code = main(["judge", "--text", "目前基本没问题", "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert code == EXIT_VIOLATION
    assert payload["overall"] == "violation"
    assert payload["backend"] == "rules"
    assert payload["verdicts"][0]["evidence"]


def test_judge_empty_library_is_uncertain(env):
    assert main(["judge", "--text", "随便一段材料"]) == EXIT_UNCERTAIN


def test_rm_defaults_to_archive(env, capsys):
    main(["add", "t", "s"])
    assert main(["rm", "P-001"]) == EXIT_PASS
    assert "已归档" in capsys.readouterr().out
    assert main(["rm", "P-001", "--hard"]) == EXIT_UNCERTAIN  # 需要 --yes
    assert main(["rm", "P-001", "--hard", "--yes"]) == EXIT_PASS


def test_seed_then_stats(env, capsys):
    assert main(["seed"]) == EXIT_PASS
    assert "写入 8 条" in capsys.readouterr().out
    # 再跑一次全部跳过
    assert main(["seed"]) == EXIT_PASS
    assert "跳过已存在 8 条" in capsys.readouterr().out

    assert main(["stats", "--json"]) == EXIT_PASS
    stats = json.loads(capsys.readouterr().out)
    assert stats["stats"]["active"] == 8


def test_seed_does_not_overwrite_lowercase_id(env, capsys):
    """seed 去重必须不区分大小写（OCR 复审发现）。

    `PrincipleStore.get/save` 按小写比较 id。用户若已有 `p-001`，
    种子里的 `P-001` 以前会被当成「不存在」而覆盖掉它。
    """
    from openagent_principle.models import Principle
    from openagent_principle.store import PrincipleStore

    store = PrincipleStore(env / "home")
    store.save(Principle(id="p-001", title="我的原则", statement="用户写的", criteria="保留我"))

    assert main(["seed"]) == EXIT_PASS
    out = capsys.readouterr().out
    assert "跳过已存在 1 条" in out
    saved = store.get("P-001")
    assert saved.title == "我的原则", "用户的原则被 seed 覆盖了"
    assert saved.criteria == "保留我"


def test_judge_missing_file_is_param_error(env, tmp_path, capsys):
    """参数错误 → 退出码 2（不是 3）。"""
    assert main(["judge", str(tmp_path / "nope.md")]) == EXIT_UNCERTAIN
    assert "文件不存在" in capsys.readouterr().err


def test_judge_directory_is_param_error(env, tmp_path, capsys):
    assert main(["judge", str(tmp_path)]) == EXIT_UNCERTAIN
    assert "目录" in capsys.readouterr().err


def test_bad_rule_is_param_error_not_execution_failure(env, capsys):
    """`--rule` 写错是参数错误 → 退出码 2（OCR 复审发现）。

    以前 `ModelError` 被当成执行失败（3），CI 分不清「参数写错」和「跑挂了」。
    """
    code = main(["add", "t", "s", "--rule", "没有冒号"])
    assert code == EXIT_UNCERTAIN
    assert "[错误]" in capsys.readouterr().err


def test_home_survives_corrupt_legacy_store(env, monkeypatch, capsys, tmp_path):
    """旧库损坏不该拖垮 `home`（OCR 复审发现）。"""
    legacy = tmp_path / "legacy"
    legacy.mkdir()
    (legacy / "principles.json").write_text("{ 坏掉的 JSON", encoding="utf-8")
    monkeypatch.setattr("pathlib.Path.expanduser", lambda self: legacy if "openagent-principle" in str(self) else self)

    code = main(["home"])
    out = capsys.readouterr().out
    assert code == EXIT_PASS
    assert "原则库" in out
    assert "读不了" in out or "旧库" in out


def test_feedback_command(env, capsys):
    main(["add", "不粉饰", "s", "--rule", "must_not_contain_any:基本没问题"])
    main(["judge", "--text", "基本没问题"])
    capsys.readouterr()
    cases = json.loads(_json_cases(env))
    case_id = cases[0]["case_id"]
    assert main(["feedback", case_id, "--disagree", "--note", "原则太宽"]) == EXIT_PASS
    assert "不同意" in capsys.readouterr().out
    assert main(["feedback", "C-404", "--agree"]) == EXIT_UNCERTAIN


def test_update_only_touches_given_fields(env, capsys):
    main(["add", "标题", "原来的陈述", "--criteria", "原来的标准"])
    assert main(["update", "P-001", "--statement", "新的陈述"]) == EXIT_PASS
    detail = _json_show(env, "P-001")
    assert detail["statement"] == "新的陈述"
    assert detail["criteria"] == "原来的标准"
    assert detail["version"] == 2


def test_check_runs_offline_without_keys(env, capsys):
    code = main(["check"])
    out = capsys.readouterr().out
    assert "判断后端" in out
    assert "没配 JEV key" in out
    assert code == EXIT_PASS


# ------------------------------------------------------------------ 单轮提示词
def test_known_subcommands_are_not_treated_as_prompt(env, capsys):
    """已知子命令必须走原路径 —— 不能被当成提示词。"""
    from openagent_principle.cli import _known_subcommands, build_parser

    known = _known_subcommands(build_parser())
    assert {"judge", "list", "add", "chat", "seed", "stats", "home", "check"} <= known
    # 跑一个真实子命令，确认没被单轮模式截走
    assert main(["list"]) == EXIT_PASS
    assert "原则库是空的" in capsys.readouterr().out


def test_prompt_mode_is_detected(env, monkeypatch):
    """首个参数不是已知子命令 → 走单轮模式（不解析成子命令）。"""
    from openagent_principle import cli

    called: list[str] = []
    monkeypatch.setattr(cli, "_run_prompt", lambda p: called.append(p) or EXIT_PASS)
    assert main(["读一下 ./周报.md，看看违反了哪几条原则"]) == EXIT_PASS
    assert called == ["读一下 ./周报.md，看看违反了哪几条原则"]


def test_prompt_mode_joins_multiple_args(env, monkeypatch):
    """多个参数拼成一句提示词（shell 没加引号时也能用）。"""
    from openagent_principle import cli

    called: list[str] = []
    monkeypatch.setattr(cli, "_run_prompt", lambda p: called.append(p) or EXIT_PASS)
    main(["帮我", "看看", "这个项目"])
    assert called == ["帮我 看看 这个项目"]


def test_option_first_still_goes_to_argparse(env, monkeypatch):
    """首个参数是选项（`--version`）时不能被当成提示词。"""
    from openagent_principle import cli

    called: list[str] = []
    monkeypatch.setattr(cli, "_run_prompt", lambda p: called.append(p) or EXIT_PASS)
    with pytest.raises(SystemExit):
        main(["--version"])
    assert called == []


def test_prompt_mode_reports_errors_without_traceback(env, capsys):
    """单轮模式失败时要给人话错误，不是抛栈。

    用「没配 key」触发真实失败路径 —— 比 mock import 更接近用户实际遇到的。
    """
    from openagent_principle.cli import _run_prompt

    code = _run_prompt("随便一句")
    assert code == 3  # EXIT_FAILURE
    err = capsys.readouterr().err
    assert "[错误]" in err
    assert "Traceback" not in err


def test_empty_prompt_is_rejected(env, capsys):
    from openagent_principle.cli import _run_prompt

    assert _run_prompt("   ") == EXIT_UNCERTAIN
    assert "空" in capsys.readouterr().err


# ------------------------------------------------------------------ 小工具
def _json_cases(env) -> str:
    """直接从存储读案例（避免依赖 CLI 输出格式）。"""

    from openagent_principle.store import PrincipleStore

    return json.dumps([c.to_dict() for c in PrincipleStore(env / "home").list_cases()], ensure_ascii=False)


def _json_show(env, principle_id) -> dict:
    import contextlib
    import io

    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        main(["show", principle_id, "--json"])
    return json.loads(buffer.getvalue())
