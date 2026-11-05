"""库位置解析：默认项目级 + `PRINCIPLE_HOME` 覆盖 + 库内 `.gitignore`。

为什么要专门一整个文件：库位置错了**不会报错**，只会悄悄写进另一个库。
线上表现是「我明明 `list` 到 8 条，`judge` 却说没有原则可判」这种自相矛盾，
从报错里一点线索都没有。所以每条发现规则都要单独钉死。
"""

from __future__ import annotations

import json

import pytest

from openagent_principle.cli import main
from openagent_principle.config import Settings, find_project_root, resolve_home
from openagent_principle.store import GITIGNORE_TEXT, PrincipleStore

#: `.gitignore` 里真正生效的行（去掉注释与空行）—— 断言只看规则，不看解释。
def _rules(text: str) -> list[str]:
    return [line.strip() for line in text.splitlines() if line.strip() and not line.strip().startswith("#")]


def _marker(directory, name: str = ".git"):
    """造一个项目标记（文件即可 —— worktree 的 `.git` 就是个文件）。"""
    directory.mkdir(parents=True, exist_ok=True)
    (directory / name).write_text("x", encoding="utf-8")
    return directory


@pytest.fixture
def no_env_home(monkeypatch):
    """摘掉 `PRINCIPLE_HOME` —— 发现规则只在没有显式指定时才生效。"""
    monkeypatch.delenv("PRINCIPLE_HOME", raising=False)


# ---------------------------------------------------------------- 发现规则
def test_no_marker_falls_back_to_start_dir(tmp_path, no_env_home):
    work = tmp_path / "work"
    work.mkdir()
    resolution = resolve_home(work)

    assert resolution.path == work / ".openagent-principle"
    assert resolution.source == "cwd"
    assert resolution.root == work


def test_project_marker_wins_from_deep_subdir(tmp_path, no_env_home):
    """在 `src/deep/` 里跑命令，库仍应落在项目根 —— 否则每个子目录一个库。"""
    root = _marker(tmp_path / "proj")
    deep = root / "src" / "deep"
    deep.mkdir(parents=True)

    resolution = resolve_home(deep)

    assert resolution.path == root / ".openagent-principle"
    assert resolution.source == "project"
    assert resolution.root == root


def test_pyproject_alone_counts_as_root(tmp_path, no_env_home):
    root = _marker(tmp_path / "lib", "pyproject.toml")
    assert find_project_root(root / "sub") == (root, "project")


def test_existing_store_beats_farther_marker(tmp_path, no_env_home):
    """已有库的下级目录里没标记，也要落到那个库 —— 否则就是「数据凭空消失」。"""
    outer = _marker(tmp_path / "outer")
    inner = outer / "pkg"
    (inner / ".openagent-principle").mkdir(parents=True)

    assert find_project_root(inner / "a" / "b") == (inner, "store")


def test_nearest_store_wins_over_own_marker(tmp_path, no_env_home):
    """子仓库（自己有 `.git`）里已有库时，认自己那份，不往上跑到父仓库。"""
    _marker(tmp_path / "mono")
    sub = _marker(tmp_path / "mono" / "sub")
    (sub / ".openagent-principle").mkdir()

    assert find_project_root(sub) == (sub, "store")


def test_file_start_uses_its_parent(tmp_path, no_env_home):
    root = _marker(tmp_path / "proj")
    target = root / "report.md"
    target.write_text("x", encoding="utf-8")

    assert find_project_root(target) == (root, "project")


# ------------------------------------------------------------ 覆盖与时机
def test_env_home_overrides_discovery(tmp_path, monkeypatch):
    monkeypatch.setenv("PRINCIPLE_HOME", str(tmp_path / "elsewhere"))
    _marker(tmp_path / "proj")

    resolution = resolve_home(tmp_path / "proj")

    assert resolution.path == tmp_path / "elsewhere"
    assert resolution.source == "env"


def test_settings_resolves_home_at_instantiation(tmp_path, monkeypatch):
    """回归：`home` 的默认值**不能在 import 时**算一次，否则 cd 之后仍写旧库。"""
    first = _marker(tmp_path / "a")
    second = _marker(tmp_path / "b")
    monkeypatch.delenv("PRINCIPLE_HOME", raising=False)

    monkeypatch.chdir(first)
    assert Settings().home == first / ".openagent-principle"

    monkeypatch.chdir(second)
    assert Settings().home == second / ".openagent-principle"


def test_store_without_home_follows_settings(tmp_path, monkeypatch):
    root = _marker(tmp_path / "proj")
    monkeypatch.delenv("PRINCIPLE_HOME", raising=False)
    monkeypatch.chdir(root)

    assert PrincipleStore().home == root / ".openagent-principle"


# -------------------------------------------------------------- .gitignore
def test_ensure_drops_gitignore_keeping_principles_trackable(store):
    store.ensure()
    text = (store.home / ".gitignore").read_text(encoding="utf-8")

    assert text == GITIGNORE_TEXT
    rules = _rules(text)
    assert rules == ["cases.jsonl", "feedback.jsonl", "*.tmp"]
    assert "principles.json" not in rules  # 原则是资产，应该能被提交


def test_ensure_never_overwrites_existing_gitignore(store):
    store.ensure()
    path = store.home / ".gitignore"
    path.write_text("mine\n", encoding="utf-8")

    store.ensure()

    assert path.read_text(encoding="utf-8") == "mine\n"


def test_ensure_still_works_when_gitignore_unwritable(store, monkeypatch):
    """写 `.gitignore` 只是锦上添花：写不了也不该挡住建库。"""
    store.home.mkdir(parents=True, exist_ok=True)

    def boom(*args, **kwargs):
        raise OSError("read-only")

    monkeypatch.setattr("pathlib.Path.write_text", boom)
    store.ensure()  # 不抛
    assert not (store.home / ".gitignore").exists()

    monkeypatch.undo()
    store.ensure()  # 恢复正常后补上


# -------------------------------------------------------------------- CLI
def test_cli_home_shows_where_and_why(tmp_path, monkeypatch, capsys, no_env_home):
    root = _marker(tmp_path / "proj")
    monkeypatch.chdir(root)

    assert main(["home"]) == 0
    out = capsys.readouterr().out

    assert "原则库" in out
    assert str(root / ".openagent-principle") in out
    assert "项目根标记" in out
    assert "尚未建库" in out  # 还没写入过


def test_cli_home_json_is_machine_readable(tmp_path, monkeypatch, capsys, no_env_home):
    root = _marker(tmp_path / "proj")
    (root / ".openagent-principle").mkdir()
    monkeypatch.chdir(root)

    assert main(["home", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)

    assert payload["source"] == "store"
    assert payload["exists"] is True
    assert payload["root"] == str(root)
