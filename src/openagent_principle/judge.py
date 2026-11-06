"""编排：把「原则 + 模型」合成一次可复盘、可核对的判断。

**判断完全交给模型。** 程序不做字面匹配、不替模型下结论 —— 它只负责把原则
写成可判定的输入、校验模型回答的协议、检测自相矛盾、留痕。

一次判断的固定流程（每一步都要在结果里留痕）：

1. **选原则**：显式指定的就用指定的；否则按严重度挑（JEV 按调用计费，一次装够）。
   原则库为空不是错误，而是一个结论：`uncertain` + 「先去加原则」。
2. **模型层**：把原则翻译成 choice/score（`jev.build_questions`），交给 JEV。
   JEV 不可用就换 LLM 答同一套题；两者都不可用就是**判断失败** —— 明确报错，
   不拿一个「假装在判」的降级糊弄人。
3. **合并**（见 `merge`）：模型自相矛盾（所选选项概率 < 0.6）一律降为存疑；
   模型没给答案时标存疑并写明「语义判断缺席」。
4. **落盘**：案例写进 `cases.jsonl`（只追加）。整体结论以模型的全局答案为准
   （`_resolve_overall`）；逐条与整体冲突时写进 `notes` 供复核，不改判。
   模型没给全局答案时，才由逐条推导并注明原因。
"""

from __future__ import annotations

from typing import Any, Sequence

from . import jev, llm
from .config import Settings, load_settings
from .models import (
    SEVERITY_LABELS,
    Judgement,
    Principle,
    Verdict,
    new_case_id,
    now_iso,
)
from .store import PrincipleStore


class JudgeError(RuntimeError):
    """判断失败（材料为空 / 没有可用后端）。消息必须是给人看的话。"""


# ---------------------------------------------------------------------------
# 后端选择
# ---------------------------------------------------------------------------
def choose_backend(settings: Settings, requested: str = "") -> tuple[str, list[str]]:
    """决定这次用哪个后端，并把「为什么」说清楚。

    `auto` 的降级表是固定的（不猜）：
        JEV 配好 → jev；否则 LLM 配好 → llm；都没有 → 抛 `JudgeError`。

    Args:
        settings: 配置。
        requested: 显式指定的后端（空 = 用配置里的）。

    Returns:
        `(backend, notes)`；`notes` 是要写进案例的说明。

    Raises:
        JudgeError: 一个后端都没配置 —— 判断需要模型，没有模型就判不了。
    """
    notes: list[str] = []
    want = (requested or settings.judge_backend or "auto").strip().lower()
    if want not in ("auto", "jev", "llm"):
        notes.append(f"未知后端 {want!r}，按 auto 处理")
        want = "auto"
    if want == "jev":
        if not settings.jev_configured:
            notes.append("指定了 jev 后端，但没有 JEV_API_KEY —— 会先尝试，失败后降级")
        return "jev", notes
    if want == "llm":
        if not settings.llm_configured:
            notes.append("指定了 llm 后端，但没有 LLM key —— 会先尝试，失败后降级")
        return "llm", notes
    if settings.jev_configured:
        return "jev", notes
    if settings.llm_configured:
        notes.append("没有配置 JEV，改用 LLM 后端（同一套题型）")
        return "llm", notes
    raise JudgeError(
        "没有可用的判断后端：JEV 与 LLM 都没配置。判断需要模型 —— "
        "请配置 JEV_API_KEY 或 LLM key 后重试"
    )


# ---------------------------------------------------------------------------
# 选原则
# ---------------------------------------------------------------------------
def select_principles(
    store: PrincipleStore,
    principle_ids: Sequence[str] | None = None,
    *,
    settings: Settings | None = None,
) -> tuple[list[Principle], list[str]]:
    """挑出这次判断要用的原则。

    显式 id 优先（找不到的如实报告，不静默跳过）；否则按「严重度降序 + id」
    取前 `max_principles` 条 —— 红线优先被看到。

    Returns:
        `(principles, notes)`。
    """
    settings = settings or load_settings()
    notes: list[str] = []
    if principle_ids:
        picked: list[Principle] = []
        missing: list[str] = []
        for pid in principle_ids:
            found = store.get(pid)
            if found is None or found.archived:
                missing.append(pid)
            else:
                picked.append(found)
        if missing:
            notes.append(f"这些原则不存在或已归档，已跳过：{', '.join(missing)}")
        return picked, notes

    candidates = sorted(store.all(), key=lambda p: (-p.severity, p.id))
    if not candidates:
        return [], ["原则库是空的 —— 先用 add/seed 建几条可判定的原则"]
    limit = max(1, settings.max_principles)
    if len(candidates) > limit:
        notes.append(
            f"原则共 {len(candidates)} 条，本次只判最严重的 {limit} 条"
            f"（用 --principle 指定其他条目，或调 PRINCIPLE_MAX_PRINCIPLES_PER_JUDGE）"
        )
        candidates = candidates[:limit]
    return candidates, notes


