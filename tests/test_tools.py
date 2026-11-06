"""工具层：每个函数都必须返回字符串，且坏输入不抛异常。"""

from __future__ import annotations

import json

from openagent_principle import tools
from openagent_principle.store import PrincipleStore


def _jev_env(monkeypatch, answers: dict) -> None:
    """配好 JEV，并把传输层换成固定回答。"""
    from openagent_principle import _http

    monkeypatch.setenv("JEV_API_KEY", "k")
    monkeypatch.setenv("JEV_BASE_URL", "https://jev.example")

    def _send(url, headers, payload, timeout):  # noqa: ANN001, ARG001
        return 200, json.dumps({"model": "jev-1.13.0", "answers": answers})

    monkeypatch.setattr(_http, "urllib_transport", _send)


def _violation_answers() -> dict:
    return {
        "p_001": {
            "type": "choice",
            "choice": "violation",
            "probabilities": {"pass": 0.05, "violation": 0.9},
            "confidence": 0.85,
        },
        "overall": {"type": "choice", "choice": "violation"},
    }


def test_save_list_get_flow(env):
    saved = tools.save_principle(
        title="复盘必须给改进项",
        statement="复盘里提到错误时，必须给可执行改进措施",
        criteria="没有改进措施算违反",
        examples="反例：下次注意",
        severity=4,
        tags="复盘,改进",
    )
    assert "已新建 P-001" in saved

    listing = tools.list_principles()
    assert "P-001" in listing and "复盘必须给改进项" in listing

    detail = tools.get_principle("P-001")
    assert "判定标准" in detail and "没有改进措施算违反" in detail

    updated = tools.save_principle(principle_id="P-001", criteria="没有改进措施或责任人都算违反")
    assert "已更新 P-001" in updated and "版本 2" in updated
    assert "责任人都算违反" in tools.get_principle("P-001")


def test_save_new_requires_title_and_statement(env):
    assert tools.save_principle(title="", statement="s").startswith("[错误]")
    assert tools.save_principle(title="t", statement="").startswith("[错误]")


def test_update_missing_principle_is_explained(env):
    assert tools.save_principle(principle_id="P-404", title="t").startswith("[错误]")
    assert tools.get_principle("P-404").startswith("[错误]")


def test_delete_archives_and_reports(env):
    tools.save_principle(title="t", statement="s")
    assert "已归档" in tools.delete_principle("P-001")
    assert tools.get_principle("P-001").startswith("[错误]") is False  # 归档仍可查
    assert "P-001" not in tools.list_principles()
    assert "已删除" in tools.delete_principle("P-001", hard=True)


def test_judge_material_reports_violation(env, monkeypatch):
    tools.save_principle(
        title="不粉饰",
        statement="不得用无依据的淡化措辞",
        criteria="出现「基本没问题」且无数据支撑算违反",
        severity=4,
    )
    _jev_env(monkeypatch, _violation_answers())
    report = tools.judge_material("目前基本没问题，客户还没发现。", source="周报")
    assert "结论：违反" in report
    assert "后端 jev" in report


def test_judge_material_empty_returns_error(env):
    assert tools.judge_material("  ").startswith("[错误]")


def test_judge_file_reads_and_judges(env, tmp_path, monkeypatch):
    tools.save_principle(
        title="目标先行",
        statement="方案必须先写目标",
        criteria="只有做法没有目标算违反",
    )
    _jev_env(monkeypatch, _violation_answers())
    doc = tmp_path / "方案.md"
    doc.write_text("我们打算重写这个模块。", encoding="utf-8")
    report = tools.judge_file(str(doc))
    assert "结论：违反" in report
    assert str(doc) in report


def test_judge_file_missing_is_explained(env, tmp_path):
    assert tools.judge_file(str(tmp_path / "nope.md")).startswith("[错误]")


