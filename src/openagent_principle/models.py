"""数据模型：一条原则长什么样、一次判断长什么样。

这里刻意让**判定标准**先于**文案**：一条原则如果只有一句漂亮话（「要坦诚」），
它就不是原则，是口号 —— 模型没法据此判断违反与否，人也说不清。所以 `Principle`
必须能回答三个问题：

1. 它说什么？（`statement`）
2. 什么算违反？（`criteria` 人话 + `rules` 机器可判 + `examples` 反例）
3. 违反了有多严重？（`severity` 1~5）

`Rule` 是**确定性**那一层：`must_not_contain_any(["保证","绝对没问题"])` 这种判断
不需要模型、不花钱、可复现。模型（Jev）负责规则覆盖不到的语义判断。两层合并后
才是一次 `Judgement` —— 规则命中是硬证据，模型不能推翻（见 `judge.merge`）。
"""

from __future__ import annotations

import json
import re
import secrets
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

#: 严重度词表：1 = 提醒，5 = 红线。判断结果里带这个词，人一眼知道该不该管。
SEVERITY_LABELS = {
    1: "可忽略",
    2: "轻微",
    3: "中等",
    4: "严重",
    5: "红线",
}

#: 规则类型 → 语义。统一约定：**每条规则都是「必须成立」的断言**，
#: 不成立即违规信号。这样 `passed=False` 永远等于「命中了一条违规规则」。
RULE_KINDS = {
    "must_contain_any": "材料必须包含其中任一短语",
    "must_contain_all": "材料必须包含全部短语",
    "must_not_contain_any": "材料不得包含其中任一短语",
    "must_match": "材料必须匹配这个正则",
    "must_not_match": "材料不得匹配这个正则",
    "min_chars": "材料长度不得少于 N 字符",
    "max_chars": "材料长度不得超过 N 字符",
}

#: 正则安全阀：用户写的正则可能灾难性回溯（ReDoS），而这里要判的是**不可信材料**。
MAX_REGEX_LEN = 300


def now_iso() -> str:
    """本地时区的 ISO 时间戳（秒精度）—— 日志/案例里人能直接读。"""
    return datetime.now().astimezone().isoformat(timespec="seconds")


def new_case_id() -> str:
    """案例号：`C-20261105-101905-3f7a` —— 按时间可排序，尾缀防同秒碰撞。"""
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return f"C-{stamp}-{secrets.token_hex(2)}"


class ModelError(ValueError):
    """模型层的数据不合法（原则字段缺失 / 规则类型未知 / 正则写坏）。"""


# ---------------------------------------------------------------------------
# 规则
# ---------------------------------------------------------------------------
@dataclass
class Rule:
    """一条机器可判的断言。`kind` 见 `RULE_KINDS`。"""

    kind: str
    value: str | list[str] = ""
    note: str = ""
    weight: float = 1.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "value": self.value,
            "note": self.note,
            "weight": self.weight,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Rule":
        return cls(
            kind=str(data.get("kind") or "").strip(),
            value=data.get("value", ""),
            note=str(data.get("note") or ""),
            weight=float(data.get("weight") or 1.0),
        )

    def validate(self) -> None:
        """规则写错要在**保存时**就报，而不是等到判断时才炸。

        Raises:
            ModelError: 未知类型 / 空值 / 正则过长或写坏 / 数字规则不是数字。
        """
        if self.kind not in RULE_KINDS:
            raise ModelError(
                f"未知规则类型 {self.kind!r}，可选：{', '.join(RULE_KINDS)}"
            )
        if self.kind in ("must_match", "must_not_match"):
            pattern = str(self.value or "")
            if not pattern:
                raise ModelError(f"规则 {self.kind} 需要一个正则表达式")
            if len(pattern) > MAX_REGEX_LEN:
                raise ModelError(f"正则过长（>{MAX_REGEX_LEN} 字符），拒绝编译")
            try:
                re.compile(pattern)
            except re.error as exc:
                raise ModelError(f"正则写坏了：{exc}") from exc
            return
        if self.kind in ("min_chars", "max_chars"):
            try:
                number = int(str(self.value).strip())
            except ValueError as exc:
                raise ModelError(f"规则 {self.kind} 需要一个整数") from exc
            if number < 0:
                raise ModelError(f"规则 {self.kind} 不能为负数")
            return
        # 短语类规则
        if isinstance(self.value, str):
            items = [self.value]
        elif isinstance(self.value, (list, tuple)):
            items = [str(v) for v in self.value]
        else:
            raise ModelError(f"规则 {self.kind} 的 value 需要字符串或字符串列表")
        items = [i.strip() for i in items if str(i).strip()]
        if not items:
            raise ModelError(f"规则 {self.kind} 至少需要一个短语")
        self.value = items  # 统一成列表：JSON 里形态一致，比较也一致

    @property
    def label(self) -> str:
        """给人看的短描述（案例报告里用）。"""
        value = self.value if isinstance(self.value, str) else "、".join(map(str, self.value))
        return f"{self.kind}: {value}"