# ---------------------------------------------------------------------------
# 合并
# ---------------------------------------------------------------------------
def merge(
    principle: Principle,
    model_verdict: Verdict | None,
    *,
    backend: str = "jev",
) -> Verdict:
    """模型结论 → 一条最终判断。

    **判断权完全在模型。** 程序只做两件事：检测模型自相矛盾、在模型没给答案时
    如实标存疑。它不替模型下结论，也不拿字面匹配去覆盖模型。

    合并规则（写死在这里，测试逐条钉住）：

    1. **模型有结论就采纳**，只做矛盾检测。
    2. **模型自相矛盾一律降为存疑**：选了某个选项、但该选项的概率低于 0.6，
       说明模型自己没把握。`violation` 与 `pass` 对称处理。
    3. **模型没给答案**（后端失败 / 降级）→ 存疑，并写明「语义判断缺席」。

    Args:
        principle: 原则（取标题与严重度）。
        model_verdict: 模型对该原则的结论；`None` 表示没给出答案。
        backend: 本次实际使用的后端（`jev` / `llm`）。

    Returns:
        合并后的 `Verdict`。
    """
    severity = int(principle.severity)

    # ---- 模型没给答案：语义判断缺席 → 存疑
    if model_verdict is None:
        return Verdict(
            principle_id=principle.id,
            title=principle.title,
            violated=None,
            confidence=0.0,
            severity=severity,
            reasoning=f"语义判断缺席（{backend} 后端没给出答案）",
            source="model",
        )

    # ---- 模型自相矛盾：所选选项概率低于 0.6 → 存疑
    if model_verdict.violated is not None and model_verdict.probability < 0.6:
        chosen = "违反" if model_verdict.violated else "未违反"
        return Verdict(
            principle_id=principle.id,
            title=principle.title,
            violated=None,
            probability=model_verdict.probability,
            confidence=model_verdict.confidence,
            severity=severity,
            evidence=list(model_verdict.evidence),
            reasoning=(
                f"{model_verdict.reasoning}；但模型自报的所选选项概率只有 "
                f"{model_verdict.probability:.2f}，与「{chosen}」的结论不一致，按存疑处理"
            ),
            source="model",
        )

    # ---- 模型有明确结论：采纳
    return Verdict(
        principle_id=principle.id,
        title=principle.title,
        violated=model_verdict.violated,
        probability=model_verdict.probability,
        confidence=model_verdict.confidence,
        severity=severity,
        evidence=list(model_verdict.evidence),
        reasoning=model_verdict.reasoning,
        source="model",
    )


def _resolve_overall(
    verdicts: list[Verdict],
    answers: dict[str, jev.Answer],
) -> tuple[str, str]:
    """定整体结论：**以模型的全局答案为准**，没有就由逐条推导。

    模型给了 `overall` 就用它；逐条结论与它不一致时写进 notes 供复核，但不改判。

    Returns:
        `(overall, note)`；`note` 为空表示没有需要说明的事。
    """
    model_overall = ""
    overall_answer = answers.get(jev.OVERALL_KEY)
    if overall_answer is not None and isinstance(overall_answer.value, str):
        candidate = overall_answer.value.strip().lower()
        if candidate in ("pass", "violation", "uncertain"):
            model_overall = candidate

    # 逐条推导（作为对照，也作为 JEV 没给全局答案时的兜底）
    violations = [v for v in verdicts if v.violated is True]
    unknown = [v for v in verdicts if v.violated is None]
    if violations:
        derived = "violation"
    elif unknown:
        derived = "uncertain"
    else:
        derived = "pass"

    if not model_overall:
        return derived, "模型没有给出全局结论，整体结论由逐条推导"

    if model_overall != derived:
        return model_overall, (
            f"模型的全局结论是 {model_overall}，与逐条推导 {derived} 不一致 —— "
            f"以模型为准（逐条结论见下），建议复核"
        )
    return model_overall, ""