def test_case_tools_roundtrip(env, monkeypatch):
    tools.save_principle(
        title="不粉饰",
        statement="s",
        criteria="出现「基本没问题」算违反",
    )
    _jev_env(monkeypatch, _violation_answers())
    report = tools.judge_material("目前基本没问题。", source="周报")
    case_id = report.split()[1]

    listing = tools.list_cases()
    assert case_id in listing and "违反" in listing
    assert "结论：违反" in tools.show_case(case_id)

    feedback = tools.record_case_feedback(case_id, agree=False, note="这条原则写太宽")
    assert "不同意" in feedback
    assert tools.record_case_feedback("C-404", agree=True).startswith("[错误]")


def test_read_text_file(env, tmp_path):
    doc = tmp_path / "a.txt"
    doc.write_text("你好", encoding="utf-8")
    assert "你好" in tools.read_text_file(str(doc))
    assert tools.read_text_file(str(tmp_path / "missing.txt")).startswith("[错误]")
    binary = tmp_path / "b.bin"
    binary.write_bytes(b"\x00\x01\x02")
    assert "二进制" in tools.read_text_file(str(binary))


def test_fetch_url_is_removed(env):
    """`fetch_url` 已移除：抓网页是衍生能力，用 Bash 的 curl 即可。

    它不该再出现在工具集里 —— 少一个域工具位，也少一个 SSRF 面。
    """
    assert not hasattr(tools, "fetch_url")
    from openagent_principle.agent import DOMAIN_TOOLS, READ_ONLY_DOMAIN

    assert "fetch_url" not in DOMAIN_TOOLS
    assert "fetch_url" not in READ_ONLY_DOMAIN


def test_tools_share_the_same_store(env):
    tools.save_principle(title="t", statement="s")
    assert PrincipleStore(env / "home").get("P-001") is not None


# ---------------------------------------------------------------------------
# 模型传来的参数不可信（OCR 复审发现）
# ---------------------------------------------------------------------------
def test_delete_hard_string_false_archives_not_deletes(env):
    """`hard="false"` 必须归档，不能真删（OCR 复审发现）。

    模型常把布尔写成字符串；`"false"` 在 Python 里是真值 —— 直接透传会把
    「归档」变成「不可恢复的真删」。
    """
    tools.save_principle(title="t", statement="s")
    result = tools.delete_principle("P-001", hard="false")
    assert "已归档" in result
    assert PrincipleStore(env / "home").get("P-001") is not None, "原则被真删了"


def test_delete_hard_string_true_deletes(env):
    tools.save_principle(title="t", statement="s")
    result = tools.delete_principle("P-001", hard="true")
    assert "已删除" in result
    assert PrincipleStore(env / "home").get("P-001") is None


def test_save_principle_tolerates_null_fields(env):
    """模型传 null 不能抛 AttributeError（OCR 复审发现）。"""
    result = tools.save_principle(title="t", statement="s", criteria=None, tags=None)
    assert "已新建" in result


def test_save_principle_tolerates_bad_severity(env):
    result = tools.save_principle(title="t", statement="s", severity="abc")
    assert "已新建" in result


def test_list_cases_tolerates_bad_limit(env):
    assert isinstance(tools.list_cases(limit="abc"), str)


def test_list_dir_tolerates_bad_max_items(env):
    assert isinstance(tools.list_dir(str(env), max_items="abc"), str)


def test_search_files_context_does_not_inflate_hit_count(env, tmp_path):
    """`context > 0` 时命中数不能把上下文行算进去（OCR 复审发现）。"""
    f = tmp_path / "a.txt"
    f.write_text("\n".join(["x", "TARGET", "y", "z", "TARGET", "w"]), encoding="utf-8")
    out = tools.search_files("TARGET", path=str(tmp_path), context=1, max_results=2)
    assert "命中 2 条" in out, out


def test_search_files_tolerates_bad_args(env, tmp_path):
    (tmp_path / "a.txt").write_text("hello", encoding="utf-8")
    assert isinstance(tools.search_files("hello", path=str(tmp_path), max_results="abc"), str)
    assert isinstance(tools.search_files("hello", path=str(tmp_path), context="abc"), str)


def test_list_principles_empty_hint(env):
    message = tools.list_principles()
    assert "空" in message and "save_principle" in message
