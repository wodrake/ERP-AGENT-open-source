# ERP 智能采购助手 — Harness Engineering 架构

> 基于 DeepAgent + LangGraph + MCP 协议的摩托车零部件采购智能助手，严格遵循 **Harness Engineering** 架构思想（Planning → Executing → Review → Result）。

---

## 项目简介

本项目是一个面向摩托车零部件采购管理场景的 **AI Agent 系统**。通过 DeepSeek 模型驱动的智能体，与已部署的 Java ERP 后端进行交互，实现：

- 供应商智能分析（信用评级、供货能力对比）
- 采购订单全生命周期管理（创建/修改/审批）
- 库存预警与出入库管理
- 零部件搜索与供应商关联查询
- 数据可视化图表生成（26 种图表类型）
- 结构化文档输出（Markdown/HTML/CSV/JSON）
- 人工审批流程（HITL — Human-in-the-Loop）

---

## 2026-09-25 更新：可切换审查路由与模型配对评测

本次新增 **JEV 路由适配器与可复现评测流程**。只切换模糊请求的前置判断模型，不替换主 Agent、Grader，也不改变 ERP 工具、人工审批或执行后升级审查机制。

- **可配置的模型入口**：新增 `build_review_router` 与 `JevRouter`，支持通过配置选择 DeepSeek 或 JEV；两者统一返回 `ReviewRoute`，复用原有工作流。
- **JEV 结构化判断**：一次请求包含审查决定、任务类型两个 Choice，以及计算、来源、约束、比较、产物五个 Noul 问题；校验后映射为受限检查项。JEV 分支的简短理由由适配器生成，不是模型自然语言解释。
- **异常处理与回退**：保留默认 8 秒异步总等待限制；超时、接口错误、无效结构或不确定判断保守进入审查，不自动重试，不绕过 HITL。
- **配对评测**：冻结 200 条合成样本，按 50 个场景组划分为 80 条开发集和 120 条测试集，保存数据哈希、逐题结果、模型版本、耗时与用量。每个模型正式调用 160 次，其余 40 条由相同规则处理。
- **回归验证**：新增 14 项适配与评测辅助测试，连同原有 15 项混合审查测试，共 29 项通过。

### 首轮实测结果

以下比较冻结测试集中真正进入模型的 **107 条模糊请求**，模型分别为 `deepseek-flash` 和 `jev-1.13.0`：

| 指标 | DeepSeek | JEV |
|---|---:|---:|
| 与参考标签一致率 | 93.46%（100/107） | 85.98%（92/107） |
| 应审查召回率 | 88.33%（53/60） | 75.00%（45/60） |
| 误审率 | 0%（0/47） | 0%（0/47） |
| API 有效返回率 | 100% | 100% |
| 路由调用 P95 | 1133.06 ms | 1245.79 ms |

**仓库默认仍保留 DeepSeek，JEV 作为可选实验后端。** 首轮结果不支持准确率或延迟提升；本次成果是模型可替换、评测可复现，以及定位规则误审和模型漏审。参考标签由开发辅助工具编写，尚未经独立人工复核，不能将这些结果称为线上准确率或端到端任务成功率。

### 启用与回退

在本地 `.env` 中配置，密钥不要提交 Git：

```dotenv
REVIEW_ROUTER_PROVIDER=jev
TYPESAFE_API_KEY=填写自己的密钥
TYPESAFE_MODEL=jev-1.13.0
```

修改后重启后端，使 Agent Session 重建。回退只需将 `REVIEW_ROUTER_PROVIDER` 改为 `deepseek` 并重启；这不会切换主对话或 Grader 的模型。

接入与复现见 [评测说明](evals/review_routing/README.md)，样本规范见 [标签定义](evals/review_routing/LABELING.md)，逐项指标见 [完整报告](evals/review_routing/results/paired-v1/REPORT.md)，错误分析与选型结论见 [评测结论](evals/review_routing/FINDINGS.md)。

