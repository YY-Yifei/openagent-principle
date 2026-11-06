"""结构化 API：给**其他 agent / 程序**调用的入口。

为什么单独一层：`tools.py` 的函数是给模型读的 —— 返回格式化好的中文报告，
错误也是 `[错误] …` 字符串。那对模型友好，对程序不友好：调用方得再解析一遍
文本才能拿到结论。

这一层只做一件事：**同样的能力，返回结构化对象**。

    from openagent_principle.api import judge, list_principles

    result = judge("目前基本没问题。")
    if result.overall == "violation":
        for v in result.violations:
            print(v.principle_id, v.evidence)

错误处理：这一层**抛异常**（`ApiError` 及其子类），不返回错误字符串 ——
程序需要的是能 `except` 的东西，不是要正则匹配的文本。
`tools.py` 负责把异常翻译成给模型看的话。

本模块不 import agentscope，与 `judge` / `jev` 一样只用 stdlib。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Sequence

from .config import Settings, load_settings
from .judge import Judge, JudgeError
from .models import Judgement, ModelError, Principle
from .store import PrincipleStore, StoreError


class ApiError(RuntimeError):
    """结构化 API 的统一异常基类。

    调用方可以只 `except ApiError` 兜住全部业务错误；需要区分时再捕获子类。
    """


class NotFoundError(ApiError):
    """要的东西不存在（原则编号、案例号）。"""


class InvalidInputError(ApiError):
    """输入不合法（字段缺失、材料为空）。"""


class BackendError(ApiError):
    """判断后端不可用或失败（没配 key、网络、鉴权、响应格式）。"""


def _store(settings: Settings | None = None) -> PrincipleStore:
    settings = settings or load_settings()
    return PrincipleStore(settings.home_path)


# ---------------------------------------------------------------------------
# 原则
# ---------------------------------------------------------------------------
def list_principles(
    query: str = "",
    tag: str = "",
    include_archived: bool = False,
    *,
    settings: Settings | None = None,
) -> list[Principle]:
    """按条件列出原则（按严重度降序）。

    Raises:
        ApiError: 原则库读不了（JSON 坏了 / 目录不可写）。
    """
    try:
        principles = _store(settings).search(
            query=query, tag=tag, include_archived=include_archived
        )
    except StoreError as exc:
        raise ApiError(str(exc)) from exc
    return sorted(principles, key=lambda p: (-p.severity, p.id))


def get_principle(principle_id: str, *, settings: Settings | None = None) -> Principle:
    """取一条原则。

    Raises:
        NotFoundError: 没有这个编号。
        ApiError: 库读不了。
    """
    try:
        principle = _store(settings).get(principle_id)
    except StoreError as exc:
        raise ApiError(str(exc)) from exc
    if principle is None:
        raise NotFoundError(f"没有 {principle_id} 这条原则")
    return principle


def save_principle(
    *,
    title: str = "",
    statement: str = "",
    rationale: str = "",
    scope: str = "",
    criteria: str = "",
    examples: Sequence[str] | None = None,
    severity: int = 0,
    tags: Sequence[str] | None = None,
    principle_id: str = "",
    settings: Settings | None = None,
) -> Principle:
    """新建或修改一条原则，返回落盘后的对象。

    给了 `principle_id` 就是修改（只覆盖非空字段），否则新建。

    Raises:
        NotFoundError: 指定了 `principle_id` 但它不存在。
        InvalidInputError: 字段不合法（缺标题/陈述）。
        ApiError: 写盘失败。
    """
    store = _store(settings)
    try:
        existing = store.get(principle_id) if principle_id.strip() else None
    except StoreError as exc:
        raise ApiError(str(exc)) from exc
    if principle_id.strip() and existing is None:
        raise NotFoundError(f"没有 {principle_id} 这条原则；要新建请把 principle_id 留空")

    if existing is None:
        principle = Principle(id="", title=title.strip(), statement=statement.strip(), severity=severity or 3)
    else:
        principle = existing
        if title.strip():
            principle.title = title.strip()
        if statement.strip():
            principle.statement = statement.strip()

    if rationale.strip():
        principle.rationale = rationale.strip()
    if scope.strip():
        principle.scope = scope.strip()
    if criteria.strip():
        principle.criteria = criteria.strip()
    if examples:
        principle.examples = [e.strip() for e in examples if e.strip()]
    if tags:
        principle.tags = [t.strip() for t in tags if t.strip()]
    if severity:
        principle.severity = max(1, min(5, int(severity)))

    try:
        return store.save(principle)
    except ModelError as exc:
        raise InvalidInputError(str(exc)) from exc
    except StoreError as exc:
        raise ApiError(str(exc)) from exc


def delete_principle(
    principle_id: str, *, hard: bool = False, settings: Settings | None = None
) -> Principle:
    """归档（默认）或删除一条原则，返回被处理的那条。

    Raises:
        NotFoundError: 没有这个编号。
        ApiError: 写盘失败。
    """
    store = _store(settings)
    try:
        principle = store.get(principle_id)
        if principle is None:
            raise NotFoundError(f"没有 {principle_id} 这条原则")
        store.delete(principle_id, hard=hard)
    except StoreError as exc:
        raise ApiError(str(exc)) from exc
    return principle


# ---------------------------------------------------------------------------
# 判断
# ---------------------------------------------------------------------------
def judge(
    material: str,
    *,
    principle_ids: Sequence[str] | None = None,
    source: str = "",
    backend: str = "",
    save: bool = True,
    settings: Settings | None = None,
    transport: Any = None,
) -> Judgement:
    """判断一段材料，返回结构化 `Judgement`。

    Args:
        material: 被判断的原文（不要先总结 —— 证据引用需要原句）。
        principle_ids: 只判这些原则（空 = 按严重度自动挑）。
        source: 材料来源标签，写进案例。
        backend: 临时覆盖后端（jev / llm / auto）。
        save: 是否把案例写进 `cases.jsonl`。
        settings: 配置（默认读环境变量）。
        transport: 传输层钩子（测试离线用）。

    Returns:
        `Judgement`：`overall` / `violations` / `verdicts` / `notes` 都是结构化字段。

    Raises:
        InvalidInputError: 材料为空。
        BackendError: 没有可用的判断后端。
        ApiError: 案例写盘失败。
    """
    settings = settings or load_settings()
    try:
        return Judge(settings, PrincipleStore(settings.home_path)).judge(
            material,
            principle_ids=list(principle_ids) if principle_ids else None,
            subject=source,
            backend=backend,
            save=save,
            transport=transport,
        )
    except JudgeError as exc:
        # 材料为空是调用方的输入问题；没有可用后端是环境问题 —— 分开报，
        # 调用方才能决定「改输入」还是「去配 key」。按入参判断，不解析异常文案。
        if not (material or "").strip():
            raise InvalidInputError(str(exc)) from exc
        raise BackendError(str(exc)) from exc
    except (StoreError, ModelError) as exc:
        raise ApiError(str(exc)) from exc


def judge_file(
    path: str | Path,
    *,
    principle_ids: Sequence[str] | None = None,
    backend: str = "",
    save: bool = True,
    settings: Settings | None = None,
    transport: Any = None,
) -> Judgement:
    """读文件并判断。

    Raises:
        NotFoundError: 文件不存在。
        InvalidInputError: 是目录 / 空文件 / 二进制。
        BackendError: 没有可用的判断后端。
        ApiError: 读文件或写案例失败。
    """
    settings = settings or load_settings()
    target = Path(path).expanduser()
    if not target.exists():
        raise NotFoundError(f"文件不存在：{target}")
    if target.is_dir():
        raise InvalidInputError(f"这是一个目录，不是文件：{target}")
    try:
        text = target.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        raise ApiError(f"读不了 {target}：{exc}") from exc
    if "\x00" in text[:2000]:
        raise InvalidInputError(f"{target} 看起来是二进制文件")
    if not text.strip():
        raise InvalidInputError(f"{target} 是空文件")
    return judge(
        text,
        principle_ids=principle_ids,
        source=str(target),
        backend=backend,
        save=save,
        settings=settings,
        transport=transport,
    )


# ---------------------------------------------------------------------------
# 案例与反馈
# ---------------------------------------------------------------------------
def list_cases(
    *,
    principle_id: str = "",
    limit: int = 5,
    settings: Settings | None = None,
) -> list[Judgement]:
    """最近的判断案例（新的在前）。

    Raises:
        ApiError: 案例文件读不了。
    """
    try:
        return _store(settings).list_cases(principle_id=principle_id, limit=max(1, int(limit)))
    except StoreError as exc:
        raise ApiError(str(exc)) from exc


def get_case(case_id: str, *, settings: Settings | None = None) -> Judgement:
    """按案例号取一条。

    Raises:
        NotFoundError: 没有这个案例号。
    """
    try:
        case = _store(settings).get_case(case_id)
    except StoreError as exc:
        raise ApiError(str(exc)) from exc
    if case is None:
        raise NotFoundError(f"没有 {case_id} 这个案例")
    return case


def record_feedback(
    case_id: str,
    *,
    agree: bool,
    principle_id: str = "",
    note: str = "",
    settings: Settings | None = None,
) -> dict[str, Any]:
    """记录人工反馈，返回写入的记录。

    Raises:
        NotFoundError: 没有这个案例号。
        ApiError: 写盘失败。
    """
    store = _store(settings)
    try:
        if store.get_case(case_id) is None:
            raise NotFoundError(f"没有 {case_id} 这个案例")
        return store.append_feedback(case_id, agree=agree, principle_id=principle_id, note=note)
    except StoreError as exc:
        raise ApiError(str(exc)) from exc


def stats(*, settings: Settings | None = None) -> dict[str, Any]:
    """库统计 + 按原则的误判率。

    Returns:
        `{"stats": {...}, "feedback": [...]}`，与 `principles stats --json` 同构。

    Raises:
        ApiError: 库读不了。
    """
    store = _store(settings)
    try:
        return {"stats": store.stats(), "feedback": store.feedback_summary()}
    except StoreError as exc:
        raise ApiError(str(exc)) from exc
