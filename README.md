# Mini Claude Code (mini-cc)

> 用 ~600 行 Python 复刻 Claude Code 的核心架构。麻雀虽小，五脏俱全。

Claude Code 官方版本是一个 **~1900 文件、51万+行** TypeScript 代码的庞大工程。mini-cc 将其精髓压缩到 **5 个 Python 文件**，保留了完整的核心机制，让你能像使用 Claude Code 一样使用它——同时清晰地理解它是如何工作的。

---

## 快速开始

```bash
# 安装
cd mini-claude-code
pip3 install -e .
```

### 方式一：Anthropic 官方 API

```bash
export ANTHROPIC_API_KEY="your-anthropic-key"
mcc
```

### 方式二：OpenAI 兼容 API（OpenRouter、DeepSeek、智谱等）

```bash
export OPENAI_API_KEY="your-key"
export OPENAI_BASE_URL="https://openrouter.ai/api/v1"   # 替换为你的 provider
mcc -m anthropic/claude-opus-4-6                          # 替换为你的模型名
```

### 其他用法

```bash
# 非交互模式（一问一答）
mcc -p "列出当前目录下所有 Python 文件"

# 指定模型
mcc -m claude-sonnet-4-20250514
```

---

## 项目结构

```
mini-claude-code/
├── README.md           ← 你正在读的这个文件
├── setup.py            ← pip 安装配置
├── requirements.txt
└── mcc/
    ├── __init__.py
    ├── cli.py           ← 入口 + REPL 交互循环
    ├── engine.py        ← 查询引擎（核心 agentic loop）
    ├── tools.py         ← 工具系统（6 个工具）
    ├── prompt.py        ← 系统提示词构建
    └── permissions.py   ← 权限系统
```

---

## 架构解读：mini-cc 与 Claude Code 源码的对照

### 整体数据流

```
用户输入
  ↓
[cli.py] REPL 捕获输入，构建 user message
  ↓
[engine.py] 发送 messages + system_prompt + tools 到 API
  ↓
API 返回 streaming response（text 和/或 tool_use blocks）
  ↓
[engine.py] 如果有 tool_use:
  ├── [permissions.py] 检查权限 → 提示用户确认
  ├── [tools.py] 执行工具 → 得到 tool_result
  └── 将 tool_result 追加到 messages → 回到 API 调用 ← 这就是"agentic loop"
  ↓
如果没有 tool_use → 对话完成，等待下一轮用户输入
```

这个流程在 Claude Code 和 mini-cc 中**完全一致**，区别只在于实现的复杂度。

---

### 1. CLI 入口与 REPL (`cli.py`)

**对应 CC 源码**：
| mini-cc | Claude Code | 说明 |
|---------|-------------|------|
| `cli.py:main()` | `src/entrypoints/cli.tsx` | 启动入口，解析参数 |
| `cli.py:_run_repl()` | `src/screens/REPL.tsx` | 交互式 REPL 主循环 |
| `cli.py:_render_stream()` | `REPL.tsx → onQueryEvent()` | 消费流式事件并渲染 |
| `cli.py:_run_single()` | `src/cli/print.ts` | 非交互 `--print` 模式 |
| `cli.py:_print_banner()` | `src/components/LogoV2/` | 启动欢迎界面 |

**CC 怎么做的**：
CC 使用 React + Ink 构建终端 UI。REPL 是一个 React 组件，用户输入通过 `PromptInput` 组件捕获，经过 `processUserInput` 管线（处理斜杠命令、附件、bash 模式等），最终进入 `onQuery → query()` 流式循环。渲染使用 Ink 的虚拟 DOM 差异更新，维持 ~60fps 的流畅终端动画。

**mini-cc 怎么做的**：
直接用 `input()` 获取用户输入，用 `sys.stdout.write()` 逐字符流式输出，用 `rich` 库做颜色格式化。没有 React，没有组件树，没有虚拟 DOM——但用户体验本质相同：输入 → 流式输出 → 工具调用 → 继续输出。

---

### 2. 查询引擎 (`engine.py`) ← **最核心的文件**

**对应 CC 源码**：
| mini-cc | Claude Code | 说明 |
|---------|-------------|------|
| `engine.py:run_agent_loop()` | `src/query.ts → queryLoop()` | **while(true) agentic loop** |
| `engine.py:create_client()` | `src/services/api/client.ts` | Anthropic SDK 客户端创建 |
| `client.messages.stream()` | `src/services/api/claude.ts → queryModel()` | `anthropic.beta.messages.create({stream:true})` |
| tool_use 检测 | `query.ts` line 551-558 | `needsFollowUp` 标志 |
| tool 执行 + tool_result | `src/services/tools/toolExecution.ts` | `runToolUse → tool.call` |
| messages 追加 + 循环 | `query.ts` line 1714-1728 | `state.messages = [...old, ...assistant, ...toolResults]` |