## 2026-09-19 更新

本次发布混合式按需审查，并完善本地前端启动兼容性。

- **规则与模型协同路由**：明确的简单请求直接跳过 Grader，明确业务请求由规则触发；模糊请求结合最近对话、计划和待办，调用模型返回结构化审查决定。路由关闭思考、限制输出和等待时间，超时或无效结果保守进入审查。
- **执行信号升级审查**：在 Grader 运行前检查本轮工具记录，根据工具错误、多工具或多来源调用、写操作及子 Agent 委派等信号升级审查，并保存决策来源和原因。
- **防止评审驱动的重复执行**：调用写入、执行类工具或委派子任务后采用单轮评审，失败不自动重放整轮任务；只读任务保留有限次修改重审，订单写操作继续受双层 HITL 约束。
- **输出与配置**：内部路由模型内容不进入用户回答流；支持 `review.strategy: hybrid/rules` 与 `review.mode: auto/always/never`，任务检查项不再一律要求图表或报告。
- **前端兼容与启动说明**：兼容浏览器扩展在根 HTML 上注入属性引发的 hydration 警告；补充 Turbopack 对跨项目 `node_modules` 软链接的限制及在前端目录安装本地依赖的说明。
- **验证**：混合审查的 15 项回归测试通过，覆盖路由、异常降级、执行升级、状态清理与写操作防重放。

配置及工作原理见下方“混合审查路由”，本机启动步骤见 [WSL_START.md](WSL_START.md)。

## 2026-09-17 更新

本次新增三层记忆并修复其主链路集成问题，保留原有按需 Grader、用户级沙箱、Skills 恢复及双层 HITL 能力。

- **三层记忆**：HOT 使用会话状态与 checkpoint；WARM 在每次模型调用前动态读取当前用户的偏好和近期情节摘要；COLD 由 `read_memory` 按需检索。新增 `remember` 显式写入工具。
- **记忆治理**：统一 `MemoryKeeper` 管理语义、情节、程序记忆，支持偏好合并、版本失效标记、TTL 清理和数量淘汰。统一入口不等同于数据库事务或分布式并发保证。
- **修复跨会话覆盖**：从 LangGraph 运行配置获取 `thread_id`；缺少 ID 时跳过归档，避免所有会话写入 `ep_unknown`。
- **修复记忆不刷新**：新增 `WarmMemoryMiddleware`，即使 Agent Session 已缓存也会刷新记忆，单次注入摘要最多 4000 字符（不是 token）。异步调用通过线程执行同步 Store 读取。
- **修复配置及抽取**：加载 `.env` 中的记忆配置，接通可选 LLM 抽取与规则回退；默认仍关闭额外模型抽取。只处理最新用户轮次，并跳过规则能识别的否定、明确临时表达，减少旧消息反复覆盖偏好。
- **其他兼容修复**：MongoDBStore 构造 Item 时提供时间戳；子 Agent 工具匹配改为精确优先、下划线前缀其次、子串兜底，未匹配时告警。子串兜底仍有权限误匹配风险。
- **验证**：34 项记忆基础测试和 8 项真实 LangGraph 离线集成测试通过。

本机 WSL 启动与回退见 [WSL_START.md](WSL_START.md)。修复前标签为 `memory-before-fixes-20260917`，核心修复标签为 `memory-fixed-20260917`；文档更新可能晚于该标签。版本回退不回滚数据库内容，旧 `ep_unknown` 已覆盖的数据也不会自动恢复。

## 技术栈

