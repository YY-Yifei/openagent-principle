# CHANGELOG

## 未发布

### 移除规则层（破坏性变更）

**判断完全交给模型。** 确定性规则引擎（短语表 / 正则 / 长度阈值）整个移除 ——
它和设计哲学冲突：不认识否定（「不用备份」含「备份」）、替模型做判断、
用字面命中冒充语义结论。

- **删除 `rules.py`**（`RuleHit` / `RuleReport` / `evaluate` / `rule_report` / `rule_verdict`）。
- **`models.py`**：删 `Rule`、`RULE_KINDS`、`parse_rules`、`MAX_REGEX_LEN`；
  `Principle` 删 `rules` 与 `scope_keywords` 字段、删 `applies_to()`。
- **`judge.merge` 重写**：签名从 `merge(principle, rule_report, model_verdict)` 改为
  `merge(principle, model_verdict)`。只保留两条规则 —— 模型有结论就采纳；
  模型自相矛盾（所选选项概率 < 0.6）降为存疑。
- **`choose_backend` 不再降级到 `rules`**：JEV 与 LLM 都没配时抛 `JudgeError`
  （「没有可用的判断后端」）。判断需要模型，不提供假装在判的降级。
- **`jev.build_questions` / `principle_instruction`** 去掉 `rule_reports` 参数，
  不再把规则报告写进 instructions。
- **CLI**：删 `--rule` 参数与 `--backend rules`；`add` / `update` 不再接受规则。
- **API / tools**：`save_principle` 去掉 `rules` 参数。
- **`config.JUDGE_BACKENDS`**：`("auto", "jev", "llm", "rules")` → `("auto", "jev", "llm")`。
- **种子原则**：8 条全部去掉 `rules` / `scope_keywords`，只保留 `criteria` / `examples`；
  `data/principles.seed.json` 已重新生成。
- **`agent.describe` / `cli check`**：没配后端时显示「不可用」，不再抛异常。
- **测试**：删 `test_rules.py`；`test_judge.py` 的合并用例重写为「模型结论 + 矛盾检测」；
  判断链路用例改用假 JEV transport。

### JEV 重试

- **200 但响应不可用也重试**：不是 JSON（网关吐 HTML 错误页）、没有 `answers`、
  `answers` 里一道题都解析不出来 —— 这三种以前直接抛 `JevError` 降级，现在按可重试处理。
  重打一次往往就好了，直接降级反而丢掉语义判断。
- **默认重试次数 2 → 3**（`jev.DEFAULT_RETRIES`），退避 `(1, 2)` → `(1, 2, 4)` 秒。
  最坏情况 4 次请求、多等 7 秒。
- 不可重试的仍是：401/403/404 等非 `RETRYABLE_STATUS` 状态码、缺 key、材料为空、题型不合法。
- 新增测试：非 JSON 重试后成功、空 answers / 解析不出答案重试后成功、重试耗尽（4 次请求）。

## 0.5.0 — 2026-11-05

**判断架构重构：最终语义判断统一由 JEV 裁决；CLI 支持单轮提示词；移除 `fetch_url`。**

### 判断架构（破坏性变更）

- **规则层不再判违规，只产出报告**（`rules.rule_report` → `RuleReport`）
  - 只做三件事：适用范围检查、确定性匹配、原文取证。
  - `RuleHit.passed=False` 的语义从「违反」改为「这条规则的字面条件没满足」。
  - 新增 `Principle.scope_keywords`：结构化的适用范围。材料不命中就不判 ——
    修掉「仅复盘」的原则对天气报告判「缺少改进」的误报。
- **规则结果作为结构化输入交给 JEV**（`RuleReport.as_prompt()`）
  - 写进每条原则的 instructions，明确告诉 JEV：「未通过 ≠ 违反，请自行判断」。
  - JEV 可以采纳，也可以基于语义推翻。
- **`judge.merge` 重写**：JEV 有结论就以 JEV 为准，规则命中只作为证据附上。
  - 规则与 JEV 冲突时（规则未通过但 JEV 判 pass，或反之），在 reasoning 里留痕，
    不改判。
  - 模型自相矛盾（所选选项概率 < 0.6）一律降为存疑，`violation` 与 `pass` 对称处理。
  - **JEV 没给答案时**：显式 `--backend rules` 才允许规则定论；其他情况标存疑并写明
    「语义判断缺席」。