**CC 的 queryLoop 核心伪代码**（对应 `src/query.ts`）：
```
while (true) {
    // 1. 可选：auto-compact 压缩上下文
    // 2. 调用模型（流式）
    stream = callModel(messages, systemPrompt, tools)
    
    // 3. 消费流式响应，收集 assistant blocks
    for await (event of stream) {
        assistantMessages.push(event)
        if (event has tool_use) needsFollowUp = true
    }
    
    // 4. 如果没有 tool_use → 退出循环
    if (!needsFollowUp) return { reason: 'completed' }
    
    // 5. 执行所有 tool_use blocks
    for (block of toolUseBlocks) {
        result = await runToolUse(block)  // permission check + execute
        toolResults.push(result)
    }
    
    // 6. 更新消息历史，继续循环
    messages = [...messages, ...assistantMessages, ...toolResults]
}
```

**mini-cc 的 `run_agent_loop` 做了完全一样的事**，只是：
- CC 用 `AsyncGenerator` + `yield`，mini-cc 也用 Python `Generator` + `yield`
- CC 有 StreamingToolExecutor（流式边收边执行），mini-cc 等流完再执行
- CC 有 auto-compact（上下文压缩）、maxTurns、budget、abort 信号等，mini-cc 只保留 maxTurns
- CC 的消息格式化经过 `normalizeMessagesForAPI`，mini-cc 直接拼字典

---

### 3. 工具系统 (`tools.py`)

**对应 CC 源码**：
| mini-cc | Claude Code | 说明 |
|---------|-------------|------|
| `Tool` dataclass | `src/Tool.ts → Tool type + buildTool` | 工具基类 |
| `ALL_TOOLS` / `TOOL_MAP` | `src/tools.ts → getAllBaseTools()` | 工具注册表 |
| `get_tool_schemas()` | `src/utils/api.ts → getApiToolSchemas()` | 生成 API tool 定义 |
| `BashTool` | `src/tools/BashTool/BashTool.tsx` (~900 行) | Shell 执行 |
| `FileReadTool` | `src/tools/FileReadTool/FileReadTool.ts` (~600 行) | 文件读取 |
| `FileWriteTool` | `src/tools/FileWriteTool/FileWriteTool.ts` (~400 行) | 文件写入 |
| `FileEditTool` | `src/tools/FileEditTool/FileEditTool.ts` (~500 行) | 文件编辑 |
| `GlobTool` | `src/tools/GlobTool/GlobTool.ts` (~200 行) | 文件搜索 |
| `GrepTool` | `src/tools/GrepTool/GrepTool.ts` (~400 行) | 内容搜索 |

**CC 的 Tool 接口**（`src/Tool.ts`）有 30+ 个字段：
```typescript
{
  name, aliases, description, inputSchema, outputSchema,
  call(),           // 执行入口
  checkPermissions(), // 权限检查
  isEnabled(), isConcurrencySafe, isReadOnly, isDestructive,
  validateInput(),  // 输入校验
  renderToolUseMessage(), renderToolResultMessage(), // UI 渲染
  mapToolResultToToolResultBlockParam(), // 结果→API格式
  getActivityDescription(), userFacingName, // 日志/展示
  preparePermissionMatcher(), // 安全匹配器
  ...
}
```

**mini-cc 的 Tool** 只保留 5 个字段：`name`, `description`, `input_schema`, `is_read_only`, `requires_permission`，和一个 `run()` 方法。这就够了。

**各工具的简化对照**：

| 功能 | CC 怎么做 | mini-cc 怎么做 |
|------|----------|---------------|
| **Bash** | 沙箱隔离、后台任务、进度 UI、sed 模拟、自动超时转后台 | `subprocess.run()` |
| **Read** | 图片/PDF/notebook 支持、token 预算、readFileState 去重 | `open().read()` + 行号 |
| **Write** | 陈旧性检查(mtime)、LSP 通知、VS Code 同步、备份 | `open().write()` |
| **Edit** | 引号规范化、结构化 patch、诊断追踪、LSP | `str.replace()` |
| **Glob** | ripgrep `--files` + 忽略规则 + 插件排除 | `glob.glob()` |
| **Grep** | ripgrep 全参数、分页、mtime 排序、多输出模式 | `subprocess.run(["rg", ...])` |

---

### 4. 系统提示词 (`prompt.py`)

**对应 CC 源码**：
| mini-cc | Claude Code | 说明 |
|---------|-------------|------|
| `build_system_prompt()` | `src/constants/prompts.ts → getSystemPrompt()` | 主提示词构建 |
| `_get_environment_info()` | `prompts.ts → computeSimpleEnvInfo()` | 环境信息收集 |
| `_get_git_info()` | `src/context.ts → getSystemContext()` | Git 状态 |

**CC 的系统提示词结构**（`src/constants/prompts.ts`）：
```
[Static / cache-friendly prefix]
├── getSimpleIntroSection()      → 身份 + 输出风格 + 安全规则
├── getSimpleSystemSection()     → 系统约束（markdown、权限模式、hooks）
├── getSimpleDoingTasksSection() → 任务执行规范（大段行为指南）
├── getActionsSection()          → 危险操作确认规则
├── getUsingYourToolsSection()   → 工具使用指南（按启用的工具动态生成）
├── getSimpleToneAndStyleSection() → 语气风格
├── getOutputEfficiencySection() → 输出效率

[__SYSTEM_PROMPT_DYNAMIC_BOUNDARY__]  ← 缓存分界线

[Dynamic sections]
├── session_guidance   → 会话级指导（AskUserQuestion, Agent, Skills...）
├── memory             → CLAUDE.md 持久记忆
├── env_info_simple    → 环境信息（CWD, OS, Git, 模型名）
├── language           → 用户语言偏好
├── mcp_instructions   → MCP 服务器指令
└── scratchpad, frc, summarize...
```