def _verdict_from_answer(principle: Principle, answer: jev.Answer) -> Verdict:
    """把一个 `choice` 答案翻译成 `Verdict`（三态 + 概率 + 置信度）。

    三态由模型直接给出，不用阈值事后切 —— 这是 `noul` 做不到的：
    二态题型没有「不确定」这个出口，模型想说的「证据不够」无处表达。
    """
    if answer.type != "choice":
        return Verdict(
            principle_id=principle.id,
            title=principle.title,
            violated=None,
            severity=int(principle.severity),
            reasoning=f"题型不符（{answer.type}），无法判定",
            source="model",
        )
    choice = str(answer.value or "").strip().lower()
    if choice not in ("pass", "violation", "uncertain"):
        return Verdict(
            principle_id=principle.id,
            title=principle.title,
            violated=None,
            severity=int(principle.severity),
            reasoning=f"模型返回了无法识别的选项 {answer.value!r}，按存疑处理",
            source="model",
        )
    violated: bool | None = {"pass": False, "violation": True, "uncertain": None}[choice]
    # 概率取「所选选项」的概率；模型没给 probabilities 时退回**确定值**。
    #
    # 注意这里是「所选选项的概率」，不是「违反的概率」：`pass` 必须是 1.0。
    # 以前写成 `pass: 0.0`（按违反概率理解），结果 LLM 不给 probabilities 时，
    # 一个明确的 pass 拿到 probability=0.0，`merge` 的「< 0.6 即自相矛盾」检查
    # 就把它降成存疑 —— 把「明确通过」误判成「不知道」（OCR 复审发现）。
    probability = answer.probability
    if not answer.probabilities:
        probability = {"pass": 1.0, "violation": 1.0, "uncertain": 0.5}[choice]
    # 置信度**不能**用 `max(p, 1-p)` 兜底：那会让「选了 pass 但 violation 概率 0.9」
    # 显示成 0.9 置信度（复审 4.2）。模型没给 confidence 时，就用所选选项的概率本身 ——
    # 它低，置信度就该低；矛盾由 `merge` 统一降为存疑。
    confidence = answer.confidence or probability
    reason = getattr(answer, "reason", "") or ""
    label = {"pass": "未违反", "violation": "违反", "uncertain": "存疑"}[choice]
    reasoning = reason or (
        f"模型判定「{label}」（{choice} 概率 {probability:.2f}，置信度 {confidence:.2f}）"
    )
    return Verdict(
        principle_id=principle.id,
        title=principle.title,
        violated=violated,
        probability=probability,
        confidence=confidence,
        severity=int(principle.severity),
        evidence=[],
        reasoning=reasoning,
        source="model",
    )