- **整体结论以 JEV 的全局答案为准**（`judge._resolve_overall`）
  - 逐条与整体冲突时写进 `notes` 供复核，不改判。
  - JEV 没给全局答案时才由逐条推导，并注明原因。
- **`Judge` 只负责协议校验、矛盾检测、存疑处理、审计留痕**，不再自行改判或重算整体结论。

### CLI

- **新增单轮模式**：`principles "一段自然语言提示词"`
  - 首个参数不是已知子命令时，建 agent、执行一次、输出、退出，不进多轮交互。
  - `principles`（无参数）仍是交互模式；所有已有子命令行为不变。
  - 子命令表从 parser 动态读取（`_known_subcommands`），加子命令时自动同步。

### 移除

- **`fetch_url` 工具**：agent 有 `Bash` / `PowerShell`，`curl` / `Invoke-WebRequest`
  就是抓取能力。衍生能力不该占域工具位，也少一个 SSRF 面。
  - `DOMAIN_TOOLS` 从 11 个减到 10 个；`READ_ONLY_DOMAIN` 同步。
  - `PRINCIPLE_MAX_FETCH_CALLS` 保留为兼容字段（设了也不会有工具命中）。

### 修复（来自两轮复审）

- `judge --source` 参数失效（读错了 `args.source`，应为 `source_label`）。
- 纯规则后端硬编码 `probability=0.95`，导致 `score` 无依据。
- `add --id` 撞号时静默覆盖并清空字段 → 现在拒绝，改字段用 `update`，整条覆盖需 `--force`。
- 安全配置拼写错误回退 `full` → 改为回退 `readonly`（失败方向必须更严）。
- `readonly` / `domain` 仍装写工具 → 只装 `READ_ONLY_DOMAIN`。
- 程序化 `Settings` 不传给域工具 → 用 `contextvars` 绑定（`tools.bind_settings`）。
- 原则 ID 撞上保留字 `overall` / `severity` → 保留字先占位。
- 畸形 JEV 响应（`probabilities` 是列表）抛 `AttributeError` 绕过降级 → 类型校验。
- 存储并发丢更新 → 加跨进程写锁（`store._write_lock`）。
- ReDoS → 正则执行加超时（子进程隔离，超时 `terminate`）。
- `search_files` 的 `glob` 用 `Path.match` 只匹配当前层 → 改用 `fnmatch` 匹配文件名。

### OCR 复审（open-code-review v1.13.5）

对 `judge.py` / `cli.py` / `agent.py` / `tools.py` 跑了 `ocr scan`，共 18 条发现，
全部逐条验证后修复：

**judge.py（4 条）**
- `pass` 无 probabilities 时 fallback 写成 `0.0`，被 `merge` 的「< 0.6 即矛盾」
  检查误降为存疑 → 改为 `1.0`（`probability` 是「所选选项的概率」，不是违反概率）。
- 范围检查只在 `model_verdict is None` 分支做，模型对范围外原则给答案时会被采纳
  → 补上范围外短路，模型结论一律忽略。
- 降级备注写「存疑（规则层不裁决）」，实际代码让规则层定论 → 改文案。
- 模块 docstring 仍说「规则命中不可推翻」，与实现相反 → 重写。

**cli.py（5 条）**
- `seed` 去重区分大小写，用户已有 `p-001` 时会被 `P-001` 覆盖 → 统一小写比较。
- `_read_material` 把参数错误和 I/O 失败都抛 `OSError`，退出码都是 2
  → 拆出 `_MaterialArgError`（2）与 `OSError`（3）。
- `ModelError` 被当成执行失败（3），但它是参数错误 → 改为 2。
- 旧库损坏会拖垮 `home` → 包 try/except，只提示「读不了」。
- 帮助文本残留英文 `only` → 改「只」。

**agent.py（4 条）**
- **我引入的回归**：`read_only_only=toolset != "full"` 把 `domain` 也砍成 5 个
  只读工具，`judge_material` / `save_principle` 消失 → 改为 `== "readonly"`。
