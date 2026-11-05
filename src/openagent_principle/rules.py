"""确定性规则引擎：判断里**不花模型、不花钱、可复现**的那一半。

为什么要有这一层：《原则》的落点是「可执行的判据」。凡是能写成短语表/正则/长度的
判据，都不必问模型 —— 模型既贵又不稳定，同一段材料两次判断可能给出不同结论。
规则层给出的是**硬证据**：命中了就是命中了，引用得出原文，人不服可以去核对。

规则语义（与 `models.RULE_KINDS` 一致）：每条规则都是「必须成立」的断言，
`passed=False` 等于「命中一条违规」。这样合并时的逻辑只有一句话：
**规则说违反了，模型不能推翻**（见 `judge.merge`）。
"""

from __future__ import annotations

import multiprocessing
import re
import signal
import threading
from dataclasses import dataclass, field
from typing import Any

from .models import Principle, Rule

#: 证据引用时，命中短语前后各留多少字符的上下文。
EVIDENCE_PAD = 40

#: 单条证据最多多长（防止把整段材料抄进案例日志）。
EVIDENCE_MAX = 240

#: 单条正则的执行上限（秒）。300 字符的长度限制防不住灾难性回溯 ——
#: `(a+)+$` 只有 6 个字符，配 30 个 a 就能跑几分钟（复审 5.7）。
REGEX_TIMEOUT = 2.0

#: 超时标记：`_safe_search` 超时后置位，调用方据此区分「没匹配」与「没判完」。
_TIMEOUT_FLAG = threading.local()


def _regex_worker(pattern: str, text: str, conn) -> None:
    """子进程里跑正则，把 `(start, end)` 或 `None` 发回去。

    为什么必须是**子进程**而不是线程：Python 没法从外部杀线程。线程超时后
    仍在后台烧 CPU（实测一次 ReDoS 测试烧了 73 分钟），`join(timeout)` 只是
    让调用方不等，问题没解决。子进程可以 `terminate()`，CPU 立刻释放。
    """
    try:
        match = re.compile(pattern).search(text)
        conn.send(None if match is None else (match.start(), match.end()))
    except Exception:  # noqa: BLE001  子进程里任何异常都当作「没匹配」
        conn.send(None)
    finally:
        conn.close()


def _safe_search(pattern: re.Pattern[str], text: str) -> tuple[int, int] | None:
    """带超时的 `pattern.search`，返回 `(start, end)` 或 `None`。

    * 主线程（POSIX）优先用 `SIGALRM` —— 零开销、精确；
    * 其他情况（Windows / 非主线程）用**子进程** + `terminate()` —— 超时能真正
      杀掉，不留后台线程烧 CPU。

    超时返回 `None` 并置 `_TIMEOUT_FLAG.timed_out`，由调用方决定怎么报告。
    """
    _TIMEOUT_FLAG.timed_out = False

    if hasattr(signal, "SIGALRM") and threading.current_thread() is threading.main_thread():
        def _on_alarm(signum, frame):  # noqa: ARG001
            raise TimeoutError

        old = signal.signal(signal.SIGALRM, _on_alarm)
        signal.setitimer(signal.ITIMER_REAL, REGEX_TIMEOUT)
        try:
            match = pattern.search(text)
            return None if match is None else (match.start(), match.end())
        except TimeoutError:
            _TIMEOUT_FLAG.timed_out = True
            return None
        finally:
            signal.setitimer(signal.ITIMER_REAL, 0)
            signal.signal(signal.SIGALRM, old)

    # Windows / 非主线程：子进程隔离
    ctx = multiprocessing.get_context("spawn")
    parent_conn, child_conn = ctx.Pipe(duplex=False)
    proc = ctx.Process(target=_regex_worker, args=(pattern.pattern, text, child_conn), daemon=True)
    proc.start()
    child_conn.close()
    try:
        if not parent_conn.poll(REGEX_TIMEOUT):
            _TIMEOUT_FLAG.timed_out = True
            return None
        return parent_conn.recv()
    except (EOFError, OSError):
        return None
    finally:
        parent_conn.close()
        if proc.is_alive():
            proc.terminate()
        proc.join(timeout=1.0)


def _timed_out(pattern: re.Pattern[str], text: str) -> bool:
    """上一次 `_safe_search` 是否超时。"""
    return bool(getattr(_TIMEOUT_FLAG, "timed_out", False))


@dataclass
class RuleHit:
    """一条规则的**确定性匹配结果** + 可核对的原文证据。

    注意：`passed=False` 只表示「这条规则的字面条件没满足」，**不等于**「违反原则」。
    是否构成违反由 JEV 裁决（架构变更）。`violated` 这个属性名保留是为了兼容，
    语义上应读作「这条规则未通过」。
    """

    rule: Rule
    passed: bool
    detail: str
    evidence: list[str] = field(default_factory=list)

    @property
    def violated(self) -> bool:
        return not self.passed


