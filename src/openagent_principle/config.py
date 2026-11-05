"""配置：全部来自环境变量，缺什么就明确报什么。

一条贯穿全模块的纪律（沿用 openagent-ds-search 的做法）：**非法配置不该让程序起不来，
更不该让工具抛异常**。环境变量是人的手写输入 —— 多打个字母、留个空、写成负数都正常。
所以所有数值型读取都走 `env_int` / `env_float`：读不出合法的就退回默认，
并把「默认值是什么」写在调用处，而不是让 `int('abc')` 在第 47 层调用栈里炸开。

与 ds-search 的关键差别：本项目的**判断层**（`judge` / `rules` / `jev` / `llm`）只用
stdlib，不 import agentscope。所以这里的配置也要能支撑「没有 agentscope 也能判」——
`home`、`judge_backend`、`jev_*` 这些字段与 agentscope 无关。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

#: LLM（agent 与 llm 判断后端共用）的默认地址与真名。
#: 真名只有 `deepseek-flash`（快、便宜）与 `deepseek-v4-pro`（带推理）；
#: `deepseek-chat` / `deepseek-reasoner` 是会被服务端静默路由的别名，别写。
DEFAULT_BASE_URL = "https://api.deepseek.com"
DEFAULT_MODEL = "deepseek-flash"
DEFAULT_CONTEXT_SIZE = 1_048_576

#: JEV 端点。**没有默认地址** —— 网关是部署方自己的，写死一个只会把别人的
#: 内网地址带进仓库。不配 `JEV_BASE_URL` 时 `jev_configured` 为假，自动降级到 llm / rules。
DEFAULT_JEV_BASE_URL = ""
DEFAULT_JEV_PATH = "/systemone/v1/systemone"
DEFAULT_JEV_MODEL = "jev-1.13.0"
DEFAULT_JEV_TIMEOUT = 120.0

#: 原则库的目录名。默认落在**项目根**下，而不是用户目录：
#: ① 原则跟着项目走，就能 `git diff` 看见它怎么改的、随仓库一起被同事拉到；
#: ② 「维护原则」与「用原则判断」共享同一份数据，不必靠约定同步；
#: ③ 拿它做迭代开发时，库和代码在同一个目录里，删了仓库就是删干净了。
STORE_DIR_NAME = ".openagent-principle"

#: 兜底：找不到任何项目标记时就建在当前目录（等价于就地建库）。
DEFAULT_HOME = STORE_DIR_NAME

#: 认定「这里是一个项目根」的标记。`.git` 可能是目录也可能是文件（worktree），
#: 所以判断用 exists 而不是 is_dir。
ROOT_MARKERS = (
    ".git",
    "pyproject.toml",
    "setup.py",
    "setup.cfg",
    "package.json",
    "Cargo.toml",
    "go.mod",
)

#: 合法后端名。`auto` 不是「猜」，而是下面 `judge.choose_backend` 里那张固定的降级表。
JUDGE_BACKENDS = ("auto", "jev", "llm", "rules")

#: 单次判断最多带多少条原则：JEV 按**调用**计费，一次装够才划算；
#: 但装太多会让每条原则在 `state` 里被稀释，12 是经验分界点。
DEFAULT_MAX_PRINCIPLES = 12

#: agent 的能力边界（`agent.resolve_toolset` 会用同一组名字校验）：
#:   full     = 通用能力 + 原则能力（默认）
#:   readonly = 只读通用 + 原则能力（不能写、不能跑命令）
#:   domain   = 只有原则工具（老行为，给「只判断、不动磁盘」的场景）
#: 默认 full 是刻意的：一个不能读代码、不能跑测试、不能改文件的 agent，
#: 看起来安全，实际上没法独立干活 —— 安全应该来自「明确的开关」，而不是
#: 来自「它根本做不到」。
TOOLSETS = ("full", "readonly", "domain")
DEFAULT_TOOLSET = "full"


def env_str(name: str, default: str = "") -> str:
    """读一个字符串环境变量，去首尾空白；空串等同于没设。"""
    return (os.environ.get(name) or "").strip() or default


def env_int(name: str, default: int, *, minimum: int = 1) -> int:
    """读一个整数环境变量；缺失 / 非数字 / 小于 `minimum` 一律退回 `default`。

    全项目唯一的「环境变量 → int」入口。集中一处的好处是行为只可能错一次，
    且可以用一组参数化测试把它钉死（`abc` / `-1` / `0` / `" 7 "` / 空串）。

    Args:
        name: 环境变量名。
        default: 读不到合法值时的返回值。
        minimum: 可接受的最小值（默认 1；传 0 表示允许 0）。

    Returns:
        合法整数，或 `default`。
    """
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value >= minimum else default


def env_float(
    name: str,
    default: float,
    *,
    minimum: float = 0.0,
    maximum: float = 2.0,
) -> float:
    """读一个浮点环境变量；缺失 / 非法 / 越界一律退回 `default`。"""
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    return value if minimum <= value <= maximum else default


def env_bool(name: str, default: bool = True) -> bool:
    """读一个布尔环境变量：0/false/no/off 为假，1/true/yes/on 为真，其余退回默认。"""
    raw = (os.environ.get(name) or "").strip().lower()
    if not raw:
        return default
    if raw in ("0", "false", "no", "off"):
        return False
    if raw in ("1", "true", "yes", "on"):
        return True
    return default


def first_env(*names: str) -> str:
    """按顺序取第一个非空环境变量 —— key 的多级回落（本项目自己的 → 通用的）。"""
    for name in names:
        value = (os.environ.get(name) or "").strip()
        if value:
            return value
    return ""


# ---------------------------------------------------------------------------
# 库位置：默认项目级
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class HomeResolution:
    """原则库目录是怎么定下来的 —— 一个绝对路径不够，得说清「为什么是它」。

    同一台机器上，在不同目录跑 `principles` 会得到不同的库；把来源摊开
    （`principles home`），才能让人一眼看出「我看到的库是不是我以为的那个」。
    """

    path: Path
    #: env / store / project / cwd，含义见 `find_project_root`
    source: str
    #: 决定 `path` 的那个目录（source=env 时无意义，为 None）
    root: Path | None
    #: 这次发现的起点（通常就是 cwd）
    start: Path


def _resolve(start: Path | str | None) -> Path:
    """把起点规范成绝对路径；`resolve()` 失败（权限 / 盘符消失）就原样退回。"""
    path = Path(start or Path.cwd()).expanduser()
    try:
        return path.resolve()
    except OSError:  # noqa: BLE001 —— 发现过程不该因为一次 stat 失败而炸
        return path


def _exists(path: Path) -> bool:
    """`exists()` 的容错版：权限不足 / 路径过长都当作「没有」。"""
    try:
        return path.exists()
    except OSError:
        return False


def find_project_root(start: Path | str | None = None) -> tuple[Path, str]:
    """向上找项目根，返回 `(目录, 依据)`。优先级是刻意的：

    1. `store` —— **最近的已有原则库**。库一旦建在某目录，它之下任何子目录都必须
       落到同一个库。否则在 `src/` 里跑 `principles judge` 会看不见仓库根
       `principles list` 里那几条原则，「数据凭空消失」是最难排查的一类 bug。
    2. `project` —— 最近的根标记（`.git` / `pyproject.toml` / …）。第一次在某项目
       里用，库就建在项目根，而不是当前所在的那层子目录。
    3. `cwd` —— 都没有，就退回起点本身：在哪儿跑就在哪儿建库，行为可预期。

    Args:
        start: 起点目录（默认 cwd）。给文件路径会退到它的父目录。

    Returns:
        `(目录, 依据)`，依据取 `store` / `project` / `cwd`。
    """
    base = _resolve(start)
    if base.is_file():
        base = base.parent

    marker_hit: Path | None = None
    for directory in (base, *base.parents):
        if _exists(directory / STORE_DIR_NAME):
            return directory, "store"
        if marker_hit is None and any(_exists(directory / name) for name in ROOT_MARKERS):
            marker_hit = directory
    if marker_hit is not None:
        return marker_hit, "project"
    return base, "cwd"


def resolve_home(start: Path | str | None = None) -> HomeResolution:
    """定下原则库目录：`PRINCIPLE_HOME` > 项目根发现 > 当前目录。

    `PRINCIPLE_HOME` 仍然是最高优先 —— 测试与「临时拿另一个库试试」都靠它，
    项目级只是**默认值**，不是硬规矩。

    Args:
        start: 发现的起点目录（默认 cwd）。

    Returns:
        `HomeResolution`，含路径与来源。
    """
    base = _resolve(start)
    env_home = (os.environ.get("PRINCIPLE_HOME") or "").strip()
    if env_home:
        return HomeResolution(Path(env_home).expanduser(), "env", None, base)
    root, source = find_project_root(base)
    return HomeResolution(root / STORE_DIR_NAME, source, root, base)


@dataclass(frozen=True)
class Settings:
    """一次运行所需的全部配置。字段默认值就是「什么都没配」时的行为。"""

    # ---- LLM（agent / llm 后端）----
    api_key: str = ""
    base_url: str = DEFAULT_BASE_URL
    model: str = DEFAULT_MODEL
    context_size: int = DEFAULT_CONTEXT_SIZE
    temperature: float = 0.3
    max_tokens: int = 4096
    stream: bool = True
    max_iters: int = 200

    # ---- 判断后端 ----
    judge_backend: str = "auto"
    jev_base_url: str = DEFAULT_JEV_BASE_URL
    jev_api_key: str = ""
    jev_model: str = DEFAULT_JEV_MODEL
    jev_path: str = DEFAULT_JEV_PATH
    jev_timeout: float = DEFAULT_JEV_TIMEOUT

    # ---- 存储 ----
    #: 默认值必须**在实例化时**才求值：import 时算一次会把「当时的 cwd」固化下来，
    #: 之后 cd 到别的项目仍然写回旧库 —— 这正是项目级默认最容易踩的坑。
    home: Path = field(default_factory=lambda: resolve_home().path)

    # ---- agent 能力边界 ----
    #: full / readonly / domain，见 TOOLSETS。非法值在 agent 层退回 full。
    toolset: str = DEFAULT_TOOLSET
    #: shell 的工作目录，也是给模型看的「主目录」。默认当前目录。
    workspace: Path = field(default_factory=lambda: Path.cwd())
    #: 关掉执行：true→false 会让 full 自动降成 readonly（见 agent.resolve_toolset）。
    allow_shell: bool = True
    #: bypass（默认，本机自用不逐次确认）/ default / accept_edits / explore
    permission: str = "bypass"

    # ---- 预算（0 = 不限；默认不限，见 agent.ToolBudget）----
    max_principles: int = DEFAULT_MAX_PRINCIPLES
    max_material_chars: int = 12_000
    max_judge_calls: int = 0
    max_fetch_calls: int = 0
    max_read_chars: int = 8_000
    max_read_calls: int = 0

    # ------------------------------------------------------------------ 派生
    @property
    def jev_configured(self) -> bool:
        """JEV 是否具备被调用的条件（地址 + key 都有）。"""
        return bool(self.jev_base_url and self.jev_api_key)

    @property
    def llm_configured(self) -> bool:
        """LLM 是否具备被调用的条件。"""
        return bool(self.api_key and self.base_url and self.model)

    @property
    def workspace_path(self) -> Path:
        """工作目录（`Path` 形式，已展开 `~`）。shell 与相对路径都从它出发。"""
        return Path(self.workspace).expanduser()

    @property
    def home_path(self) -> Path:
        """原则库目录（`Path` 形式，已展开 `~`）。"""
        return Path(self.home).expanduser()


def _calls(overrides: dict, key: str, env: str, default: int) -> int:
    """读一个「次数上限」：**0 是合法值**（表示不限），所以不能写成 `overrides.get(key) or …`。"""
    if overrides.get(key) is not None:
        try:
            return max(0, int(overrides[key]))  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return default
    return env_int(env, default, minimum=0)


def load_settings(**overrides: object) -> Settings:
    """从环境变量装载配置，`overrides` 优先级最高（测试与 CLI 都走它）。

    环境变量：
        PRINCIPLE_HOME                     原则库目录，默认 <项目根>/.openagent-principle（见 `principles home`）
        PRINCIPLE_JUDGE_BACKEND            auto|jev|llm|rules，默认 auto
        PRINCIPLE_API_KEY                  LLM key（缺省回落 DEEPSEEK_API_KEY / OPENAI_API_KEY）
        PRINCIPLE_BASE_URL                 LLM 地址，默认 https://api.deepseek.com
        PRINCIPLE_MODEL                    LLM 模型名，默认 deepseek-flash
        PRINCIPLE_CONTEXT_SIZE             上下文窗口，默认 1048576
        PRINCIPLE_TEMPERATURE              采样温度，默认 0.3
        PRINCIPLE_MAX_TOKENS               单次生成长度，默认 4096
        PRINCIPLE_MAX_ITERS                agent 每轮 ReAct 步数，默认 200
        PRINCIPLE_TOOLSET                  full|readonly|domain，默认 full
        PRINCIPLE_WORKSPACE                shell 的工作目录，默认当前目录
        PRINCIPLE_ALLOW_SHELL              0 = 关掉执行与写入（full 降为 readonly）
        PRINCIPLE_PERMISSION               bypass|default|accept_edits|explore，默认 bypass
        JEV_BASE_URL / JEV_PATH / JEV_MODEL / JEV_API_KEY / JEV_TIMEOUT
        PRINCIPLE_MAX_PRINCIPLES_PER_JUDGE 单次判断带几条原则，默认 12
        PRINCIPLE_MAX_MATERIAL_CHARS       送给模型的材料上限，默认 12000
        PRINCIPLE_MAX_JUDGE_CALLS          每轮 judge 次数上限，默认 0 = 不限
        PRINCIPLE_MAX_FETCH_CALLS          每轮抓网页次数上限，默认 0 = 不限
        PRINCIPLE_MAX_READ_CALLS           每轮读文件次数上限，默认 0 = 不限
        PRINCIPLE_MAX_READ_CHARS           单次读文件字符上限，默认 8000
        OPENAGENT_STREAM                   0 关闭流式

    Args:
        **overrides: 直接覆盖任意字段（`home` 传 str 会被转成 `Path`）。

    Returns:
        完整的 `Settings`。
    """
    api_key = str(
        overrides.get("api_key")
        or first_env("PRINCIPLE_API_KEY", "DEEPSEEK_API_KEY", "OPENAI_API_KEY")
    )
    jev_api_key = str(
        overrides.get("jev_api_key")
        or first_env("JEV_API_KEY", "PRINCIPLE_API_KEY", "OPENAI_API_KEY", "DEEPSEEK_API_KEY")
    )
    backend = str(overrides.get("judge_backend") or env_str("PRINCIPLE_JUDGE_BACKEND", "auto")).lower()
    if backend not in JUDGE_BACKENDS:
        # 写错了不报错：退回 auto（它的降级表会把「没配」翻译成 rules）。
        backend = "auto"

    home = overrides.get("home")
    home_path = Path(str(home)).expanduser() if home else resolve_home().path

    toolset_raw = str(overrides.get("toolset") or env_str("PRINCIPLE_TOOLSET", DEFAULT_TOOLSET)).lower()
    # 非法值归一成 `readonly`（不是 `full`）：安全开关写错时，失败方向必须是
    # 「更严」。以前归一成 full，拼错一个字母就重新获得写文件和跑命令的能力（复审 4.4）。
    toolset = toolset_raw if toolset_raw in TOOLSETS else "readonly"

    workspace_raw = overrides.get("workspace") or env_str("PRINCIPLE_WORKSPACE", "")
    workspace = Path(str(workspace_raw)).expanduser() if workspace_raw else Path.cwd()

    allow_shell = (
        bool(overrides["allow_shell"])
        if "allow_shell" in overrides
        else env_bool("PRINCIPLE_ALLOW_SHELL", True)
    )

    stream_raw = (os.environ.get("OPENAGENT_STREAM") or "").strip().lower()

    settings = Settings(
        api_key=api_key,
        base_url=str(overrides.get("base_url") or env_str("PRINCIPLE_BASE_URL", DEFAULT_BASE_URL)).rstrip("/"),
        model=str(overrides.get("model") or env_str("PRINCIPLE_MODEL", DEFAULT_MODEL)),
        context_size=int(overrides.get("context_size") or env_int("PRINCIPLE_CONTEXT_SIZE", DEFAULT_CONTEXT_SIZE)),
        temperature=float(
            overrides.get("temperature")
            if overrides.get("temperature") is not None
            else env_float("PRINCIPLE_TEMPERATURE", 0.3)
        ),
        max_tokens=int(overrides.get("max_tokens") or env_int("PRINCIPLE_MAX_TOKENS", 4096)),
        stream=bool(overrides["stream"]) if "stream" in overrides else stream_raw not in ("0", "false", "no", "off"),
        max_iters=int(overrides.get("max_iters") or env_int("PRINCIPLE_MAX_ITERS", 200)),
        judge_backend=backend,
        jev_base_url=str(
            overrides.get("jev_base_url") or env_str("JEV_BASE_URL", DEFAULT_JEV_BASE_URL)
        ).rstrip("/"),
        jev_api_key=jev_api_key,
        jev_model=str(overrides.get("jev_model") or env_str("JEV_MODEL", DEFAULT_JEV_MODEL)),
        jev_path=str(overrides.get("jev_path") or env_str("JEV_PATH", DEFAULT_JEV_PATH)),
        jev_timeout=float(
            overrides.get("jev_timeout")
            if overrides.get("jev_timeout") is not None
            else env_float("JEV_TIMEOUT", DEFAULT_JEV_TIMEOUT, minimum=1.0, maximum=600.0)
        ),
        home=home_path,
        toolset=toolset,
        workspace=workspace,
        allow_shell=allow_shell,
        permission=str(overrides.get("permission") or env_str("PRINCIPLE_PERMISSION", "bypass")).lower(),
        max_principles=int(
            overrides.get("max_principles") or env_int("PRINCIPLE_MAX_PRINCIPLES_PER_JUDGE", DEFAULT_MAX_PRINCIPLES)
        ),
        max_material_chars=int(
            overrides.get("max_material_chars") or env_int("PRINCIPLE_MAX_MATERIAL_CHARS", 12_000)
        ),
        max_judge_calls=_calls(overrides, "max_judge_calls", "PRINCIPLE_MAX_JUDGE_CALLS", 0),
        max_fetch_calls=_calls(overrides, "max_fetch_calls", "PRINCIPLE_MAX_FETCH_CALLS", 0),
        max_read_chars=int(overrides.get("max_read_chars") or env_int("PRINCIPLE_MAX_READ_CHARS", 8_000)),
        max_read_calls=_calls(overrides, "max_read_calls", "PRINCIPLE_MAX_READ_CALLS", 0),
    )
    return settings