- `PRINCIPLE_PERMISSION` 拼错回退 `BYPASS`（最宽松）→ 改为 `DEFAULT`。
- `ToolBudget.take()` 的「检查 + 自增」无锁，并行调用会超限 → 加 `threading.Lock`。
- system prompt 无条件列出所有工具，只读/domain 模式下模型会反复撞不存在的工具
  → 加「先看实际装了哪些」的说明。

**tools.py（5 条）**
- **最严重**：`hard="false"` 是真值，会把「归档」变成「不可恢复的真删」
  → 新增 `_to_bool` 严格解析。
- `save_principle` 收到 `null` 时 `.strip()` 抛 `AttributeError` 逃逸
  → 新增 `_as_text` 归一 + catch-all。
- 多处 `int(...)` 在 guard 外，模型传非数字会抛异常 → 新增 `_to_int`。
- `search_files` 的 `context > 0` 时把上下文行算进命中数 → 单独计数。
- `_iter_files` 跟随软链目录，成环时无限遍历 → 跳过软链目录。

### 测试

- 244 passed。新增覆盖：规则与 JEV 冲突（双向）、逐条与整体冲突、后端降级
  （jev→llm、jev→rules）、单轮模式识别、`fetch_url` 已移除、ReDoS 超时、
  OCR 复审的 18 条修复（含 `hard="false"` 不真删、seed 大小写去重、
  退出码区分参数错误与执行失败、domain 模式工具数、`context` 命中计数）。

## 0.4.0 — 2026-11-05

**判断题型从二态改三态；补上「被其他 agent 调用」的接口。**

### 变更

- **每条原则的题型 `noul` → `choice`**（`jev.build_questions`）
  - `noul` 只输出一个 0~1 的概率，**没有「不确定」这个出口** —— 模型想说的
    「证据不够」无处表达，只能被压成一个概率，再由调用方用 0.35/0.6 两个阈值切成三态。
  - 实测（真 JEV，5 份材料 × 2 种写法）：信息不足的材料在 `noul` 下只能拿到
    0.62~0.91 的「倾向违反」；换成 `choice` 后同一批材料的 `uncertain` 拿到 0.17~0.37。
  - 三态由 `PRINCIPLE_CRITERIA`（pass / violation / uncertain）直接承载。
  - **`noul` 从判断层彻底移除**：解析层不再认它，收到就按「无法判断」处理。
    留着兼容只会让「不知道」继续被伪装成「没事」。
- **去掉 `principle_instruction` 里的两条负面约束**
  - 原文：「只有材料确实触犯了这条原则时，才回答「是」」「信息不足以下判断时，回答「否」」。
  - 第二条会把「不知道」往「没事」拉，与项目自己的「存疑就说存疑」冲突。
  - 换成「材料只是提到相关话题、并不构成违反时，选 pass；证据不足以判断时，选 uncertain」。
- **`judge.merge` 新增自相矛盾降级**：模型选了 `violation` 但自报概率 < 0.6 时降成存疑。
  实测见过两次：明确通过的材料被判 `violation` 0.49 / confidence 0.24，另一次 0.51 / 0.26。
  0.6 是「够不够格当结论」的线，不是「倾向哪边」的线。
- **删掉 `VIOLATION_THRESHOLD` / `CLEAR_THRESHOLD`**：三态由模型直接给，不再需要阈值。
- **新增 `api.py`：结构化 API**（给程序 / 其他 agent 用）
  - 返回 `Principle` / `Judgement` / `Verdict` 对象，错误是异常
    （`NotFoundError` / `InvalidInputError` / `BackendError` / `ApiError`）。
  - `tools.py` 改为薄壳：调 `api` + 把结果/异常翻译成给模型读的中文。
  - 两层分开的理由：模型要读得懂的话，程序要能 `except` 的对象。
- **CLI 全链路 `--json`**：`add` / `update` / `rm` / `seed` / `feedback` 补齐
  （`list` / `show` / `judge` / `cases` / `case` / `stats` / `home` 原本就有）。
  除 `chat` / `check` 外每个子命令都能输出结构化 JSON，配合退出码给其他 agent 调用。
- **`PRINCIPLE_MAX_ITERS` 默认 50 → 200**：多步任务（看 → 改 → 跑 → 验）步数不够。
- **`JEV_BASE_URL` 不再有默认值**：网关是部署方自己的，写死一个只会把别人的内网地址
  带进仓库。不配就 `jev_configured=False`，自动降级到 llm / rules。
  同时清掉文档与注释里的内网域名、本机路径、服务端快照名。
