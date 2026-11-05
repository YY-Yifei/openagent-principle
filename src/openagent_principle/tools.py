"""agent 的工具集：原则 CRUD、判断、材料获取、案例与反馈。

工具是 agent 唯一的行动出口，所以每个函数都守三条纪律：

1. **返回 str，绝不抛异常**：失败返回 `[错误] …`。异常会打断 ReAct 循环，
   而「工具报错」本身就是模型需要读到的一条信息。
2. **参数有类型注解与默认值**：AgentScope 用签名生成给模型看的 JSON Schema，
   `*args, **kwargs` 等于告诉模型「没有参数」。
3. **文案里写下一步**：读工具结果的是模型，所以错误信息要能指导它重试或换路。

**业务逻辑在 `api.py`**：那一层返回结构化对象、抛异常，给程序用。
这里只负责「调 api → 把结果/异常翻译成给模型看的中文」。两层分开是因为
同一个能力有两个消费者：模型要读得懂的话，程序要能 `except` 的对象。

本模块不 import agentscope —— CLI 与测试都直接用这些函数。
"""

from __future__ import annotations

import contextvars
import fnmatch
import re
from pathlib import Path

from . import api
from .config import Settings, load_settings
from .judge import format_judgement
from .store import PrincipleStore, StoreError


# ---------------------------------------------------------------------------
# 内部装配
# ---------------------------------------------------------------------------
#: 当前生效的配置。为什么用 contextvar 而不是给每个工具加 `settings` 参数：
#: 工具签名会被 AgentScope 拿去生成给模型看的 JSON Schema，多一个参数就多一个
#: 模型可能乱填的字段。contextvar 让「装配时绑定一次、调用时自动生效」成为可能，
#: 且对并发任务天然隔离（每个 task 有自己的 context 副本）。
#:
#: 复审 4.5 的问题：`build_agent(Settings(home=A))` 里的工具仍读环境变量指向的库 B。
#: 现在 `agent.build_toolkit` 会用 `bind_settings(settings)` 把同一个 Settings 绑进来。
_SETTINGS: contextvars.ContextVar[Settings | None] = contextvars.ContextVar(
    "openagent_principle_settings", default=None
)


def bind_settings(settings: Settings | None) -> contextvars.Token:
    """把一份 Settings 绑到当前上下文；返回 token 供 `reset_settings` 还原。"""
    return _SETTINGS.set(settings)


def reset_settings(token: contextvars.Token) -> None:
    """还原到 `bind_settings` 之前的配置。"""
    _SETTINGS.reset(token)


def current_settings() -> Settings:
    """当前生效的配置：绑过就用绑的，没绑就读环境变量（CLI / 测试的默认路径）。"""
    bound = _SETTINGS.get()
    return bound if bound is not None else load_settings()


def _store() -> PrincipleStore:
    return PrincipleStore(current_settings().home_path)


#: 模型常把布尔写成字符串。这些值必须解析成 False —— 否则 `"false"` 是真值。
_FALSE_WORDS = frozenset({"false", "0", "no", "off", "n", "f", ""})
_TRUE_WORDS = frozenset({"true", "1", "yes", "on", "y", "t"})


def _to_bool(raw: object, *, default: bool = False) -> bool:
    """把模型传来的任意值**严格**解析成布尔。

    为什么需要它：工具参数来自模型，`hard="false"` 在 Python 里是真值 ——
    直接透传会把「归档」变成「不可恢复的真删」（OCR 复审发现）。
    解析不了就用 `default`（安全侧）。
    """
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, (int, float)):
        return bool(raw)
    if isinstance(raw, str):
        text = raw.strip().lower()
        if text in _TRUE_WORDS:
            return True
        if text in _FALSE_WORDS:
            return False
    return default