@dataclass
class RuleReport:
    """规则层对一条原则的**完整报告** —— 交给 JEV 的结构化输入。

    规则层只回答三个问题，不回答「算不算违规」：
    1. 材料在不在适用范围？（`in_scope`）
    2. 每条规则的字面条件满足没有？（`hits`）
    3. 命中了哪些原文？（`evidence`）

    为什么要有这个类型：以前规则层直接产出 `(violated, confidence, ...)`，
    等于让「字面匹配」冒充「语义判断」。现在它只产出报告，由 JEV 决定结论。
    """

    principle_id: str
    in_scope: bool
    hits: list[RuleHit] = field(default_factory=list)
    #: 范围外时的说明（为什么没判）。
    scope_note: str = ""

    @property
    def has_rules(self) -> bool:
        return bool(self.hits)

    @property
    def failed(self) -> list[RuleHit]:
        """字面条件未满足的规则（**不是**「违反原则」）。"""
        return [h for h in self.hits if not h.passed]

    @property
    def evidence(self) -> list[str]:
        """所有未通过规则的原文引用（去重保序）。"""
        out: list[str] = []
        for hit in self.failed:
            for item in hit.evidence:
                if item not in out:
                    out.append(item)
        return out

    def as_prompt(self) -> str:
        """渲染成给 JEV 看的一段结构化输入。"""
        if not self.in_scope:
            return f"【规则层】{self.scope_note or '材料不在适用范围'}。请自行判断是否适用。"
        if not self.hits:
            return "【规则层】这条原则没有机器可判规则，请完全依据语义判断。"
        lines = ["【规则层：确定性匹配结果（仅供参考，你可以推翻）】"]
        for hit in self.hits:
            mark = "未通过" if not hit.passed else "通过"
            lines.append(f"- [{mark}] {hit.rule.label} —— {hit.detail}")
            for item in hit.evidence[:2]:
                lines.append(f"    原文：{item}")
        if self.failed:
            lines.append(
                "注意：以上「未通过」只是字面条件不满足，**不等于**违反原则。"
                "请结合原则陈述与判定标准，判断它是否真的构成违反。"
            )
        return "\n".join(lines)

    def to_dict(self) -> dict[str, Any]:
        return {
            "principle_id": self.principle_id,
            "in_scope": self.in_scope,
            "scope_note": self.scope_note,
            "hits": [
                {
                    "rule": h.rule.label,
                    "passed": h.passed,
                    "detail": h.detail,
                    "evidence": list(h.evidence),
                }
                for h in self.hits
            ],
        }


def _clip(text: str) -> str:
    """把证据压到一行、并截到可读长度。"""
    flat = " ".join(str(text).split())
    return flat[:EVIDENCE_MAX] + ("…" if len(flat) > EVIDENCE_MAX else "")


def _quote(text: str, start: int, end: int) -> str:
    """取命中位置前后文，标出命中处：`…前文【命中】后文…`。"""
    left = max(0, start - EVIDENCE_PAD)
    right = min(len(text), end + EVIDENCE_PAD)
    head = "…" if left > 0 else ""
    tail = "…" if right < len(text) else ""
    return _clip(f"{head}{text[left:start]}【{text[start:end]}】{text[end:right]}{tail}")


def _phrases(rule: Rule) -> list[str]:
    """规则里的短语表（统一成非空字符串列表）。"""
    value = rule.value
    if isinstance(value, str):
        items = [value]
    else:
        items = list(value)
    return [str(i).strip() for i in items if str(i).strip()]


