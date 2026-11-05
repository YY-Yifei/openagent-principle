"""通用工具：目录列举与全文检索（纯 Python，不依赖 ripgrep）。"""

from __future__ import annotations

from openagent_principle.tools import list_dir, search_files


def _tree(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_text("def judge():\n    return 'ok'\n", encoding="utf-8")
    (tmp_path / "src" / "b.py").write_text("import os\n# TODO: 改进\n", encoding="utf-8")
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "note.md").write_text("这里提到 judge 一次\n", encoding="utf-8")
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "x.js").write_text("judge\n", encoding="utf-8")
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "config").write_text("judge\n", encoding="utf-8")
    return tmp_path


def test_search_files_finds_and_skips_noise(tmp_path):
    root = _tree(tmp_path)
    result = search_files("judge", path=str(root))
    assert "a.py" in result and "note.md" in result
    assert "node_modules" not in result, "依赖目录不该淹没结果"
    assert ".git" not in result


def test_search_files_glob_filter(tmp_path):
    root = _tree(tmp_path)
    result = search_files("judge", path=str(root), glob="*.md")
    assert "note.md" in result and "a.py" not in result


def test_search_files_regex_and_case(tmp_path):
    root = _tree(tmp_path)
    assert "b.py" in search_files("TODO", path=str(root), regex=True)
    assert "b.py" in search_files("todo", path=str(root), ignore_case=True)


def test_search_files_reports_no_hit(tmp_path):
    root = _tree(tmp_path)
    assert "无命中" in search_files("不存在的词", path=str(root))


def test_search_files_bad_regex_is_a_message(tmp_path):
    result = search_files("([", path=str(tmp_path), regex=True)
    assert result.startswith("[错误]") and "正则" in result


def test_search_files_missing_path(tmp_path):
    result = search_files("x", path=str(tmp_path / "nope"))
    assert result.startswith("[错误]")


def test_search_files_context(tmp_path):
    root = _tree(tmp_path)
    result = search_files("def judge", path=str(root), context=1)
    assert "return 'ok'" in result, "context=1 应带出相邻行"


def test_list_dir(tmp_path):
    root = _tree(tmp_path)
    result = list_dir(str(root))
    assert "src/" in result and "docs/" in result
    assert ".git" not in result, "默认不显示隐藏项"
    assert "node_modules/" in result


def test_list_dir_show_hidden(tmp_path):
    root = _tree(tmp_path)
    assert ".git/" in list_dir(str(root), show_hidden=True)


def test_list_dir_on_file_is_an_error(tmp_path):
    target = tmp_path / "a.txt"
    target.write_text("x", encoding="utf-8")
    assert list_dir(str(target)).startswith("[错误]")


def test_list_dir_max_items(tmp_path):
    for i in range(10):
        (tmp_path / f"f{i}.txt").write_text("x", encoding="utf-8")
    result = list_dir(str(tmp_path), max_items=3)
    assert "还有 7 项" in result