| 层级 | 技术 | 说明 |
|------|------|------|
| **LLM** | DeepSeek deepseek-flash | DeepSeek API（对话、grader、联网搜索共用） |
| **审查路由** | DeepSeek / JEV 可配置切换 | 模糊请求前置判断；JEV 使用独立 TypeSafe Key |
| **Agent 框架** | DeepAgent + LangGraph | 状态图引擎，支持中断/恢复/子Agent |
| **MCP 协议** | FastMCP + SSE | Agent ↔ ERP 的工具桥接层 |
| **Web 框架** | FastAPI + Uvicorn | SSE 流式响应 |
| **前端** | Next.js 16 + React 19 + TailwindCSS 4 | 流式对话 UI + 中断交互 |
| **数据库** | MongoDB (Motor/Pymongo) | 会话/消息/Store 持久化 |
| **沙箱** | Docker SDK + 7 层安全防护 | 隔离代码执行环境 |
| **图表** | Matplotlib + Pandas | 26 种图表生成 |
| **语言** | Python 3.11+ / TypeScript | 后端 Python，前端 TypeScript |

---

## 系统架构

```
┌─────────────────────────────────────────────────────────────────┐
│                    Frontend (Next.js :3000)                       │
│              SSE 流式对话 + HITL 中断交互 + 历史管理              │
└────────────────────────────┬────────────────────────────────────┘
                             │ HTTP / SSE
┌────────────────────────────▼────────────────────────────────────┐
│              Backend API (FastAPI :8000)                          │
│   chat.py (SSE流) + history.py (会话CRUD) + agent_loader.py      │
│   MongoDBStore + MongoDBSaver (生产级持久化)                      │
└────────────────────────────┬────────────────────────────────────┘
                             │
┌────────────────────────────▼────────────────────────────────────┐
│              Agent Core (DeepAgent)                               │
│  ┌──────────┐ ┌───────────────┐ ┌─────────────┐ ┌────────────┐ │
│  │ LLM      │ │ Composite     │ │ 中间件栈     │ │ 2 子Agent  │ │
│  │ DeepSeek │ │ Backend       │ │ (自定义 +    │ │ analyst    │ │
│  │          │ │ (Docker+Store)│ │ 框架内置)    │ │ order      │ │
│  └──────────┘ └───────────────┘ └─────────────┘ └────────────┘ │
│  ┌─────────────────────────────────────────────────────────────┐│
│  │ Tools: 23 MCP + 12 Custom = 35 个显式工具（默认）             ││
│  │ chart(26种) + web_search + web_fetch + install_skill         ││
│  │ + hitl_tools + download_sandbox_file + document_generator    ││
│  └─────────────────────────────────────────────────────────────┘│
└────────────────────────────┬────────────────────────────────────┘
                             │ MCP (SSE)
┌────────────────────────────▼────────────────────────────────────┐
│              MCP Server (FastMCP :9000)                           │
│   suppliers(5) + parts(5) + orders(7) + inventory(6) = 23 tools │
└────────────────────────────┬────────────────────────────────────┘
                             │ HTTP REST
┌────────────────────────────▼────────────────────────────────────┐
│              Java ERP 后端 (:8081)                                │
│              http://localhost:8081（可替换为你的 ERP 地址）         │
└─────────────────────────────────────────────────────────────────┘
```

---

## 核心功能

### 1. Harness 工作流（Planning → Executing → Review → Result）
Agent 严格遵循四阶段工作流：
- **Planning**：分析用户意图，生成任务规划（前端展示 TodoList）
- **Executing**：调用 MCP 工具 / 沙箱执行代码 / 委派子Agent
- **Review**：按需审查执行结果，验证数据完整性（问候、感谢和纯概念问答默认跳过 grader；查询、分析、下单等业务任务自动启用）
- **Result**：结构化输出最终结果

审查策略配置在 `src/agent/harness_config.yaml`。`review.mode` 支持 `auto`（默认）、`always` 和 `never`；`review.strategy` 默认 `hybrid`，也可切换为 `rules` 保留纯规则入口。

#### 混合审查路由