def _to_int(raw: object, default: int, *, minimum: int | None = None) -> int:
    """把模型传来的任意值解析成整数；解析不了就用 `default`。

    工具永不抛异常 —— `int("abc")` 会抛 `ValueError`，那会打断 ReAct 循环。
    """
    try:
        value = int(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default
    if minimum is not None and value < minimum:
        return minimum
    return value


def _as_text(raw: object) -> str:
    """把模型传来的任意值归一成字符串（`None` → `""`）。

    模型常把可选参数传成 `null`；直接 `.strip()` 会抛 `AttributeError`，
    绕过工具的 except 分支（OCR 复审发现）。
    """
    if raw is None:
        return ""
    if isinstance(raw, str):
        return raw
    return str(raw)


def _split_ids(raw: str) -> list[str]:
    """把 `"P-001, P-003"` / `"P-001 P-003"` 拆成 id 列表。"""
    return [part for part in re.split(r"[,，;；\s]+", (raw or "").strip()) if part]


def _split_tags(raw: str) -> list[str]:
    return _split_ids(raw)


def _split_lines(raw: str) -> list[str]:
    return [line.strip() for line in (raw or "").splitlines() if line.strip()]


def _read_source(path: str, max_chars: int) -> tuple[str, str]:
    """读本地文件，返回 `(正文, 错误)` —— 两者恰有一个为空。"""
    target = Path(path).expanduser()
    if not target.exists():
        return "", f"[错误] 文件不存在：{target}"
    if target.is_dir():
        return "", f"[错误] 这是一个目录，不是文件：{target}"
    try:
        with target.open("r", encoding="utf-8", errors="replace") as fh:
            text = fh.read(max_chars + 1)
    except OSError as exc:
        return "", f"[错误] 读不了 {target}：{exc}"
    if "\x00" in text[:2000]:
        return "", f"[错误] {target} 看起来是二进制文件，请换一个文本文件。"
    if len(text) > max_chars:
        text = text[:max_chars] + f"\n…（已截断到 {max_chars} 字符）"
    return text, ""


# ---------------------------------------------------------------------------
# 原则维护
# ---------------------------------------------------------------------------
def list_principles(query: str = "", tag: str = "", include_archived: bool = False) -> str:
    """列出原则库里的原则（可按关键词/标签筛选）。动手判断前先用它确认有哪些原则可用。

    Args:
        query: 关键词，在标题/陈述/理由/判定标准/标签里做不区分大小写的子串匹配。
        tag: 只要这个标签的原则（如「复盘」「决策」）。
        include_archived: 是否连已归档的原则一起列。

    Returns:
        一行一条的清单，末尾给总数；库为空时提示先新建或 seed。
    """
    try:
        settings = current_settings()
        store = _store()
        principles = api.list_principles(
            query=query, tag=tag, include_archived=include_archived, settings=settings
        )
        if not principles:
            total = len(store.load())
            if total == 0:
                return "原则库是空的。先用 save_principle 建一条（或让用户跑 `openagent-principle seed` 装 8 条种子原则）。"
            return f"没有符合条件的原则（库里共 {total} 条）。换个关键词，或去掉 tag 过滤。"
        lines = [p.one_line() for p in principles]
        lines.append(f"（共 {len(principles)} 条；库目录 {store.home}）")
        return "\n".join(lines)
    except (api.ApiError, StoreError) as exc:
        return f"[错误] {exc}"


def get_principle(principle_id: str) -> str:
    """看一条原则的完整内容（陈述/理由/适用范围/判定标准/例子/机器可判规则/版本）。

    Args:
        principle_id: 原则编号，如 `P-001`。

    Returns:
        完整展开的原则；不存在时给出提示。
    """
    try:
        principle = api.get_principle(principle_id, settings=current_settings())
    except api.NotFoundError:
        return f"[错误] 没有 {principle_id} 这条原则。先用 list_principles 看现有编号，或 save_principle 新建。"
    except api.ApiError as exc:
        return f"[错误] {exc}"
    meta = f"版本 {principle.version} | 来源 {principle.source} | 更新于 {principle.updated_at}"
    return f"{principle.full_text()}\n{meta}"


def save_principle(
    title: str = "",
    statement: str = "",
    rationale: str = "",
    scope: str = "",
    criteria: str = "",
    examples: str = "",
    rules: str = "",
    severity: int = 0,
    tags: str = "",
    principle_id: str = "",
) -> str:
    """新建或修改一条原则（给了 principle_id 就是修改，否则新建）。

    **一条好原则必须可判定**：只有一句「要坦诚」是口号，判断不了。所以尽量补齐
    `criteria`（人话判定标准）与 `rules`（机器可判的硬规则），反例写进 `examples`。
    判定不了的原则，判断阶段只能给出「说不准」。

    Args:
        title: 短标题（修改时可留空 = 不改）。
        statement: 一句话说清这条原则要求什么（修改时可留空 = 不改）。
        rationale: 为什么有这条原则（面对什么风险/吃过什么亏）。
        scope: 适用范围，如「复盘 / 事故报告」。
        criteria: 人话判定标准：什么样算违反。
        examples: 例子，每行一个；建议写成「反例：…」「正例：…」。
        rules: 机器可判规则，JSON 数组字符串。可用类型见 models.RULE_KINDS，
            例如 `[{"kind":"must_not_contain_any","value":["基本没问题"]}]`。
        severity: 严重度 1~5（5=红线）；0 表示不修改/新建时默认 3。
        tags: 标签，逗号或空格分隔。
        principle_id: 要修改的原则编号；留空 = 新建。

    Returns:
        保存结果（含编号、版本），以及「还缺什么才能更可判定」的提示。
    """
    # 模型可能传 null（JSON 里常见）—— 先归一成字符串，否则 `.strip()` 抛
    # AttributeError，绕过下面的 except（OCR 复审发现）。
    title = _as_text(title)
    statement = _as_text(statement)
    rationale = _as_text(rationale)
    scope = _as_text(scope)
    criteria = _as_text(criteria)
    examples = _as_text(examples)
    rules = _as_text(rules)
    tags = _as_text(tags)
    principle_id = _as_text(principle_id)
    severity = _to_int(severity, 0, minimum=0)

    try:
        settings = current_settings()
        existing = (
            api.get_principle(principle_id, settings=settings) if principle_id.strip() else None
        )
    except api.NotFoundError:
        return f"[错误] 没有 {principle_id} 这条原则。要新建请把 principle_id 留空。"
    except api.ApiError as exc:
        return f"[错误] {exc}"
    except Exception as exc:  # noqa: BLE001  工具永不抛异常
        return f"[错误] 读取原则失败（{type(exc).__name__}）：{exc}"

    try:
        saved = api.save_principle(
            title=title,
            statement=statement,
            rationale=rationale,
            scope=scope,
            criteria=criteria,
            examples=_split_lines(examples),
            rules=rules,
            severity=severity,
            tags=_split_tags(tags),
            principle_id=principle_id,
            settings=settings,
        )
    except api.InvalidInputError as exc:
        return f"[错误] {exc}。正确示例：`[{{\"kind\":\"must_not_contain_any\",\"value\":[\"绝对没问题\"]}}]`"
    except api.ApiError as exc:
        return f"[错误] {exc}"
    except Exception as exc:  # noqa: BLE001  工具永不抛异常
        return f"[错误] 保存失败（{type(exc).__name__}）：{exc}"

    action = "已更新" if existing is not None else "已新建"
    hints: list[str] = []
    if not saved.criteria and not saved.rules:
        hints.append("它现在还不可判定：补 criteria（人话标准）或 rules（机器可判），否则判断只能给「说不准」")
    elif not saved.examples:
        hints.append("建议补一个反例（examples 每行一个），判断会更稳")
    hint = ("\n提示：" + "；".join(hints)) if hints else ""
    return f"{action} {saved.id}《{saved.title}》（版本 {saved.version}，严重度 {saved.severity}）{hint}"


def delete_principle(principle_id: str, hard: bool = False) -> str:
    """归档（默认）或彻底删除一条原则。

    默认只归档：原则的演化史本身有复盘价值（「我们曾经这么判，后来改了主意」）。
    只有「写错了、从没生效过」才用 `hard=True` 真删。

    Args:
        principle_id: 原则编号。
        hard: True = 从文件里真删；False = 标记归档（判断时不再使用）。

    Returns:
        结果说明。
    """
    # `hard` 必须**严格**解析成布尔（OCR 复审发现）：模型常把布尔写成字符串，
    # `"false"` / `"no"` / `"0"` 在 Python 里都是真值 —— 直接透传会把
    # 「归档」变成「不可恢复的真删」。宁可多问一句，也不能误删。
    hard_flag = _to_bool(hard, default=False)
    try:
        principle = api.delete_principle(principle_id, hard=hard_flag, settings=current_settings())
    except api.NotFoundError:
        return f"[错误] 没有 {principle_id} 这条原则。"
    except api.ApiError as exc:
        return f"[错误] {exc}"
    except Exception as exc:  # noqa: BLE001  工具永不抛异常
        return f"[错误] 删除失败（{type(exc).__name__}）：{exc}"
    if hard_flag:
        return f"已删除 {principle_id}《{principle.title}》（不可恢复）。"
    return f"已归档 {principle_id}《{principle.title}》：判断时不再使用，文件里仍可查。"


# ---------------------------------------------------------------------------
# 判断
# ---------------------------------------------------------------------------
def judge_material(material: str, source: str = "", principle_ids: str = "") -> str:
    """判断一段材料是否违反原则（规则层 + JEV/LLM 模型层，结果自动存为案例）。

    材料请**原样**传入，不要先总结 —— 证据引用需要原文。一次调用最多带
    `PRINCIPLE_MAX_PRINCIPLES_PER_JUDGE` 条原则（默认 12，按严重度自动挑）。

    Args:
        material: 被判断的原始文本（邮件/方案/报告/聊天记录…）。
        source: 材料来源（文件名、URL、「用户粘贴」），写进案例便于复查。
        principle_ids: 只判这些原则，逗号或空格分隔（留空 = 自动按严重度挑）。

    Returns:
        判断报告：整体结论、逐条「违反/未违反/存疑」+ 置信度 + 证据 + 备注。
    """
    try:
        ids = _split_ids(principle_ids)
        judgement = api.judge(
            material, principle_ids=ids or None, source=source, settings=current_settings()
        )
        return format_judgement(judgement)
    except api.ApiError as exc:
        return f"[错误] {exc}"
    except Exception as exc:  # noqa: BLE001  工具永不抛异常
        return f"[错误] 判断失败（{type(exc).__name__}）：{exc}"


def judge_file(path: str, principle_ids: str = "") -> str:
    """读取本地文件并判断它是否违反原则（等价于 read_text_file + judge_material）。

    Args:
        path: 文件路径（绝对路径，或相对当前目录）。
        principle_ids: 只判这些原则，逗号或空格分隔（留空 = 自动）。

    Returns:
        判断报告，或读文件失败的原因。
    """
    try:
        ids = _split_ids(principle_ids)
        judgement = api.judge_file(path, principle_ids=ids or None, settings=current_settings())
        return format_judgement(judgement)
    except api.NotFoundError as exc:
        return f"[错误] {exc}"
    except api.ApiError as exc:
        return f"[错误] {exc}"
    except Exception as exc:  # noqa: BLE001  工具永不抛异常
        return f"[错误] 判断失败（{type(exc).__name__}）：{exc}"


def show_case(case_id: str) -> str:
    """看一次历史判断的完整报告（用来解释「上次为什么判违反」）。

    Args:
        case_id: 案例号，如 `C-20261105-101905-3f7a`。

    Returns:
        报告全文；找不到时提示用 list_cases 查最近的案例号。
    """
    try:
        case = api.get_case(case_id, settings=current_settings())
    except api.NotFoundError:
        return f"[错误] 没有 {case_id} 这个案例。用 list_cases 看最近的案例号。"
    except api.ApiError as exc:
        return f"[错误] {exc}"
    return f"{format_judgement(case)}\n材料：{case.subject}"


def list_cases(principle_id: str = "", limit: int = 5) -> str:
    """列出最近的判断案例（可只看某条原则涉及的案例）。

    Args:
        principle_id: 只看涉及这条原则的案例（留空 = 全部）。
        limit: 最多列几条（默认 5）。

    Returns:
        案例清单：案例号 / 时间 / 结论 / 后端 / 材料来源 / 违反的原则。
    """
    try:
        cases = api.list_cases(
            principle_id=principle_id,
            limit=max(1, min(50, _to_int(limit, 5, minimum=1))),
            settings=current_settings(),
        )
    except api.ApiError as exc:
        return f"[错误] {exc}"
    if not cases:
        return "还没有判断案例。用 judge_material / judge_file 做第一次判断。"
    marks = {"violation": "违反", "pass": "通过", "uncertain": "存疑"}
    lines: list[str] = []
    for case in cases:
        hits = "、".join(v.principle_id for v in case.violations) or "-"
        lines.append(
            f"{case.case_id}  {case.created_at}  {marks.get(case.overall, case.overall)}"
            f"  [{case.backend}]  材料 {case.subject[:40]}  违反: {hits}"
        )
    return "\n".join(lines)


def record_case_feedback(
    case_id: str,
    agree: bool,
    principle_id: str = "",
    note: str = "",
) -> str:
    """记录人工对一次判断的反馈（同意/不同意），用于发现哪条原则判不准。

    这是「痛苦 + 反思 = 进步」的落点：反馈不改写历史案例，但会累积成
    「哪条原则误判率最高」的统计，提示去修订它的 criteria / rules。

    Args:
        case_id: 案例号。
        agree: 是否同意这次判断。
        principle_id: 只对某一条原则的结论表态时填它（留空 = 针对整体结论）。
        note: 人话备注：为什么不同意、正确的判断应该是什么。

    Returns:
        结果说明。
    """
    try:
        record = api.record_feedback(
            case_id,
            agree=agree,
            principle_id=principle_id,
            note=note,
            settings=current_settings(),
        )
    except api.NotFoundError:
        return f"[错误] 没有 {case_id} 这个案例。用 list_cases 查最近的案例号。"
    except api.ApiError as exc:
        return f"[错误] {exc}"
    verdict = "同意" if agree else "不同意"
    target = f"对 {principle_id} 的结论" if principle_id else "整体结论"
    return f"已记录：{record['case_id']} —— {target}「{verdict}」{('：' + record['note']) if record['note'] else ''}"


# ---------------------------------------------------------------------------
# 材料获取
# ---------------------------------------------------------------------------
def read_text_file(path: str, max_chars: int = 0) -> str:
    """读取本地文本文件（判断前先读材料；单次默认最多 8000 字符）。

    Args:
        path: 文件路径（绝对路径，或相对当前目录）。
        max_chars: 最多读多少字符；0 = 用配置里的默认值（默认 8000）。

    Returns:
        文件正文（带路径与长度），或读失败的原因。
    """
    settings = current_settings()
    requested = _to_int(max_chars, 0, minimum=0)
    limit = max(200, requested if requested else settings.max_read_chars)
    text, error = _read_source(path, limit)
    if error:
        return error
    return f"文件 {path}（{len(text)} 字符）：\n{text}"


# `fetch_url` 已移除：agent 本身有通用能力（Bash / PowerShell），抓网页是衍生能力，
# 不值得占一个域工具位，也少一个 SSRF 面。需要时用 `curl` / `Invoke-WebRequest`。


# ---------------------------------------------------------------------------
# 通用能力：目录与全文检索
# ---------------------------------------------------------------------------
#: 检索时跳过的目录：版本库、依赖、缓存、构建产物 —— 它们不是「内容」，
#: 搜进去只会让结果被 node_modules 淹没。
SKIP_DIRS = frozenset(
    {".git", ".hg", ".svn", "node_modules", "__pycache__", ".pytest_cache", ".mypy_cache",
     ".ruff_cache", ".venv", "venv", "env", "dist", "build", ".idea", ".vscode", ".tox"}
)

#: 单文件检索上限：超过就不搜（1MB 的日志搜起来只会拖慢整轮对话）。
MAX_SEARCH_BYTES = 1_000_000


def list_dir(path: str = ".", show_hidden: bool = False, max_items: int = 200) -> str:
    """列出一个目录里有什么（文件名 + 大小 + 类型）。

    什么时候该调我：不知道文件在哪、要给用户确认目录结构、或判断某个路径是文件还是目录。

    Args:
        path: 目录路径（默认当前目录）。
        show_hidden: 是否显示 `.` 开头的隐藏项（默认不显示）。
        max_items: 最多列多少条（默认 200，防止大目录刷屏）。

    Returns:
        目录清单；路径不存在或不是目录时返回 `[错误] …`。
    """
    base = Path(path or ".").expanduser()
    try:
        if not base.exists():
            return f"[错误] 路径不存在：{base}"
        if not base.is_dir():
            return f"[错误] 不是目录：{base}（要读文件用 read_text_file / Read）"
        entries = []
        for child in sorted(base.iterdir(), key=lambda p: (not _is_dir(p), p.name.lower())):
            if not show_hidden and child.name.startswith("."):
                continue
            entries.append(child)
    except OSError as exc:
        return f"[错误] 列目录失败（{type(exc).__name__}）：{exc}"

    total = len(entries)
    shown = entries[: max(1, _to_int(max_items, 200, minimum=1))]
    lines = [f"目录 {base}（共 {total} 项，显示 {len(shown)} 项）："]
    for child in shown:
        if _is_dir(child):
            lines.append(f"  [目录] {child.name}/")
            continue
        try:
            size = child.stat().st_size
            lines.append(f"  [文件] {child.name}  {_human_size(size)}")
        except OSError:
            lines.append(f"  [文件] {child.name}  （大小未知）")
    if total > len(shown):
        lines.append(f"  …还有 {total - len(shown)} 项未显示（调大 max_items 可看全）")
    return "\n".join(lines)


def search_files(
    pattern: str,
    path: str = ".",
    glob: str = "",
    regex: bool = False,
    ignore_case: bool = False,
    max_results: int = 50,
    context: int = 0,
) -> str:
    """在文件里按内容检索，返回「文件:行号: 内容」清单。

    什么时候该调我：知道要找什么文字、但不知道在哪个文件；或要在整个项目里
    确认某段代码/某个说法出现过几次。**先用它定位，再用 read_text_file / Read 精读**，
    比逐个文件读要省得多。这是纯 Python 实现，不依赖 ripgrep。

    Args:
        pattern: 要找的文本；`regex=True` 时按正则解释。
        path: 检索起点（目录或单个文件，默认当前目录）。
        glob: 只搜匹配此通配符的文件名，如 `*.py`、`test_*.py`（默认全部文本文件）。
        regex: pattern 是否按正则解释（默认 False，纯文本子串匹配）。
        ignore_case: 是否忽略大小写。
        max_results: 最多返回多少条命中（默认 50）。
        context: 每条命中额外显示前后各几行（默认 0）。

    Returns:
        命中清单；没有命中会明确说「无命中」，出错返回 `[错误] …`。
    """
    needle = pattern or ""
    if not needle:
        return "[错误] pattern 不能为空。"
    base = Path(path or ".").expanduser()
    if not base.exists():
        return f"[错误] 路径不存在：{base}"

    flags = re.IGNORECASE if ignore_case else 0
    matcher = None
    if regex:
        try:
            matcher = re.compile(needle, flags)
        except re.error as exc:
            return f"[错误] 正则写错了：{exc}"

    def hit(line: str) -> bool:
        if matcher is not None:
            return bool(matcher.search(line))
        return needle.lower() in line.lower() if ignore_case else needle in line

    if base.is_file():
        candidates = [base]
    else:
        candidates = [p for p in _iter_files(base)]
    limit = max(1, _to_int(max_results, 50, minimum=1))
    window = max(0, _to_int(context, 0, minimum=0))

    found: list[str] = []
    scanned = 0
    # 命中数要单独数：`context > 0` 时 `found` 里还混着上下文行，
    # 用 `len(found)` 当命中数会让「命中 N 条」虚高、实际返回的匹配远少于
    # `max_results`（OCR 复审发现）。
    hits = 0
    for file in candidates:
        # 用 fnmatch 匹配**文件名**，不用 `Path.match`：后者对 `*.py` 只匹配
        # 当前层，而 `_iter_files` 是递归遍历的 —— 用户传 `*.py` 期望搜全部，
        # 实际只搜到根目录那层（复审前一轮发现）。
        if glob and not fnmatch.fnmatch(file.name, glob):
            continue
        try:
            if file.stat().st_size > MAX_SEARCH_BYTES:
                continue
            raw = file.read_bytes()
        except OSError:
            continue
        if b"\x00" in raw[:2048]:  # 二进制，跳过
            continue
        scanned += 1
        try:
            text = raw.decode("utf-8", "replace")
        except Exception:  # noqa: BLE001
            continue
        lines = text.splitlines()
        for index, line in enumerate(lines):
            if not hit(line):
                continue
            hits += 1
            if window:
                start, end = max(0, index - window), min(len(lines), index + window + 1)
                for offset in range(start, end):
                    marker = ">" if offset == index else " "
                    found.append(f"{file}:{offset + 1}:{marker} {lines[offset].rstrip()}")
            else:
                found.append(f"{file}:{index + 1}: {line.rstrip()}")
            if hits >= limit:
                break
        if hits >= limit:
            break

    if not found:
        return f"在 {base} 下搜「{needle}」：无命中（扫描 {scanned} 个文件）。"
    head = f"在 {base} 下搜「{needle}」：命中 {hits} 条" + ("（已截断）" if hits >= limit else "")
    return head + "\n" + "\n".join(found)


def _is_dir(path: Path) -> bool:
    try:
        return path.is_dir()
    except OSError:
        return False


def _human_size(size: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f}{unit}" if unit == "B" else f"{size:.1f}{unit}"
        size /= 1024.0
    return f"{size:.1f}GB"


def _iter_files(root: Path):
    """深度优先遍历，跳过 SKIP_DIRS 与隐藏目录（`.git` 之类）。

    **不跟随符号链接目录**（OCR 复审发现）：`Path.is_dir()` 会跟随链接，
    一个指向自己祖先的软链会让遍历永远跑不完。这里用 `is_symlink()` 直接跳过，
    比「记录已访问路径」更简单，也够用 —— 搜索不需要穿过软链。
    """
    stack = [root]
    while stack:
        current = stack.pop()
        try:
            children = list(current.iterdir())
        except OSError:
            continue
        for child in children:
            if _is_dir(child):
                if child.name in SKIP_DIRS or child.name.startswith("."):
                    continue
                if child.is_symlink():
                    continue  # 软链目录可能成环，跳过
                stack.append(child)
            else:
                yield child
