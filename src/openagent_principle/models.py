"""数据模型：一条原则长什么样、一次判断长什么样。

这里刻意让**判定标准**先于**文案**：一条原则如果只有一句漂亮话（「要坦诚」），
它就不是原则，是口号 —— 模型没法据此判断违反与否，人也说不清。所以 `Principle`
必须能回答三个问题：

1. 它说什么？（`statement`）
2. 什么算违反？（`criteria` 人话 + `examples` 反例）
3. 违反了有多严重？（`severity` 1~5）

判断本身完全交给模型（JEV / LLM）：原则写得越可判定，模型判得越准。
程序不替模型做字面匹配 —— 那会把「不认识否定句」之类的死板约束带进结论。
"""

from __future__ import annotations

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


def now_iso() -> str:
    """本地时区的 ISO 时间戳（秒精度）—— 日志/案例里人能直接读。"""
    return datetime.now().astimezone().isoformat(timespec="seconds")


def new_case_id() -> str:
    """案例号：`C-20261105-101905-3f7a` —— 按时间可排序，尾缀防同秒碰撞。"""
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return f"C-{stamp}-{secrets.token_hex(2)}"


class ModelError(ValueError):
    """模型层的数据不合法（原则字段缺失 / 严重度越界）。"""


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
    severity: int = 3
    tags: list[str] = field(default_factory=list)
    source: str = "user"
    version: int = 1
    archived: bool = False
    created_at: str = ""
    updated_at: str = ""

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
            "severity": self.severity,
            "tags": list(self.tags),
            "source": self.source,
            "version": self.version,
            "archived": self.archived,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
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
            severity=int(data.get("severity") or 3),
            tags=[str(x) for x in (data.get("tags") or [])],
            source=str(data.get("source") or "user"),
            version=int(data.get("version") or 1),
            archived=bool(data.get("archived")),
            created_at=str(data.get("created_at") or ""),
            updated_at=str(data.get("updated_at") or ""),
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
    source: str = "model"  # 结论来源：model

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