- 明确问候、单一概念定义且没有待办任务时直接跳过；明确复核、业务处理或写操作请求由规则触发。
- 模糊请求通过 `build_review_router` 选择 DeepSeek 或 JEV，输入最近 6 条截断消息及计划、待办摘要，统一输出 `skip/review/uncertain`、任务类型、简短理由和受限检查项。DeepSeek 分支与主对话共用现有 Key，关闭思考，最多输出 400 token；JEV 分支使用独立 Key 和结构化判断接口，理由由适配器生成。两者都不自动重试，原异步入口总等待默认 8 秒，HTTP 请求也设置超时。
- 每次图调用只在入口路由一次，不因 Grader 重做重复路由；超时、格式不合法或 uncertain 保守进入评审。此调用有额外成本，不计入主 Agent 的 ModelCallLimit，单独以次数、超时及输出上限约束。
- 执行结束、Grader 运行前再次检查本轮工具记录：工具错误、多工具/多来源调用、写操作与子任务委派可以升级原先的免审决定。当前是保守信号规则，不宣称能够自动识别所有事实冲突。
- 固定业务底线不可由路由模型改写，模型只选择预定义的计算、来源、约束、比较、产物检查项。图表和完整报告不再作为所有分析任务的强制交付物。
- 调用写入、执行类工具，或委派可能隐藏写操作的子任务后，只进行单轮 Grader 评审，失败不自动重放整轮任务。只读任务保留有限次修改重审。这是评审循环防重放，不代替业务幂等和执行前 HITL。
- `never` 明确关闭自动评审（执行信号也不升级），但不关闭 HITL；调用方显式传入 rubric 时保留其评审标准。
- `review_decision` 保存判断来源和升级信号，写入 Harness trace；内部路由模型输出不作为前端回答流发送。

修改配置后重启后端。需要快速对照旧规则时设置 `strategy: rules`；执行信号升级仍保留。

```bash
python -m unittest src.test.test_hybrid_review -v
```

新增 15 项回归测试，覆盖模型路由、超时降级、执行升级、跨轮状态清理、HITL 恢复、写操作防重放和只读任务有限重审。

### 2. Docker 安全沙箱（7 层防护）
```
1. --read-only          文件系统只读
2. --tmpfs /tmp         临时目录内存挂载（限制大小）
3. --memory="512m"      内存上限
4. --cpus="1.0"         CPU 上限
5. --network bridge     网络隔离/受限
6. --cap-drop ALL       移除所有 Linux Capability
7. --security-opt       seccomp 系统调用白名单
```
支持可扩展多语言运行时：Python / Go / Node.js

### 3. 沙箱五态生命周期管理
```
预热池(WARM) → 认领(CLAIMED) → MongoDB缓存(CACHED)
     ↑                              │
     │ 补充预热                      │ 故障/超时
     │                              ↓
  新建(CREATE) ←────────────── 销毁(DESTROY)
```

### 4. HITL 人工审批
- 订单创建/更新触发 `interrupt_on` 中断
- 前端展示审批卡片，用户批准后恢复执行
- 缺少字段时触发信息补充中断

### 5. 子Agent 委派
- **procurement-analyst**：采购分析师（数据分析 + 图表生成）
- **procurement-order**：订单专家（订单 CRUD + 审批流程）

### 6. 项目显式注册的中间件栈
| # | 中间件 | 职责 |
|---|--------|------|
| 1 | SandboxHealthMiddleware | 沙箱健康检查 + 自动重连 |
| 2 | HarnessPhaseMiddleware | 阶段状态机 + 规则与模型混合路由 |
| 3 | ContextInjectionMiddleware | 用户上下文注入（工厂模式隔离） |
| 3.5 | WarmMemoryMiddleware | 每次模型调用动态读取当前用户记忆，摘要上限 4000 字符 |
| 4 | SkillsSyncMiddleware | 技能文件夹级增量同步 |
| 5 | UserSkillsRestoreMiddleware | 用户自定义技能恢复 |
| 6 | ToolsSummarizationMiddleware | 工具调用摘要监控 |
| 7 | MemoryUpdateMiddleware | 用户偏好自动提取与合并（WARM 层） |
| 8 | MemoryConsolidationMiddleware | 情节归档 + 遗忘扫描（COLD 层） |
| 9 | SandboxCircuitBreakerMiddleware | 沙箱熔断器（三态模型） |
| 10 | SafeRubricMiddleware | 基于框架 RubricMiddleware 审查；写操作后禁止自动重放 |
| 10.5 | ReviewExecutionGate | 根据本轮工具记录升级评审，after_agent 逆序下先于 Grader 执行 |
| 11 | ModelCallLimitMiddleware | 模型调用次数限制 |
| 12 | ToolCallLimitMiddleware | 工具调用次数限制 |