def parse_rules(raw: str | list[Any] | None) -> list[Rule]:
    """把工具参数里的规则解析成 `Rule` 列表。

    接受三种形态（都常见，都别让人猜）：
    * 空 → `[]`
    * JSON 字符串：`[{"kind":"must_not_contain_any","value":["保证"]}]`
    * 已在内存里的 list（测试/内部调用）

    Args:
        raw: 原始输入。

    Returns:
        校验过的 `Rule` 列表（空列表表示没有机器可判规则）。

    Raises:
        ModelError: JSON 坏了 / 不是列表 / 某条规则不合法。
    """
    if not raw:
        return []
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            return []
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ModelError(f"rules 不是合法 JSON：{exc}") from exc
    else:
        data = raw
    if isinstance(data, dict):
        data = [data]
    if not isinstance(data, list):
        raise ModelError("rules 需要是数组：[{kind, value, note}]")
    rules: list[Rule] = []
    for item in data:
        if not isinstance(item, dict):
            raise ModelError(f"每条规则需要是对象，收到 {type(item).__name__}")
        rule = Rule.from_dict(item)
        rule.validate()
        rules.append(rule)
    return rules


# ---------------------------------------------------------------------------
# 原则
# ---------------------------------------------------------------------------
@dataclass
class Principle:
    """一条可执行的原则。字段顺序 = 从「是什么」到「怎么判」。"""

    id: str
    title: str
    statement: str
    rationale: str = ""
    scope: str = ""
    criteria: str = ""
    examples: list[str] = field(default_factory=list)
    rules: list[Rule] = field(default_factory=list)
    severity: int = 3
    tags: list[str] = field(default_factory=list)
    source: str = "user"
    version: int = 1
    archived: bool = False
    created_at: str = ""
    updated_at: str = ""
    #: 结构化的适用范围：材料必须命中其中任一短语才算「在范围内」。
    #: 空 = 不限范围（老行为）。为什么需要它：`scope` 只是给人看的自由文本，
    #: 规则层读不懂它 —— 于是「仅复盘」的原则会对一份天气报告硬判「缺少改进」。
    #: 有了 `scope_keywords`，规则层就能在跑规则**之前**先判断适用性。
    scope_keywords: list[str] = field(default_factory=list)

    # ------------------------------------------------------------- 校验
    def validate(self) -> None:
        """保存前的最后一道闸：没有标题/陈述的原则等于噪音。"""
        if not self.id.strip():
            raise ModelError("原则缺少 id")
        if not self.title.strip():
            raise ModelError("原则缺少 title")
        if not self.statement.strip():
            raise ModelError("原则缺少 statement（一句话说清它要求什么）")
        if not 1 <= int(self.severity) <= 5:
            raise ModelError("severity 需要 1~5")
        for rule in self.rules:
            rule.validate()

    def applies_to(self, text: str) -> bool:
        """材料是否落在这条原则的适用范围内。

        判定规则（刻意保守）：
        * 没有 `scope_keywords` → 一律适用（老原则不受影响）；
        * 有 → 材料里出现任一关键词才算适用（不区分大小写）。

        为什么「不命中就跳过」而不是「不命中就判违规」：范围外材料本来就不该
        被这条原则管。把「不属于我管」当成「违反了」是误报的最大来源。
        """
        keywords = [k.strip() for k in self.scope_keywords if str(k).strip()]
        if not keywords:
            return True
        lowered = (text or "").lower()
        return any(k.lower() in lowered for k in keywords)

    # ------------------------------------------------------------- 序列化
    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "statement": self.statement,
            "rationale": self.rationale,
            "scope": self.scope,
            "criteria": self.criteria,
            "examples": list(self.examples),
            "rules": [r.to_dict() for r in self.rules],
            "severity": self.severity,
            "tags": list(self.tags),
            "source": self.source,
            "version": self.version,
            "archived": self.archived,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "scope_keywords": list(self.scope_keywords),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Principle":
        return cls(
            id=str(data.get("id") or ""),
            title=str(data.get("title") or ""),
            statement=str(data.get("statement") or ""),
            rationale=str(data.get("rationale") or ""),
            scope=str(data.get("scope") or ""),
            criteria=str(data.get("criteria") or ""),
            examples=[str(x) for x in (data.get("examples") or [])],
            rules=[Rule.from_dict(r) for r in (data.get("rules") or []) if isinstance(r, dict)],
            severity=int(data.get("severity") or 3),
            tags=[str(x) for x in (data.get("tags") or [])],
            source=str(data.get("source") or "user"),
            version=int(data.get("version") or 1),
            archived=bool(data.get("archived")),
            created_at=str(data.get("created_at") or ""),
            updated_at=str(data.get("updated_at") or ""),
            scope_keywords=[str(x) for x in (data.get("scope_keywords") or [])],
        )

    # ------------------------------------------------------------- 展示
    @property
    def severity_label(self) -> str:
        return SEVERITY_LABELS.get(int(self.severity), str(self.severity))

    def one_line(self) -> str:
        """列表里的一行：id / 标题 / 严重度 / 作用域。"""
        scope = f" | 适用: {self.scope}" if self.scope else ""
        return (
            f"{self.id}  {self.title}  [{self.severity_label}]{scope}"
            f"{'  (已归档)' if self.archived else ''}"
        )

    def full_text(self) -> str:
        """完整展开 —— 给模型/人判断时看的版本（也是 JEV instructions 的素材）。"""
        lines = [f"[{self.id}] {self.title}", f"陈述：{self.statement}"]
        if self.rationale:
            lines.append(f"理由：{self.rationale}")
        if self.scope:
            lines.append(f"适用范围：{self.scope}")
        if self.criteria:
            lines.append(f"判定标准：{self.criteria}")
        if self.examples:
            lines.append("例子：" + "；".join(self.examples))
        if self.rules:
            lines.append("机器可判规则：" + "；".join(r.label for r in self.rules))
        lines.append(f"严重度：{self.severity}（{self.severity_label}）")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# 判断
