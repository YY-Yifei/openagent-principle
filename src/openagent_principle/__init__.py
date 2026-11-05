"""openagent-principle —— 致敬《原则》的规则化 agent。

公开接口分三层：
* **判断层**（只用 stdlib）：`Judge` / `PrincipleStore` / `jev` / `rules` —— 不进 agent 也能判；
* **结构化 API**（只用 stdlib）：`api` —— 返回对象、抛异常，给程序 / 其他 agent 用；
* **agent 层**（需要 agentscope）：`openagent_principle.agent.build_agent` —— 延迟导入，
  不装 agentscope 也能用前两层。
"""

from __future__ import annotations

__version__ = "0.5.0"

from . import api
from .config import Settings, load_settings
from .judge import Judge, choose_backend, format_judgement
from .models import Judgement, Principle, Rule, Verdict
from .store import PrincipleStore

__all__ = [
    "__version__",
    "api",
    "Settings",
    "load_settings",
    "Judge",
    "choose_backend",
    "format_judgement",
    "Judgement",
    "Principle",
    "Rule",
    "Verdict",
    "PrincipleStore",
]