### 7. 默认 35 个显式注册工具
- **23 个 MCP 工具**：供应商(5) + 零部件(5) + 订单(7) + 库存(6)
- **10 个自定义工具**：chart_generator, web_search, web_fetch, install_skill, list_user_skills, request_order_info, download_sandbox_file, list_sandbox_files, generate_document, generate_table_report
- **2 个记忆工具**：read_memory、remember；`MEMORY_TOOLS_ENABLED=false` 时不注册。

以上不包含 DeepAgents 自动提供的文件、执行、规划和委派工具，实际可用工具还取决于 MCP 连接与框架配置。

---

## 三层记忆架构（HOT / WARM / COLD）

三层按访问方式与上下文用途划分，不是三个独立数据库，也不是严格的缓存逐级淘汰协议：

| 层 | 内容 | 介质 | 体积目标 | 策略 |
|---|---|---|---|---|
| **HOT** | 当前会话消息、工具结果、活跃 todo | LangGraph 状态及 Checkpointer（MongoDB） | 由框架管理 | checkpoint 用于恢复；归档不会自动删除原消息 |
| **WARM** | 有效语义记忆、近 7 天情节摘要（默认最多 5 条） | MongoDBStore 中的记录，动态注入 system prompt | 摘要最多 4000 字符 | 每次模型调用重新读取，超长截断 |
| **COLD** | 持久化历史情节、程序记忆等 | MongoDBStore，按需检索 | 情节默认最多 200 条，保留 90 天 | BM25 加权排序后返回 top-k，过期清理按调用触发 |

### 记忆类型

记忆内容按用途分为三类：

- **语义记忆 semantic**：用户偏好等稳定事实，记录生效、失效时间及版本关系。用户改口时旧值标记失效，按历史保留策略清理；这不是完整的双时态数据库。普通检索只返回有效记录，旧值需通过历史查询接口检查。
- **情节记忆 episodic**：每次会话归档一条（`ep_<thread_id>`，幂等），
  90 天后遗忘，7 天后从 WARM 下沉到 COLD
- **程序记忆 procedural**：经验与流程，按需检索，不做无脑注入

### 命名空间规范

```
("memories", <user_id>, "semantic")     用户级稳定事实
("memories", <user_id>, "episodic")     用户级情节归档
("memories", <user_id>, "procedural")   用户级经验/流程
("memories", "org",     "policies")     预留组织策略命名空间，未接入主链路
("user-preferences", <user_id>)         WARM 投影（偏好字典缓存，供提示词注入）
```

`user-preferences` 命名空间保留但**语义变了**：它不再是记忆本体，而是 WARM 层
的一份兼容投影；自动偏好合并会同步写入语义记录，旧数据支持迁移。`remember` 直接写记忆本体，不保证更新这份投影；动态 WARM 注入以记忆本体为准。

### 关键实现

| 文件 | 职责 |
|---|---|
| `src/agent/memory/types.py` | 记忆条目模型（生效失效时间、溯源、生命周期） |
| `src/agent/memory/keeper.py` | **唯一写入口**：合并、冲突消解、检索、遗忘 |
| `src/agent/memory/scoring.py` | COLD 检索打分（BM25 + 时间衰减 + 频次 + 置信度） |
| `src/agent/memory/extractor.py` | 偏好抽取与情节摘要（默认零 LLM 调用） |
| `middlewares/memory_update.py` | WARM 层偏好写入 |
| `middlewares/memory_consolidation.py` | 情节归档 + 遗忘扫描（按间隔节流） |
| `middlewares/warm_memory.py` | 动态加载 WARM 摘要及长度限制 |
| `src/agent/memory/run_config.py` | 从当前图运行配置读取会话 ID |
| `tools/memory_tools.py` | `read_memory` / `remember`，COLD 层按需取用 |