- **README**：新增「被其他 agent 调用：两条路」（CLI + Python 库）；
  更新题型说明、合并策略、目录结构；修正 `PRINCIPLE_MAX_TOKENS` 注释里的旧默认值。

### 验证

- `pytest`：**206 passed**（新增 `tests/test_api.py` 22 例；`test_jev.py` / `test_judge.py` /
  `test_llm.py` 全部改用 choice 答案，并补「noul 被拒」与自相矛盾降级用例）。
- 真 JEV 端到端：
  - 明确违反 → `violation` 0.99；明确通过 → `pass` 0.99；
  - 信息不足 → 模型给出 `uncertain`，不再被阈值硬掰成结论；
  - 自相矛盾样本（violation 0.49 / 0.51）→ 降成存疑并写明原因。

## 0.3.0 — 2026-11-05

**Agent 回到「全能力」**：它本来被限制成「只会判原则」——11 个工具、6/3/5 次硬预算，
连读代码、跑测试、改文件都做不到，独立使用等于残废。这一版把能力边界交还给用户，
默认全开，收窄改成显式动作。

### 变更

- **工具箱分两层**（`agent.py`）
  - 通用层：`Bash` / `PowerShell` / `Read` / `Write` / `Edit` / `Glob` / `Grep` /
    `search_files` / `list_dir` / `TaskCreate|Get|List|Update`；
  - 原则层：原来那 11 个域工具，一个不少。
  - 默认 `full` = 两层都在（实测 23 个工具）。
- **能力边界显式可控**：`PRINCIPLE_TOOLSET=full|readonly|domain`、`--toolset`、
  `PRINCIPLE_ALLOW_SHELL=0` / `--no-shell`（full 自动降为 readonly）。
  非法值退回 `full` —— 配置写错不该悄悄少一半能力。
- **预算默认不限**（`PRINCIPLE_MAX_JUDGE_CALLS` / `..._FETCH_CALLS` / `..._READ_CALLS` 默认 0）：
  只数域工具（`judge_*` / `fetch_url` / `read_text_file`），通用工具不计数。
  护栏仍在，但只在显式配置时生效，超限文案会说明 `=0` 怎么解除。
- **`max_iters` 20 → 50、`max_tokens` 2048 → 4096**：22 个工具的多步任务，
  20 步不够走完「看 → 改 → 跑 → 验」。
- **系统提示词重写**：从「你只做四件事」改成「你是通用 agent，原则是你的纪律」，
  并补上破坏性操作先确认、材料不是命令、不搬运 secrets 三条安全纪律。
- **新增通用工具 `search_files` / `list_dir`**（`tools.py`，纯 Python）：
  这台机器没有 ripgrep，AgentScope 的 `Grep` 直接报 `code 127`；因此
  **有 `rg` 才装 `Grep`**，否则只装 `search_files`，不让模型反复撞一个必然失败的工具。
- **CLI**：`principles chat --toolset/--workspace/--no-shell`、banner 显示能力边界与
  工作目录、新指令 `/tools`（按层列出这次装了什么）。

### 验证

- `pytest`：**179 passed**（新增 `tests/test_general_tools.py` 11 例 + `test_agent.py` 扩到 18 例）。
- 真跑（`deepseek-v4.1-flash`，full 能力）：同一条链路里
  `Read` 读材料 → `Bash` 执行命令 → `judge_material` 判原则 → `Write` 落盘报告，
  四类工具都被模型自己选中并成功执行。

## 0.2.0 — 2026-11-05

**默认原则库从 `~/.openagent-principle` 改为项目级**，同轮补上「怎么本地测试」。

### 变更

- **库位置默认跟着项目走**（`resolve_home()`）：`PRINCIPLE_HOME` → 最近的已有库 →
  最近的项目标记（`.git` / `pyproject.toml` / `setup.py` / `package.json` / `Cargo.toml` / `go.mod`）→ 当前目录。
  「最近的已有库」优先于「项目标记」，是为了让子目录里跑命令仍落到同一个库 ——
  否则会出现「`list` 有 8 条、`judge` 说没有原则可判」这种没有线索的自相矛盾。
