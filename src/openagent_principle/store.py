"""存储：原则、案例、反馈 —— 三份文件，全在 `$PRINCIPLE_HOME` 下。

为什么不是数据库：原则库是**个人资产**，几十到几百条量级。一个人能 `cat`、
能 `git diff`、能手工改一份 JSON，比多一个要备份/迁移的 sqlite 文件重要得多。
代价是并发写不安全，所以写盘一律**原子替换**（临时文件 + `os.replace`），
并且每次写之前重读磁盘 —— 外部编辑器改过文件也不会被内存里那份盖掉。

三份文件的职责：
* `principles.json` —— 可变状态（增删改查、版本号），原子重写；
* `cases.jsonl`     —— 只追加的判断案例，永不修改（审计与复盘的原始记录）；
* `feedback.jsonl`  —— 只追加的人工反馈，用来发现「哪条原则老是判错」。
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import tempfile
from dataclasses import asdict
from pathlib import Path
from typing import Any, Iterable

from .config import load_settings
from .models import Judgement, Principle, now_iso

SCHEMA_VERSION = 1

#: 跨进程写锁的文件名。`os.replace` 只能保证「文件不会写坏」，保证不了
#: 「两个进程同时读改写不会丢更新」—— 复审 5.3 的正是后者。
LOCK_NAME = ".principles.lock"

#: 建库时顺手写进库目录的 `.gitignore`。理由：
#: * `principles.json` 是**资产**，该进仓库 —— 原则的版本历史就是 `git log`；
#: * `cases.jsonl` / `feedback.jsonl` 含材料原文，可能是别人的邮件、聊天记录，
#:   默认留在本地，别让一次 `git add -A` 把别人说的话推进远端。
GITIGNORE_TEXT = """\
# openagent-principle 原则库（程序自动生成，可手改）
# principles.json 值得进仓库；下面这些含材料原文，默认只留本地。
cases.jsonl
feedback.jsonl
*.tmp
"""

#: 原则 id 形如 `P-001`；自增时按数字段取最大值 + 1。
_ID_RE = re.compile(r"^P-(\d{3,})$")


class StoreError(RuntimeError):
    """原则库读写失败（目录不可写 / JSON 坏了）。消息必须是给人看的。"""


def _atomic_write(path: Path, text: str) -> None:
    """写临时文件再 `os.replace` —— 断电/崩溃也不会留下半个 JSON。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_name, path)
    except Exception as exc:  # noqa: BLE001
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise StoreError(f"写入 {path} 失败：{exc}") from exc


def _exists(path: Path) -> bool:
    """`exists()` 的容错版：权限不足 / 路径过长都当作「没有」。"""
    try:
        return path.exists()
    except OSError:
        return False


@contextlib.contextmanager
def _write_lock(home: Path):
    """跨进程独占锁：保护「读 → 改 → 写」整段事务。

    为什么需要它：`_atomic_write` 用 `os.replace` 保证文件不会写坏，但两个进程
    同时「读旧内容 → 各自改 → 各自写」时，后写的会覆盖先写的（丢更新）。
    自动 ID 分配尤其危险：两个进程可能拿到同一个 `P-00N`。

    实现：Windows 用 `msvcrt.locking`，POSIX 用 `fcntl.flock`。锁文件常驻
    （不删），避免「删锁文件」本身的竞态。拿不到锁就等，不超时 —— 写操作很短。
    """
    home.mkdir(parents=True, exist_ok=True)
    lock_path = home / LOCK_NAME
    fh = open(lock_path, "a+b")
    try:
        if os.name == "nt":
            import msvcrt

            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_LOCK, 1)
        else:
            import fcntl

            fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            if os.name == "nt":
                import msvcrt

                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
    finally:
        fh.close()