新记忆的读写逻辑集中在 `MemoryKeeper`，便于统一合并与生命周期策略，但没有实现跨记录事务。情节归档采用规则截取而非 LLM 总结，遗忘按用户节流、在执行结束时触发，不是独立后台任务。Store I/O 仍有成本；异常按降级策略处理，不应描述为“零延迟”。当前每类记录检索最多读取 500 条，规模扩大后需补分页、索引与并发一致性机制。

### 验证

```bash
python -m src.test.test_memory_layer
python -m unittest src.test.test_memory_integration -v
```

基础套件 34 项；缺少框架依赖时其中集成部分会跳过。新增 8 项集成测试要求安装项目 Python 依赖，使用真实 LangGraph、模拟模型和内存 Store，覆盖不同会话独立归档、同步与异步注入、用户隔离、配置、否定表达、旧消息和失败降级。两套均不需要真实模型 Key、MongoDB 或 Docker。

---

## 项目结构

```
ERP-AGENT/
├── frontend/                          # Next.js 前端
│   ├── src/
│   │   ├── app/                       # App Router
│   │   ├── components/                # UI 组件
│   │   │   ├── chat/                  # 对话区（消息/输入/工具调用/思考动画）
│   │   │   ├── interrupt/             # HITL 中断交互（审批/补充信息）
│   │   │   ├── sidebar/              # 侧边栏（历史/搜索）
│   │   │   └── common/               # 通用组件
│   │   ├── hooks/                     # useChat / useSSE / useHistory
│   │   └── lib/                       # API / SSE解析 / 类型定义
│   └── package.json
│
├── src/                               # Python 后端
│   ├── agent/                         # Agent 核心
│   │   ├── main_agent.py              # 主入口：create_main_agent() 7步组装
│   │   ├── config.py                  # 全局配置
│   │   ├── middleware_config.py       # 子Agent中间件工厂
│   │   ├── backends/                  # Docker 沙箱后端
│   │   │   ├── custom_opensandbox.py  # Docker SDK 封装（30+ 方法）
│   │   │   ├── sandbox_setup.py       # 安全沙箱创建 + 多语言运行时
│   │   │   ├── sandbox_manager.py     # 五态生命周期管理
│   │   │   ├── sandbox_proxy.py       # 代理层（热替换）
│   │   │   └── seccomp.json           # seccomp 安全策略
│   │   ├── middlewares/               # 自定义中间件
│   │   ├── tools/                     # 10 个基础自定义工具 + 2 个记忆工具
│   │   │   ├── document_generator.py  # 文档生成（MD/HTML/CSV/JSON）
│   │   │   ├── download_sandbox_file.py # 沙箱文件下载
│   │   │   ├── chart_generator.py     # 26 种图表
│   │   │   ├── web_fetch.py           # URL抓取 + Skill安装
│   │   │   └── hitl_tools.py          # HITL 人工介入
│   │   ├── subagents/                 # 子Agent（YAML声明式）
│   │   └── memory/                    # 三层记忆 + 系统提示词
│   │       ├── types.py               #   记忆条目模型（双时态/溯源/生命周期）
│   │       ├── keeper.py              #   唯一读写口（合并/冲突/检索/遗忘）
│   │       ├── scoring.py             #   COLD 检索打分（BM25 + 衰减）
│   │       ├── extractor.py           #   偏好抽取 + 情节摘要（零 LLM 调用）
│   │       ├── namespaces.py          #   命名空间规范
│   │       ├── config.py              #   分层与生命周期参数
│   │       └── prompts.py             #   系统提示词 + 记忆使用规范
│   ├── api_view/                      # FastAPI Web 层
│   │   ├── web_main.py                # 应用入口
│   │   ├── agent_loader.py            # Agent 单例（MongoDB持久化）
│   │   ├── mongodb_store.py           # LangGraph Store（MongoDB实现）
│   │   └── api/                       # 路由（chat + history）
│   ├── mcp_server/                    # MCP 网关（23个ERP工具）
│   ├── skills/                        # 技能文件（文件夹级）
│   └── download/                      # 生成文件下载目录
│
├── .env.example                       # 环境变量模板（复制为 .env）
├── requirements.txt                   # Python 依赖
└── README.md                           # 项目说明与启动指南
```