# ---------------------------------------------------------------------------
@dataclass
class Verdict:
    """对**一条**原则的判断结果。`violated=None` 表示「说不准」，不是「没事」。"""

    principle_id: str
    title: str
    violated: bool | None
    probability: float = 0.0
    confidence: float = 0.0
    severity: int = 3
    evidence: list[str] = field(default_factory=list)
    reasoning: str = ""
    source: str = "model"  # model | rule | merged

    @property
    def label(self) -> str:
        if self.violated is None:
            return "存疑"
        return "违反" if self.violated else "未违反"

    def to_dict(self) -> dict[str, Any]:
        return {
            "principle_id": self.principle_id,
            "title": self.title,
            "violated": self.violated,
            "probability": round(float(self.probability), 4),
            "confidence": round(float(self.confidence), 4),
            "severity": self.severity,
            "evidence": list(self.evidence),
            "reasoning": self.reasoning,
            "source": self.source,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Verdict":
        violated = data.get("violated")
        return cls(
            principle_id=str(data.get("principle_id") or ""),
            title=str(data.get("title") or ""),
            violated=None if violated is None else bool(violated),
            probability=float(data.get("probability") or 0.0),
            confidence=float(data.get("confidence") or 0.0),
            severity=int(data.get("severity") or 3),
            evidence=[str(x) for x in (data.get("evidence") or [])],
            reasoning=str(data.get("reasoning") or ""),
            source=str(data.get("source") or "model"),
        )


#: 整体结论的三种取值。刻意不给「部分违反」—— 那由逐条 `Verdict` 表达，
#: 整体只回答「这份材料该不该被打回」。
OVERALL_VALUES = ("pass", "violation", "uncertain")


@dataclass
class Judgement:
    """一次完整判断：材料 + 用到的原则 + 逐条结论 + 后端自证。"""

    case_id: str
    subject: str
    material_chars: int
    overall: str
    score: float
    severity: int
    verdicts: list[Verdict]
    backend: str
    model: str = ""
    usage: dict[str, Any] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    created_at: str = ""

    @property
    def violations(self) -> list[Verdict]:
        """确认违反的条目（按严重度降序）—— 报告里最该先看的一屏。"""
        hits = [v for v in self.verdicts if v.violated is True]
        return sorted(hits, key=lambda v: v.severity, reverse=True)

    @property
    def uncertain(self) -> list[Verdict]:
        return [v for v in self.verdicts if v.violated is None]

    def to_dict(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id,
            "subject": self.subject,
            "material_chars": self.material_chars,
            "overall": self.overall,
            "score": round(float(self.score), 4),
            "severity": self.severity,
            "verdicts": [v.to_dict() for v in self.verdicts],
            "backend": self.backend,
            "model": self.model,
            "usage": dict(self.usage),
            "notes": list(self.notes),
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Judgement":
        return cls(
            case_id=str(data.get("case_id") or ""),
            subject=str(data.get("subject") or ""),
            material_chars=int(data.get("material_chars") or 0),
            overall=str(data.get("overall") or "uncertain"),
            score=float(data.get("score") or 0.0),
            severity=int(data.get("severity") or 0),
            verdicts=[Verdict.from_dict(v) for v in (data.get("verdicts") or []) if isinstance(v, dict)],
            backend=str(data.get("backend") or ""),
            model=str(data.get("model") or ""),
            usage=dict(data.get("usage") or {}),
            notes=[str(x) for x in (data.get("notes") or [])],
            created_at=str(data.get("created_at") or ""),
        )