def evaluate_rule(text: str, rule: Rule) -> RuleHit:
    """对一段材料执行一条规则。

    短语匹配**不区分大小写**（中文不受影响，英文大小写不该造成漏判）；
    正则沿用写规则时的大小写语义 —— 要忽略大小写就写 `(?i)`，别替用户做主。

    Args:
        text: 被判断的材料。
        rule: 一条已校验的规则。

    Returns:
        `RuleHit`（永不带异常 —— 判断链不能因为一条规则写法奇怪就断掉）。
    """
    body = text or ""
    kind = rule.kind
    lowered = body.lower()

    if kind in ("must_contain_any", "must_contain_all", "must_not_contain_any"):
        phrases = _phrases(rule)
        hits: list[tuple[str, int, int]] = []
        for phrase in phrases:
            idx = lowered.find(phrase.lower())
            if idx >= 0:
                hits.append((phrase, idx, idx + len(phrase)))
        if kind == "must_contain_any":
            if hits:
                return RuleHit(rule, True, f"包含：{'、'.join(p for p, _, _ in hits)}",
                               [_quote(body, s, e) for _, s, e in hits[:2]])
            return RuleHit(rule, False, f"缺少任何一个必需短语：{'、'.join(phrases)}")
        if kind == "must_contain_all":
            missing = [p for p in phrases if not any(p == h[0] for h in hits)]
            if not missing:
                return RuleHit(rule, True, f"全部包含：{'、'.join(phrases)}",
                               [_quote(body, s, e) for _, s, e in hits[:2]])
            return RuleHit(rule, False, f"缺少：{'、'.join(missing)}")
        # must_not_contain_any
        if hits:
            return RuleHit(rule, False, f"出现了禁止出现的短语：{'、'.join(p for p, _, _ in hits)}",
                           [_quote(body, s, e) for _, s, e in hits[:3]])
        return RuleHit(rule, True, "未出现禁止短语")

    if kind in ("must_match", "must_not_match"):
        try:
            pattern = re.compile(str(rule.value))
        except re.error as exc:  # 保存时已校验；这里只防手改文件
            return RuleHit(rule, False, f"正则无法编译（{exc}）", [])
        span = _safe_search(pattern, body)
        if span is None and _timed_out(pattern, body):
            # 超时不能当成「没匹配」：那会让 must_not_match 静默放行、
            # must_match 静默判违规。如实报告「这条没判」。
            return RuleHit(rule, True, f"正则执行超时（>{REGEX_TIMEOUT:.1f}s），本条跳过", [])
        if kind == "must_match":
            if span is not None:
                start, end = span
                return RuleHit(rule, True, f"匹配到：{body[start:end][:60]!r}",
                               [_quote(body, start, end)])
            return RuleHit(rule, False, f"未匹配必需的模式：{rule.value}")
        if span is not None:
            start, end = span
            return RuleHit(rule, False, f"匹配到禁止的模式：{body[start:end][:60]!r}",
                           [_quote(body, start, end)])
        return RuleHit(rule, True, "未匹配禁止的模式")

    if kind in ("min_chars", "max_chars"):
        limit = int(str(rule.value).strip())
        length = len(body)
        if kind == "min_chars":
            ok = length >= limit
            return RuleHit(rule, ok, f"材料 {length} 字符，下限 {limit}")
        ok = length <= limit
        return RuleHit(rule, ok, f"材料 {length} 字符，上限 {limit}")

    # 未知类型：不当成违规（保存时已拦截），但要说清「这条没判」
    return RuleHit(rule, True, f"未知规则类型 {kind!r}，已跳过")


def evaluate(principle: Principle, text: str) -> list[RuleHit]:
    """对一条原则的所有规则跑一遍。

    Args:
        principle: 带 `rules` 的原则（没有规则就返回空列表）。
        text: 被判断的材料。

    Returns:
        与 `principle.rules` 等长的 `RuleHit` 列表。
    """
    return [evaluate_rule(text, rule) for rule in principle.rules]


def rule_report(principle: Principle, text: str) -> RuleReport:
    """跑一条原则的所有规则，产出**报告**（不是结论）。

    规则层只做三件事：适用范围检查、确定性匹配、原文取证。
    它**不**回答「算不算违反」—— 那是 JEV 的事（架构变更）。

    **适用范围先于规则**：`scope_keywords` 不命中时直接标记 `in_scope=False`，
    不跑规则。否则「缺少某个范围外必需短语」会被误当成违规信号（复审 4.1）。

    Args:
        principle: 原则。
        text: 材料。

    Returns:
        `RuleReport`：是否在范围内、每条规则的匹配结果、原文证据。
    """
    if not principle.applies_to(text):
        return RuleReport(
            principle_id=principle.id,
            in_scope=False,
            scope_note=f"材料不在适用范围（需含：{'、'.join(principle.scope_keywords)}）",
        )
    return RuleReport(
        principle_id=principle.id,
        in_scope=True,
        hits=evaluate(principle, text),
    )


def rule_verdict(principle: Principle, text: str) -> tuple[bool | None, float, list[str], list[str], str]:
    """**仅 `rules` 后端使用**：把规则报告压成一个最终结论。

    这是「显式 rules 模式」的专用路径 —— 用户明确要求「不调模型、只按规则判」
    时才走这里。默认路径（jev / llm）下规则层只产出 `RuleReport`，结论由 JEV 给。

    语义：
    * 材料不在适用范围 → `None`（「这条原则不管这份材料」）；
    * 有规则未通过 → 违反，置信度按权重算；
    * 全部通过且有规则覆盖 → 未违反，置信度 0.6（规则只能证明「没犯这几条」）；
    * 没有任何规则 → `None`（「规则层没有意见」）。

    Args:
        principle: 原则。
        text: 材料。

    Returns:
        `(violated, confidence, evidence, details, reasoning)`。
    """
    report = rule_report(principle, text)
    if not report.in_scope:
        return None, 0.0, [], [], report.scope_note
    if not report.hits:
        return None, 0.0, [], [], ""

    details = [
        f"{'✗' if not h.passed else '✓'} {h.rule.label} —— {h.detail}" for h in report.hits
    ]
    reasoning = "；".join(details)

    if report.failed:
        weight = sum(max(0.0, h.rule.weight) for h in report.failed)
        confidence = min(0.99, 0.75 + 0.06 * weight)
        return True, confidence, report.evidence, details, reasoning
    return False, 0.6, [], details, reasoning