---

## 快速启动

### 环境要求

- Python 3.11+
- Node.js 18+
- MongoDB 6.0+
- Docker Desktop（已启动）
- DeepSeek API Key（对话、grader、联网搜索共用）

### 1. 克隆项目 & 安装依赖

```bash
# 克隆项目
git clone <repo-url>
cd ERP-AGENT

# Python 依赖
pip install -r requirements.txt

# 前端依赖
cd frontend
npm install
cd ..
```

### 2. 配置环境变量

编辑项目根目录 `.env` 文件：

```bash
# DeepSeek API Key（主对话、grader、联网搜索共用）
DEEPSEEK_API_KEY=sk-your-api-key
LLM_MODEL=deepseek-flash
LLM_BASE_URL=https://api.deepseek.com
WEB_SEARCH_MODEL=deepseek-flash
DEEPSEEK_RESPONSES_URL=https://api.deepseek.com/responses

# MongoDB 连接
MONGODB_URI=mongodb://localhost:27017

# Java ERP 后端地址
# 若没有自己的 ERP 服务，可先保留占位地址，联网搜索和基础对话仍可启动
ERP_BASE_URL=http://localhost:8081

# MCP Server 地址（本地）
MCP_SERVER_URL=http://localhost:9000

# 沙箱 Docker 镜像
SANDBOX_IMAGE=python:3.11-slim
```

### 3. 启动 MongoDB

```bash
# 确保 MongoDB 正在运行
mongod --dbpath /path/to/data

# 或使用 Docker
docker run -d --name mongodb -p 27017:27017 mongo:6.0
```

### 4. 启动 Docker 沙箱容器

```bash
docker run -d \
  --name erp-sandbox \
  -w /workspace \
  python:3.11-slim \
  sleep infinity
```

### 5. 启动 MCP Server（端口 9000）

```bash
python -m src.mcp_server.server_main
```

看到以下输出表示成功：
```
🚀 Starting MCP Server on 0.0.0.0:9000 (SSE transport)
Uvicorn running on http://0.0.0.0:9000
```

### 6. 启动后端 API（端口 8000）

```bash
python -m src.api_view.web_main
```

看到以下输出表示成功：
```
AgentLoader initialized
Starting ERP Agent Web Server...
Uvicorn running on http://0.0.0.0:8000
```

### 7. 启动前端（端口 3000）

```bash
cd frontend
npm run dev
```

### 8. 访问应用

浏览器打开 http://localhost:3000 即可使用。

---

## 启动顺序总结

```
MongoDB → Docker沙箱 → MCP Server(:9000) → Backend API(:8000) → Frontend(:3000)
```

> 注意：MCP Server 必须在 Backend API 之前启动，因为 Agent 初始化时会连接 MCP Server 加载 23 个 ERP 工具。

---

## 项目亮点

### 1. 真正的 Harness Engineering 架构
不是简单的 ChatBot，而是严格遵循 **Planning → Executing → Review → Result** 四阶段工作流。前端实时展示每个阶段的状态变化（Phase Bar + TodoList），用户可清晰看到 Agent 的思考和执行过程。