# ---------------------------------------------------------------------------
# 编排
# ---------------------------------------------------------------------------
class Judge:
    """一次判断的执行者。无状态（原则库与配置都可注入），可反复复用。"""

    def __init__(self, settings: Settings | None = None, store: PrincipleStore | None = None) -> None:
        self.settings = settings or load_settings()
        self.store = store or PrincipleStore(self.settings.home_path)

    def judge(
        self,
        material: str,
        *,
        principle_ids: Sequence[str] | None = None,
        principles: Sequence[Principle] | None = None,
        subject: str = "",
        backend: str = "",
        save: bool = True,
        transport: Any = None,
    ) -> Judgement:
        """判断一段材料是否违反原则。

        Args:
            material: 被判断的材料（原文；会被截断到 `max_material_chars`）。
            principle_ids: 只用这些原则（空 = 按严重度自动挑）。
            principles: 直接给原则对象（测试/高级用法，优先于 `principle_ids`）。
            subject: 材料来源（文件名 / URL / 「用户粘贴」），写进案例。
            backend: 临时覆盖后端（jev/llm/auto）。
            save: 是否把案例写进 `cases.jsonl`。
            transport: 传输层钩子（测试离线用）。

        Returns:
            `Judgement`。

        Raises:
            JudgeError: 材料为空，或没有可用的判断后端。
        """
        text = (material or "").strip()
        if not text:
            raise JudgeError("材料为空 —— 没有可判断的内容。")
        notes: list[str] = []
        limit = max(1, self.settings.max_material_chars)
        if len(text) > limit:
            notes.append(f"材料 {len(text)} 字符，超出上限，只判前 {limit} 字符")
            text = text[:limit]

        if principles is None:
            picked, sel_notes = select_principles(self.store, principle_ids, settings=self.settings)
            notes.extend(sel_notes)
        else:
            picked = list(principles)

        # 没有原则可判 → 这是一个结论（uncertain + 「先去加原则」），不是错误。
        # 必须在选后端**之前**返回：否则没配 key 时会被「没有可用的判断后端」盖掉，
        # 用户看不到真正该做的事（加原则）。
        if not picked:
            judgement = Judgement(
                case_id=new_case_id(),
                subject=subject or text[:60],
                material_chars=len(text),
                overall="uncertain",
                score=0.0,
                severity=0,
                verdicts=[],
                backend="",
                notes=notes,
                created_at=now_iso(),
            )
            if save:
                self.store.append_case(judgement)
            return judgement

        used_backend, backend_notes = choose_backend(self.settings, backend)
        notes.extend(backend_notes)

        # ---- 模型层：把原则翻译成题型，交给模型给出结论
        answers: dict[str, jev.Answer] = {}
        model_name = ""
        usage: dict[str, Any] = {}
        questions, key_map = jev.build_questions(picked)
        try:
            if used_backend == "jev":
                response = jev.ask(text, questions, settings=self.settings, transport=transport)
            else:
                response = llm.ask(
                    text,
                    questions,
                    settings=self.settings,
                    transport=transport,
                    key_map=key_map,
                )
            answers = response.answers
            model_name = response.model
            usage = response.usage
        except (jev.JevError, llm.LlmError) as exc:
            notes.append(f"{used_backend} 后端失败：{exc.message}")
            if used_backend == "jev" and self.settings.llm_configured:
                try:
                    response = llm.ask(
                        text,
                        questions,
                        settings=self.settings,
                        transport=transport,
                        key_map=key_map,
                    )
                    answers = response.answers
                    model_name = response.model
                    usage = response.usage
                    notes.append("已降级到 llm 后端（同一套题型）")
                    used_backend = "llm"
                except llm.LlmError as exc2:
                    notes.append(
                        f"llm 后端也失败：{exc2.message}；语义判断缺席，逐条结论一律存疑"
                    )
            else:
                notes.append("语义判断缺席，逐条结论一律存疑")

        # ---- 逐条裁决
        model_verdicts: dict[str, Verdict] = {}
        for key, answer in answers.items():
            pid = key_map.get(key)
            if not pid:
                continue
            principle = next((p for p in picked if p.id == pid), None)
            if principle is None:
                continue
            model_verdicts[pid] = _verdict_from_answer(principle, answer)

        verdicts = [
            merge(p, model_verdicts.get(p.id), backend=used_backend)
            for p in picked
        ]

        # ---- 整体结论：以模型的全局答案为准；没有就由逐条结论推导
        overall, overall_note = _resolve_overall(verdicts, answers)
        if overall_note:
            notes.append(overall_note)

        score = 0.0
        severity = 0
        violations = [v for v in verdicts if v.violated is True]
        unknown = [v for v in verdicts if v.violated is None]
        if violations:
            score = max((v.probability or v.confidence) * v.severity / 5.0 for v in violations)
            severity = max(v.severity for v in violations)
        elif unknown:
            score = max((v.probability or v.confidence or 0.5) * v.severity / 5.0 for v in unknown)

        judgement = Judgement(
            case_id=new_case_id(),
            subject=(subject or text[:60]).strip(),
            material_chars=len(text),
            overall=overall,
            score=min(1.0, max(0.0, score)),
            severity=severity,
            verdicts=verdicts,
            backend=used_backend,
            model=model_name,
            usage=usage,
            notes=notes,
            created_at=now_iso(),
        )
        if save:
            self.store.append_case(judgement)
        return judgement


# ---------------------------------------------------------------------------
# 展示
# ---------------------------------------------------------------------------
def format_judgement(judgement: Judgement) -> str:
    """把一次判断排成人能读的报告（CLI 与 agent 工具共用）。"""
    marks = {"violation": "违反", "pass": "通过", "uncertain": "存疑"}
    lines = [
        f"判定 {judgement.case_id}  后端 {judgement.backend}"
        + (f"（{judgement.model}）" if judgement.model else ""),
        f"材料 {judgement.material_chars} 字符  |  原则 {len(judgement.verdicts)} 条  |  "
        f"结论：{marks.get(judgement.overall, judgement.overall)}",
        f"材料来源：{judgement.subject}",
    ]
    if judgement.violations:
        severity_label = SEVERITY_LABELS.get(judgement.severity, str(judgement.severity))
        lines.append(f"最严重：{severity_label}（严重度 {judgement.severity}），违规程度 {judgement.score:.2f}")
    lines.append("-" * 60)
    for verdict in sorted(judgement.verdicts, key=lambda v: (-v.severity, v.principle_id)):
        lines.append(
            f"[{verdict.label}] {verdict.principle_id} {verdict.title}"
            f"  严重度 {verdict.severity}  置信度 {verdict.confidence:.2f}"
            f"  来源 {verdict.source}"
        )
        if verdict.reasoning:
            lines.append(f"    说明：{verdict.reasoning}")
        for evidence in verdict.evidence[:3]:
            lines.append(f"    证据：{evidence}")
    if judgement.notes:
        lines.append("-" * 60)
        for note in judgement.notes:
            lines.append(f"备注：{note}")
    return "\n".join(lines)