def _read_json(path: Path, default: Any) -> Any:
    """读 JSON；文件不存在返回默认值，坏了就报人话（不静默重置用户数据）。"""
    if not path.exists():
        return default
    try:
        with path.open("r", encoding="utf-8") as fh:
            return json.load(fh)
    except json.JSONDecodeError as exc:
        raise StoreError(
            f"{path} 不是合法 JSON（第 {exc.lineno} 行）：先修好或挪走它，别让程序把数据覆盖掉"
        ) from exc


def _iter_jsonl(path: Path) -> Iterable[dict[str, Any]]:
    """逐行读 JSONL；坏行跳过并**保留**（不重写文件，避免误删用户数据）。"""
    if not path.exists():
        return
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(item, dict):
                yield item


class PrincipleStore:
    """一个目录 = 一个原则库。所有读写都走这里，`tools` 与 `cli` 不碰文件。"""

    def __init__(self, home: Path | str | None = None) -> None:
        self.home = Path(home).expanduser() if home else load_settings().home_path

    # ------------------------------------------------------------------ 路径
    @property
    def principles_path(self) -> Path:
        return self.home / "principles.json"

    @property
    def cases_path(self) -> Path:
        return self.home / "cases.jsonl"

    @property
    def feedback_path(self) -> Path:
        return self.home / "feedback.jsonl"

    def ensure(self) -> None:
        """建目录（幂等）。写之前一律先调它，读的时候调也无害。"""
        try:
            self.home.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise StoreError(f"无法创建原则库目录 {self.home}：{exc}") from exc
        self._ensure_gitignore()

    def _ensure_gitignore(self) -> None:
        """首次建库时放一份 `.gitignore`；已有则**绝不覆盖**（用户可能改过）。"""
        path = self.home / ".gitignore"
        if _exists(path):
            return
        try:
            path.write_text(GITIGNORE_TEXT, encoding="utf-8", newline="\n")
        except OSError:
            # 只是「锦上添花」：比如库在只读盘上，写不了不该挡住建库本身。
            pass

    # -------------------------------------------------------------- 原则读写
    def load(self) -> list[Principle]:
        """读全部原则（含归档）；文件不存在就是空库，不是错误。"""
        data = _read_json(self.principles_path, {"schema": SCHEMA_VERSION, "principles": []})
        items = data.get("principles") if isinstance(data, dict) else data
        result: list[Principle] = []
        for item in items or []:
            if isinstance(item, dict):
                result.append(Principle.from_dict(item))
        return result

    def _write_all(self, principles: list[Principle]) -> None:
        payload = {
            "schema": SCHEMA_VERSION,
            "updated_at": now_iso(),
            "principles": [p.to_dict() for p in principles],
        }
        _atomic_write(self.principles_path, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")

    def all(self, *, include_archived: bool = False) -> list[Principle]:
        items = self.load()
        return items if include_archived else [p for p in items if not p.archived]

    def get(self, principle_id: str) -> Principle | None:
        """按 id 取一条（大小写不敏感，`p-001` 也认）。"""
        wanted = (principle_id or "").strip().lower()
        for principle in self.load():
            if principle.id.lower() == wanted:
                return principle
        return None

    def next_id(self) -> str:
        """下一个可用 id：`P-001`、`P-002`……（按最大数字 + 1，不按条数）。"""
        numbers = [int(m.group(1)) for p in self.load() if (m := _ID_RE.match(p.id))]
        return f"P-{max(numbers, default=0) + 1:03d}"

    def save(self, principle: Principle) -> Principle:
        """新增或更新一条原则（按 id 判定），返回落盘后的对象。

        更新纪律：`version` 自增、`created_at` 保留、`updated_at` 刷新。
        传进来的 `id` 为空则自动分配 —— 调用方不必自己管编号。

        Args:
            principle: 待保存的原则。

        Returns:
            保存后的原则（含分配好的 id 与时间戳）。

        Raises:
            StoreError: 目录不可写。
            ModelError: 字段不合法。
        """
        # 整段「分配 id → 读 → 改 → 写」必须在同一把跨进程锁里（复审 5.3）：
        # 否则两个进程可能拿到同一个 P-00N，或后写的覆盖先写的。
        with _write_lock(self.home):
            if not principle.id.strip():
                principle.id = self.next_id()
            principle.validate()
            stamp = now_iso()
            items = self.load()
            existing = next(
                (i for i, p in enumerate(items) if p.id.lower() == principle.id.lower()), None
            )
            if existing is None:
                principle.created_at = principle.created_at or stamp
                principle.updated_at = stamp
                items.append(principle)
            else:
                old = items[existing]
                principle.created_at = old.created_at or stamp
                principle.version = max(old.version, principle.version) + 1
                principle.updated_at = stamp
                items[existing] = principle
            self._write_all(items)
        return principle

    def delete(self, principle_id: str, *, hard: bool = False) -> bool:
        """删除（默认**归档**，`hard=True` 才真删）。

        为什么默认归档：原则的演化史本身有价值 ——「我们曾经这么判，后来改了主意」
        是可复盘的信息；真删只留给「写错了、从没生效过」的那种。
        """
        with _write_lock(self.home):
            items = self.load()
            for idx, principle in enumerate(items):
                if principle.id.lower() != (principle_id or "").strip().lower():
                    continue
                if hard:
                    del items[idx]
                else:
                    principle.archived = True
                    principle.version += 1
                    principle.updated_at = now_iso()
                    items[idx] = principle
                self._write_all(items)
                return True
        return False

    # ------------------------------------------------------------------ 检索
    def search(
        self,
        query: str = "",
        *,
        tag: str = "",
        scope: str = "",
        include_archived: bool = False,
    ) -> list[Principle]:
        """按关键词 / 标签 / 作用域筛选。空条件 = 全部。

        `query` 在 title / statement / rationale / criteria / tags / examples 里
        做**不区分大小写**的子串匹配（中文无需分词，够用且可预期）。
        """
        needle = (query or "").strip().lower()
        wanted_tag = (tag or "").strip().lower()
        wanted_scope = (scope or "").strip().lower()
        result: list[Principle] = []
        for principle in self.load():
            if principle.archived and not include_archived:
                continue
            if wanted_tag and wanted_tag not in [t.lower() for t in principle.tags]:
                continue
            if wanted_scope and wanted_scope not in principle.scope.lower():
                continue
            if needle:
                haystack = " ".join(
                    [
                        principle.title,
                        principle.statement,
                        principle.rationale,
                        principle.criteria,
                        " ".join(principle.tags),
                        " ".join(principle.examples),
                    ]
                ).lower()
                if needle not in haystack:
                    continue
            result.append(principle)
        return result

    def stats(self) -> dict[str, Any]:
        """库的读数：条数 / 各严重度分布 / 案例数。CLI 的 banner 与 `stats` 用它。"""
        principles = self.load()
        active = [p for p in principles if not p.archived]
        by_severity: dict[int, int] = {}
        for principle in active:
            by_severity[principle.severity] = by_severity.get(principle.severity, 0) + 1
        cases = sum(1 for _ in _iter_jsonl(self.cases_path))
        feedback = sum(1 for _ in _iter_jsonl(self.feedback_path))
        return {
            "home": str(self.home),
            "principles": len(principles),
            "active": len(active),
            "archived": len(principles) - len(active),
            "by_severity": dict(sorted(by_severity.items())),
            "cases": cases,
            "feedback": feedback,
        }

    # ------------------------------------------------------------------ 案例
    def append_case(self, judgement: Judgement) -> None:
        """追加一条判断案例（只追加，永不修改）。"""
        self.ensure()
        line = json.dumps(judgement.to_dict(), ensure_ascii=False)
        try:
            with self.cases_path.open("a", encoding="utf-8", newline="\n") as fh:
                fh.write(line + "\n")
        except OSError as exc:
            raise StoreError(f"写入案例失败：{exc}") from exc

    def list_cases(self, *, principle_id: str = "", limit: int = 20) -> list[Judgement]:
        """最近的案例（新的在前）。可按原则 id 过滤（只看某条原则的历史判断）。"""
        cases = [Judgement.from_dict(d) for d in _iter_jsonl(self.cases_path)]
        if principle_id:
            wanted = principle_id.strip().lower()
            cases = [
                c for c in cases if any(v.principle_id.lower() == wanted for v in c.verdicts)
            ]
        cases.sort(key=lambda c: c.created_at, reverse=True)
        return cases[: max(1, limit)]

    def get_case(self, case_id: str) -> Judgement | None:
        """按案例号取一条。"""
        wanted = (case_id or "").strip().lower()
        for data in _iter_jsonl(self.cases_path):
            if str(data.get("case_id") or "").lower() == wanted:
                return Judgement.from_dict(data)
        return None

    # ------------------------------------------------------------------ 反馈
    def append_feedback(
        self,
        case_id: str,
        *,
        agree: bool,
        principle_id: str = "",
        note: str = "",
    ) -> dict[str, Any]:
        """记录一次人工反馈：同意标「正确」，不同意标「误判」。

        这是「痛苦 + 反思 = 进步」的落点：反馈不改写历史案例，但可以统计出
        「哪条原则最常被判错」，从而提醒人去修订它的 `criteria` / `rules`。

        Args:
            case_id: 案例号。
            agree: 是否同意这次判断。
            principle_id: 只对某一条原则的结论表态时填它（空 = 针对整体结论）。
            note: 人话备注（为什么不同意、正确的判断是什么）。

        Returns:
            写入的反馈记录。
        """
        self.ensure()
        record = {
            "case_id": (case_id or "").strip(),
            "principle_id": (principle_id or "").strip(),
            "agree": bool(agree),
            "note": (note or "").strip(),
            "created_at": now_iso(),
        }
        try:
            with self.feedback_path.open("a", encoding="utf-8", newline="\n") as fh:
                fh.write(json.dumps(record, ensure_ascii=False) + "\n")
        except OSError as exc:
            raise StoreError(f"写入反馈失败：{exc}") from exc
        return record

    def list_feedback(self, *, principle_id: str = "", limit: int = 50) -> list[dict[str, Any]]:
        """最近的反馈（新的在前）。"""
        items = list(_iter_jsonl(self.feedback_path))
        if principle_id:
            wanted = principle_id.strip().lower()
            items = [i for i in items if str(i.get("principle_id") or "").lower() == wanted]
        items.sort(key=lambda i: str(i.get("created_at") or ""), reverse=True)
        return items[: max(1, limit)]

    def feedback_summary(self) -> list[dict[str, Any]]:
        """按原则聚合反馈：`{principle_id, total, disagree, rate}`，按误判率降序。

        「哪条原则最不准」比「最近发生了什么」更值得先看 —— 它指向该改的那条。
        """
        buckets: dict[str, dict[str, Any]] = {}
        for item in _iter_jsonl(self.feedback_path):
            pid = str(item.get("principle_id") or "") or "(整体)"
            bucket = buckets.setdefault(pid, {"principle_id": pid, "total": 0, "disagree": 0, "rate": 0.0})
            bucket["total"] += 1
            if not item.get("agree", True):
                bucket["disagree"] += 1
        rows = list(buckets.values())
        for row in rows:
            row["rate"] = round(row["disagree"] / row["total"], 3) if row["total"] else 0.0
        rows.sort(key=lambda r: (r["rate"], r["disagree"]), reverse=True)
        return rows


def seed_principles() -> list[dict[str, Any]]:
    """内置的 8 条种子原则（致敬《原则》，但每条都写成**可判定**的形态）。

    返还的是 `to_dict()` 形态 —— 直接 `Principle.from_dict` 就能落库，
    也可以被 `cli seed` 写进用户库当作起点。
    """
    from .models import Rule

    def rule(kind: str, value: str | list[str], note: str = "") -> Rule:
        r = Rule(kind=kind, value=value, note=note)
        r.validate()
        return r

    seeds = [
        Principle(
            id="P-001",
            title="极度求真：结论必须能追到证据",
            statement="任何重要结论都要给出可核对的依据；没有依据的判断必须标注为推断。",
            rationale="达利欧把「极度求真」放在原则体系的最底层：不真实的信息会让所有后续决策一起错。",
            scope="分析报告 / 决策建议 / 事实性回答",
            scope_keywords=["报告", "建议", "结论", "分析", "数据"],
            criteria="结论涉及事实时，必须能指出依据（来源、原文、数据）；只有观点没有依据时，须显式写「推断」。",
            examples=["反例：这个方案肯定能成。（无依据）", "正例：这个方案能成，因为 A 测试中转化率提升 12%（数据来源：…）。"],
            rules=[
                rule("must_not_contain_any", ["肯定能成", "绝对没问题", "百分之百"], "禁用无依据的绝对化断言"),
            ],
            severity=4,
            tags=["求真", "证据"],
            source="seed",
        ),
        Principle(
            id="P-002",
            title="可信度加权：不同意见按可信度加权，而不是按嗓门",
            statement="采纳意见时要考虑提出者的可信度（历史准确率、专业相关性），并说明权重依据。",
            rationale="达利欧的「可信度加权决策」：一人一票会稀释专家意见，只看权威又会压制新信息。",
            scope="决策讨论 / 评审意见 / 多人分歧",
            scope_keywords=["决策", "评审", "分歧", "意见", "讨论"],
            criteria="出现分歧时，应说明各方依据与可信度差异；不得只按职位或声量定结论。",
            examples=["反例：老板说行就行。", "正例：A 在该领域有 3 次准确预测记录，本次结论采纳其判断，权重更高。"],
            rules=[],
            severity=3,
            tags=["决策", "可信度"],
            source="seed",
        ),
        Principle(
            id="P-003",
            title="痛苦 + 反思 = 进步：错误必须留下改进项",
            statement="复盘错误时，除了解释原因，还必须给出一条可执行的改进措施与责任人。",
            rationale="达利欧认为痛苦是信号，反思是把它转成进步的唯一通道；只解释不改进等于白痛一次。",
            scope="复盘 / 事故报告 / 项目总结",
            scope_keywords=["复盘", "事故", "总结", "回顾", "延期", "故障"],
            criteria="提到问题或错误时，必须同时出现改进措施；仅有「下次注意」不算可执行措施。",
            examples=["反例：这次延期是因为排期太紧，下次注意。", "正例：延期原因…；改进：需求评审增加估算环节，责任人张三，下次迭代生效。"],
            rules=[
                rule("must_contain_any", ["改进", "措施", "行动项", "责任人"], "复盘类材料必须包含改进项"),
            ],
            severity=4,
            tags=["复盘", "进步"],
            source="seed",
        ),
        Principle(
            id="P-004",
            title="直面问题：不回避、不粉饰",
            statement="描述问题时必须直说事实与影响，不得用模糊措辞淡化风险。",
            rationale="「极度透明」要求把坏消息原样传上去；粉饰会让问题在更晚、更贵的时候爆发。",
            scope="风险汇报 / 项目周报 / 对外说明",
            scope_keywords=["风险", "周报", "汇报", "说明", "进度"],
            criteria="不得出现「基本没问题」「影响不大」这类无数据支撑的淡化措辞；风险要写清触发条件与后果。",
            examples=["反例：目前基本没问题，影响不大。", "正例：当前有 2 个高优缺陷未修复，若周五前不解决，将影响 30% 用户下单。"],
            rules=[
                rule("must_not_contain_any", ["基本没问题", "影响不大", "问题不大"], "禁用无数据支撑的淡化措辞"),
            ],
            severity=3,
            tags=["透明", "风险"],
            source="seed",
        ),
        Principle(
            id="P-005",
            title="目标先行：先写清目标与衡量方式，再谈方案",
            statement="任何方案类材料必须先声明目标与成功标准（可量化），再给做法。",
            rationale="达利欧的五步流程第一步就是「明确目标」；目标不清时，方案再精致也无法判断对错。",
            scope="方案 / 需求 / 计划",
            scope_keywords=["方案", "需求", "计划", "规划", "设计"],
            criteria="材料中应能找到目标与衡量指标；只有做法没有目标的，视为违反。",
            examples=["反例：我们打算重写这个模块。", "正例：目标是把 P95 延迟从 800ms 降到 200ms，方案是重写模块…"],
            rules=[
                rule("must_contain_any", ["目标", "指标", "成功标准", "衡量"], "方案类材料必须写明目标或指标"),
            ],
            severity=3,
            tags=["目标", "五步流程"],
            source="seed",
        ),
        Principle(
            id="P-006",
            title="不把情绪当事实",
            statement="描述他人动机或情绪时，必须与可观察的行为分开陈述。",
            rationale="把「他不在乎」当事实写进报告，会让后续讨论建立在猜测上 —— 这是求真的大敌。",
            scope="人事评价 / 冲突复盘 / 沟通记录",
            scope_keywords=["评价", "冲突", "沟通", "记录", "人事", "反馈"],
            criteria="不得直接断言他人主观动机（如「他就是不想做」）；应写可观察的行为与影响。",
            examples=["反例：他根本不重视这个项目。", "正例：他连续两次未回复评审请求，导致排期延后 3 天。"],
            rules=[
                rule("must_not_contain_any", ["根本不重视", "就是不想", "故意刁难"], "禁止把动机猜测写成事实"),
            ],
            severity=3,
            tags=["沟通", "求真"],
            source="seed",
        ),
        Principle(
            id="P-007",
            title="可逆决策要快，不可逆决策要慢",
            statement="先判断决策的可逆性：可逆的快速试错，不可逆的必须做足验证与备份。",
            rationale="达利欧强调「区分一类与二类决策」：把不可逆的事当小事办，代价往往不可挽回。",
            scope="技术选型 / 上线 / 合同 / 数据删除",
            scope_keywords=["选型", "上线", "合同", "删除", "迁移", "发布"],
            criteria="涉及不可逆操作（删数据、签合同、架构迁移）时，必须写明回滚方案或验证步骤。",
            examples=["反例：直接删库重建。", "正例：先全量备份并验证可恢复，再执行删除。"],
            rules=[
                rule("must_contain_any", ["回滚", "备份", "验证", "灰度"], "不可逆操作必须写明回滚/验证"),
            ],
            severity=5,
            tags=["决策", "风险"],
            source="seed",
        ),
        Principle(
            id="P-008",
            title="透明可追溯：留下决策记录",
            statement="重要决策要留下「当时为什么这么定、放弃了什么」的记录，供以后复盘。",
            rationale="没有记录就没有复盘；只记得结论的团队会反复踩同一个坑。",
            scope="重要决策 / 架构变更 / 人事决定",
            scope_keywords=["决策", "变更", "决定", "选型", "架构"],
            criteria="决策类材料应包含备选方案与放弃理由；只有结论的，视为记录不完整。",
            examples=["反例：决定用方案 A。", "正例：选 A 而非 B，因为 B 在 3 个月内无法满足合规要求；代价是 A 的迁移成本多 2 人月。"],
            rules=[
                rule("must_contain_any", ["备选", "放弃", "权衡", "代价"], "决策记录必须包含备选与权衡"),
            ],
            severity=2,
            tags=["透明", "记录"],
            source="seed",
        ),
    ]
    return [p.to_dict() for p in seeds]