### 2. 生产级 Docker 安全沙箱
7 层安全防护（只读文件系统 + tmpfs + 资源限制 + 网络隔离 + Capability 移除 + seccomp 白名单 + PID 限制），不是玩具级沙箱。支持多语言运行时扩展（Python/Go/Node.js），项目文件完整同步到沙箱实现真正隔离测试。

### 3. 五态沙箱生命周期
预热池 → 认领 → MongoDB 缓存 → 新建 → 销毁。服务重启不丢失用户绑定关系，预热池保证 < 100ms 分配速度，健康检查 + 自动重建故障容器。

### 4. 完整的 HITL 审批流程
订单创建/更新需人工审批，缺少字段时触发信息补充中断。基于 LangGraph 的 interrupt/resume 机制，前端展示审批卡片和信息补充表单。

### 5. 中间件栈
沙箱健康检查、用户上下文注入（工厂模式防串扰）、技能增量同步、用户技能恢复、工具摘要监控、偏好自动提取、熔断器保护、调用限制。每个中间件都有明确的职责边界。

### 6. MCP 协议解耦
Agent 不直接调用 ERP API，而是通过 MCP Server 提供的 23 个标准化工具交互。MCP 层可独立部署、独立扩展，Agent 侧无需关心 ERP 接口细节。

### 7. MongoDB 全链路持久化
- **MongoDBSaver**：LangGraph Checkpointer（会话状态持久化）
- **MongoDBStore**：LangGraph Store（跨会话用户偏好/技能存储）
- **display_messages**：前端展示消息持久化
- **conversations**：会话列表管理

### 8. 子Agent 委派 + YAML 声明式配置
采购分析师和订单专家两个子Agent，通过 YAML 文件声明式配置（工具集、系统提示词、委派规则），主Agent 根据任务类型自动委派。

### 9. SSE 流式协议
完整的 SSE 事件协议：`thinking` → `token` → `tool_start` → `tool_result` → `phase` → `todo_update` → `interrupt` → `done`。前端逐 token 渲染，实时展示工具调用和阶段变化。

### 10. 技能系统（Skills）
文件夹级技能管理（SKILL.md + 脚本 + 依赖），支持安装/同步/恢复。SkillsSyncMiddleware 实现增量同步（SHA256 哈希比对），保留完整目录结构。

### 11. 三层记忆治理（HOT / WARM / COLD）
语义、情节、程序记忆分别建模，通过用户命名空间隔离。MemoryKeeper 集中处理偏好合并、旧版本失效、情节归档、TTL 和数量清理；时间衰减用于检索排序，不等同于删除。并发事务与大规模存储优化仍是后续工作。

### 12. 上下文经济
WARM 摘要在每次模型调用前刷新，最多 4000 字符；更早的任务与程序记忆由 `read_memory` 按需检索，采用 BM25、时间衰减、引用频次和置信度加权。默认偏好抽取使用规则，不额外调用 LLM；开启 `MEMORY_LLM_EXTRACTION` 后会增加模型调用成本。尚未测量真实任务的 token 节省比例。

---

## API 端点

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/api/chat/stream` | SSE 流式对话 |
| POST | `/api/chat/{thread_id}/resume` | 中断恢复 |
| GET | `/api/chat/{thread_id}/state` | 获取中断状态 |
| GET | `/api/chat/{thread_id}/history` | 获取消息历史 |
| GET | `/api/history/{user_id}` | 获取会话列表 |
| DELETE | `/api/history/{thread_id}` | 删除会话 |
| GET | `/api/download/{filename}` | 下载生成文件 |
| GET | `/health` | 健康检查 |

---

## 开发说明

- 修改 Agent 行为：编辑 `src/agent/memory/prompts.py`（系统提示词）
- 添加新工具：在 `src/agent/tools/` 创建工具文件，在 `main_agent.py` 注册
- 添加新中间件：在 `src/agent/middlewares/` 创建，在 `main_agent.py` 中间件栈中添加
- 修改子Agent：编辑 `src/agent/subagents/configs/*.yaml`