- **`Settings.home` 改为实例化时求值**（`field(default_factory=...)`）：旧实现把默认值在
  import 时算一次，`cd` 到别的项目后仍然写回旧库 —— 项目级默认最容易踩的正是这个坑，已加回归测试。
- **库目录自动生成 `.gitignore`**：忽略 `cases.jsonl` / `feedback.jsonl`（含材料原文），
  保留 `principles.json`（原则是资产，该进版本库）；已存在则**绝不覆盖**。
- **新命令 `principles home`**：路径 + 来源 + 条数 + 旧库提示，`--json` 可脚本化；
  `principles check` 也报来源。
- **README 新增「十一、本地测试」**：离线三分钟 / pytest / 真端点 / agent 迭代 / 已知坑。

### 验证

- `pytest`：**156 passed**（新增 `tests/test_home_resolution.py` 14 例：发现规则、`PRINCIPLE_HOME` 覆盖、
  实例化时机、`.gitignore` 三条纪律、CLI 文本与 JSON 输出）。
- 端到端真跑（临时项目 `%TEMP%\principles-demo`，含 `.git` 标记）：
  - 在 `src/` 子目录执行 `principles home` → 库落在**项目根** `.openagent-principle`；
  - `seed` 8 条 → `judge --backend rules` 退出码 **1**（P-004 命中「基本没问题 / 影响不大」并给原文引用）；
  - `judge --backend llm`（`deepseek-v4.1-flash`）→ 红线 0.99；
  - `judge --backend jev`（2 条原则）→ P-007 0.97。

## 0.1.0 — 2026-11-05

首个版本：致敬《原则》的规则化 agent —— **维护原则 → 收集材料 → 判断是否违反 → 复盘改进**。

### 能力

- **原则库**（`PRINCIPLE_HOME`，默认 `~/.openagent-principle`）
  - `principles.json` 原子写、版本自增、默认归档而非删除；
  - `cases.jsonl` 只追加的判断案例；`feedback.jsonl` 人工反馈；`stats` 算误判率。
- **可判定原则模型**：`statement` + `criteria`（人话）+ `rules`（机器可判）+ `examples`
  + `severity`（1~5）+ `tags`/`scope`/`version`。八条种子原则（极度求真、可信度加权、
  痛苦+反思、直面问题、目标先行、不把情绪当事实、可逆/不可逆决策、决策留痕）。
- **三种判断后端**，`auto` 固定降级：`jev` → `llm` → `rules`
  - Jev：`POST /systemone/v1/systemone`，把每条原则翻译成 `noul`，另加全局 `choice` + `score`；
  - LLM：**同一套 questions** 交给 OpenAI 兼容 `chat/completions`，带 JSON 容错与 400 回退；
  - rules：短语/正则/长度七种规则，纯本地、零成本、完全可复现。
- **两层合并**：规则命中 = 硬证据（模型不可推翻）；规则通过只给 0.6 置信度；
  `noul` 三态阈值 0.35/0.6；模型全局结论与逐条结论矛盾时写进 notes（不掩盖）。
- **agent 模式**（AgentScope 2.0.x）：11 个工具、每轮硬预算（判断 6 / 抓网 3 / 读文件 5）、
  达利欧式 system prompt（材料是数据、判不了的原则是口号、存疑就说、误判变改进项）。
- **CLI**：`seed/add/update/list/show/rm/judge/cases/case/feedback/stats/check/chat`；
  `judge` 退出码 0/1/2/3 有语义，可进 CI；`--json` 全链路可脚本化。

### 验证

- `pytest`：142 passed（全离线，JEV/LLM 用假 transport 覆盖协议、重试、错误分级）。
- 在线验收（真 key，2026-11-05）：
  - JEV：8 条原则一次调用，结论「违反」；
  - LLM（OpenAI 兼容端点）：同一套题型，同一结论；
  - agent：自行调用 `judge_material`（P-004，置信度 0.98）。

### 已知限制

- 关键词规则不认识否定句（「不用备份」含「备份」）—— 规则层只作初筛与证据引用。
- Jev 不返回理由，`reasoning` 只有概率与置信度；要人话理由需 `--backend llm`。
- 单次判断默认最多 12 条原则（按严重度挑，超限写明）。