**mini-cc** 把这些压缩成一个 ~60 行的模板，保留了核心的：身份声明、行为规范、环境信息、工具使用指南、安全规则。

---

### 5. 权限系统 (`permissions.py`)

**对应 CC 源码**：
| mini-cc | Claude Code | 说明 |
|---------|-------------|------|
| `check_permission()` | `src/utils/permissions/permissions.ts → hasPermissionsToUseTool()` | 总决策函数 |
| `_prompt_user()` | `src/hooks/toolPermission/interactiveHandler.ts` | 交互式提示 |
| `_session_allow_all` | `bypassPermissions` 模式 | 会话级全部放行 |

**CC 的权限决策链**（`hasPermissionsToUseToolInner`）：
```
1. 检查 deny rules (配置) → 直接拒绝
2. 检查 ask rules (配置) → 强制提问
3. tool.checkPermissions(parsedInput) → 工具级权限
4. bypassPermissions 模式？ → 自动放行
5. always-allow rules？ → 自动放行
6. passthrough → 转为 ask
↓
然后外层 hasPermissionsToUseTool 再处理：
7. dontAsk 模式？ → 自动拒绝
8. auto 模式？ → AI 分类器决定
9. interactive → 弹出 PermissionRequest 组件让用户选
```

**mini-cc** 简化为：`is_read_only` → 放行，否则 → 提示用户 `y/n/a`。本质逻辑一样，只是少了规则引擎和 AI 分类器。

---

## CC 中被省略的复杂子系统

| 子系统 | CC 中的规模 | 作用 | 为什么省略 |
|--------|-----------|------|----------|
| React + Ink UI | ~140 组件 | 富终端 UI | mini-cc 用 print/rich 足够 |
| MCP (Model Context Protocol) | ~20 文件 | 外部工具服务器 | 独立协议，不影响核心 |
| 多智能体 (Coordinator/Swarm) | ~30 文件 | 并行 Agent 编排 | 高级功能，核心是单 agent |
| Bridge (VS Code/JetBrains) | ~15 文件 | IDE 集成 | CLI 专用 |
| OAuth / 认证 | ~20 文件 | 订阅/API key 管理 | 直接用环境变量 |
| Auto-compact | ~10 文件 | 长对话上下文压缩 | 短对话不需要 |
| 插件系统 | ~15 文件 | 第三方插件加载 | 核心不需要 |
| 记忆系统 (memdir) | ~10 文件 | 跨会话持久记忆 | CLAUDE.md 可手动管理 |
| Telemetry / Analytics | ~25 文件 | 使用量统计 | 无需 |
| 自动更新器 | ~5 文件 | npm/native 更新 | 无需 |
| Feature Flags | ~10 文件 | GrowthBook A/B 测试 | 无需 |
| Voice / Vim / Buddy | ~30 文件 | 语音输入/Vim模式/彩蛋 | 锦上添花 |

---

## 代码量对比

| 模块 | Claude Code | mini-cc | 压缩比 |
|------|------------|---------|--------|
| 入口 + REPL | ~3000 行 (cli.tsx + main.tsx + REPL.tsx) | ~150 行 (cli.py) | **20x** |
| 查询引擎 | ~2500 行 (QueryEngine.ts + query.ts + claude.ts) | ~130 行 (engine.py) | **19x** |
| 工具系统 | ~4000 行 (Tool.ts + tools.ts + 6个工具目录) | ~280 行 (tools.py) | **14x** |
| 系统提示词 | ~1500 行 (prompts.ts + systemPrompt.ts + context.ts) | ~80 行 (prompt.py) | **19x** |
| 权限系统 | ~2000 行 (permissions.ts + handlers/) | ~70 行 (permissions.py) | **29x** |
| **总计** | **~512,000 行 / 1900 文件** | **~710 行 / 5 文件** | **~720x** |

---

## Roadmap

以下是 mini-cc 后续计划支持的能力，逐步向完整版 Claude Code 靠拢：

1. **上下文压缩** — 当 messages 过长时自动 summarize（对应 CC 的 `services/compact/`）
2. **会话持久化** — 保存/恢复对话到 JSON 文件（对应 CC 的 `utils/sessionStorage.ts`）
3. **CLAUDE.md 记忆** — 启动时读取项目根目录的 CLAUDE.md（对应 CC 的 `context.ts → getUserContext`）
4. **流式工具执行** — 模型还在输出时就开始执行已完成的工具（对应 CC 的 `StreamingToolExecutor`）
5. **子 Agent** — 生成子进程处理独立任务（对应 CC 的 `tools/AgentTool/`）

---

## License

Educational project. Claude Code source is property of [Anthropic](https://www.anthropic.com).
