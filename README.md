# mini_code — 一个教学级 Agent 框架

从零构建的 LLM Agent 运行时。实现了工具分发、权限管控、上下文压缩、错误恢复、子代理、后台任务、Cron 调度、多代理协作、Git Worktree 隔离、MCP 外部工具接入等全部机制。

> 机制很多，循环一个。

---

## 目录

- [项目结构](#项目结构)
- [技术架构](#技术架构)
- [核心循环](#核心循环)
- [数据流](#数据流)
- [模块详解](#模块详解)
- [技术亮点](#技术亮点)
- [快速开始](#快速开始)
- [配置](#配置)

---

## 项目结构

```
mini_code/
├── agent/                  # 主程序
│   └── loop.py             #   Agent 主循环 + CLI 入口
│
├── tools/                  # 工具系统
│   ├── definitions.py      #   27 个内置工具 Schema 定义
│   ├── handlers.py         #   工具执行实现 (bash/read/write/edit/glob)
│   └── dispatch.py         #   工具池组装 + Subagent + Handler 映射
│
├── hooks/                  # 事件钩子系统
│   └── hooks.py            #   权限检查 / 日志 / 大输出告警 / Stop 统计
│
├── compact/                # 上下文压缩管线
│   └── compaction.py       #   4 层压缩: budget→snip→micro→summarize
│
├── recovery/               # 错误恢复
│   └── recovery.py         #   429/529 退避重试 / max_tokens 升级 / fallback model
│
├── cron/                   # 定时调度
│   └── scheduler.py        #   5 字段 Cron / durable 持久化 / 后台轮询注入
│
├── background/             # 后台任务
│   └── background.py       #   慢操作后台执行 / task_notification 通知
│
├── skills/                 # 技能系统
│   └── skill_loader.py     #   Markdown + YAML frontmatter 扫描加载
│
├── context/                # Prompt 组装
│   └── context.py          #   每轮动态拼接 system prompt
│
├── tasks/                  # 任务系统 (持久化)
│   └── task_system.py      #   文件级 Task CRUD / 依赖管理
│
├── worktree/               # Git Worktree 隔离
│   └── worktree.py         #   创建/删除/保留独立分支工作目录
│
├── team/                   # 多代理协作
│   ├── message_bus.py      #   JSONL 邮箱消息总线
│   ├── protocol.py         #   Plan 审批 / Shutdown 协议
│   ├── teammate.py         #   持久队友线程 (plan-gate + worktree-aware)
│   └── autonomous.py       #   空闲轮询 + 自动认领任务
│
├── mcp/                    # MCP 协议客户端
│   ├── base.py             #   抽象基类
│   ├── stdio_client.py     #   本地子进程 JSON-RPC 传输
│   ├── http_client.py      #   远程 HTTP + SSE 传输
│   └── config.py           #   .mcp.json 加载 / connect / disconnect / 工具池合并
│
└── shared/                 # 共享配置
    └── config.py           #   路径、常量、safe_path、normalize_mcp_name
```

---

## 技术架构

```
                          ┌──────────────────────────────────────┐
                          │           用户输入 / Cron 触发        │
                          └──────────────┬───────────────────────┘
                                         │
                                         ▼
                          ┌──────────────────────────────────────┐
                          │     UserPromptSubmit Hooks            │
                          └──────────────┬───────────────────────┘
                                         │
              ┌──────────────────────────┼──────────────────────────┐
              │                          ▼                          │
              │         ┌──────────────────────────────┐            │
              │         │    上下文压缩管线              │            │
              │         │  tool_result_budget           │            │
              │         │      → snip_compact           │            │
              │         │      → micro_compact          │            │
              │         │      → compact_history        │            │
              │         └──────────────┬───────────────┘            │
              │                        │                            │
              │                        ▼                            │
              │         ┌──────────────────────────────┐            │
              │         │  System Prompt 组装            │            │
              │         │  memory + skills + MCP state  │            │
              │         └──────────────┬───────────────┘            │
              │                        │                            │
              │                        ▼                            │
              │         ┌──────────────────────────────┐            │
              │         │      LLM 调用                  │            │
              │         │  429 → 退避重试               │            │
              │         │  529 → 退避 + 切 fallback     │            │
              │         │  max_tokens → 升级重试        │            │
              │         │  prompt too long → reactive    │            │
              │         └──────────────┬───────────────┘            │
              │                        │                            │
              │          ┌─────────────┴─────────────┐              │
              │          ▼                           ▼              │
              │   has tool_use?                无 tool_use          │
              │          │                           │              │
              │          ▼                           ▼              │
              │   PreToolUse Hooks              Stop Hooks          │
              │   (权限 / 日志)                  (统计 / 清理)       │
              │          │                           │              │
              │          ▼                           ▼              │
              │   ┌─────────────┐              返回给用户            │
              │   │  工具分发    │                                   │
              │   │             │                                   │
              │   │ Builtin     │                                   │
              │   │ MCP (stdio) │                                   │
              │   │ MCP (http)  │                                   │
              │   │ Background  │                                   │
              │   └──────┬──────┘                                   │
              │          │                                          │
              │          ▼                                          │
              │   PostToolUse Hooks                                 │
              │          │                                          │
              │          ▼                                          │
              │   tool_result → messages                            │
              │          │                                          │
              └──────────┼──────────────────────────────────────────┘
                         │
                         ▼
                    下一轮循环
```

---

## 核心循环

Agent 的核心始终没变——**一个 while True**：

```python
while True:
    response = LLM(messages, tools)

    if not has_tool_use(response.content):
        return                              # 没有工具调用 → 结束

    for block in response.content:
        if block.type == "tool_use":
            result = execute_tool(block)     # 执行工具
            messages.append(tool_result)     # 结果追加回上下文

    # 下一轮: LLM 看到结果, 决定继续或结束
```

变化的是循环**周围**的 harness——压缩、权限、后台、Cron、MCP、多代理都在循环的不同位置挂载。

### 组件在循环中的位置

| 位置 | 组件 | 职责 |
|------|------|------|
| 用户输入前 | `UserPromptSubmit` hooks | 记录/审计用户输入 |
| LLM 前 | Cron queue | 定时触发的 prompt 注入 `messages` |
| LLM 前 | Background notifications | 后台任务完成后的 `<task_notification>` 注入 |
| LLM 前 | **Compaction pipeline** | 4 层压缩: budget → snip → micro → summarize |
| LLM 前 | **Prompt assembly** | memory + skills + MCP state → system prompt |
| LLM 调用 | **Error recovery** | 429 退避 / 529 切模型 / max_tokens 升级 / reactive compact |
| 工具执行前 | **PreToolUse hooks** | 权限拦截、日志记录 |
| 工具分发 | **assemble_tool_pool()** | 内置工具 + MCP 工具合并 |
| 工具执行时 | **Background dispatch** | 慢操作后台线程，主循环不阻塞 |
| 工具执行后 | **PostToolUse hooks** | 大输出告警等后处理 |
| 返回循环 | **tool_result** | 每个 `tool_use` 对应一个 `tool_result`，进入下一轮 |
| 本轮无调用 | **Stop hooks** | 统计、清理 |

---

## 数据流

### 1. 单轮对话流

```
                 ┌─────────────────┐
                 │   messages[]    │  ← 累积的消息历史
                 └────────┬────────┘
                          │
          ┌───────────────┼───────────────┐
          │               ▼               │
          │  ┌──────────────────────┐      │
          │  │ Cron 注入             │      │
          │  │ Background 通知       │      │
          │  │ Todo 提醒             │      │
          │  └──────────┬───────────┘      │
          │             ▼                  │
          │  ┌──────────────────────┐      │
          │  │ prepare_context()    │      │
          │  │  - tool_result_budget│      │
          │  │  - snip_compact      │      │
          │  │  - micro_compact     │      │
          │  │  - compact_history   │      │
          │  └──────────┬───────────┘      │
          │             ▼                  │
          │  ┌──────────────────────┐      │
          │  │ assemble_system_     │      │
          │  │ prompt(context)      │      │
          │  └──────────┬───────────┘      │
          │             ▼                  │
          │  ┌──────────────────────┐      │
          │  │ assemble_tool_pool() │      │
          │  │ Builtin + MCP tools  │      │
          │  └──────────┬───────────┘      │
          │             ▼                  │
          │  ┌──────────────────────────┐  │
          │  │  client.messages.create  │  │
          │  │  with_retry (429/529)    │  │
          │  └──────────┬───────────────┘  │
          │             ▼                  │
          │   has_tool_use ?               │
          │      │          │              │
          │    yes          no             │
          │      │          │              │
          │      ▼          ▼              │
          │  execute      Stop hooks       │
          │  tools         return          │
          │      │                         │
          │      ▼                         │
          │  tool_result → messages        │
          │      │                         │
          └──────┼─────────────────────────┘
                 │
                 ▼
            下一轮循环
```

### 2. MCP 工具发现流

```
connect_mcp("github")
    │
    ▼
mcp/config.py
    │
    ├─ 读 .mcp.json → {"github": {type:"stdio", command:"npx", ...}}
    │
    ├─ type=stdio → StdioMCPClient
    │     └─ subprocess.Popen("npx", ["-y", "@anthropic/mcp-server-github"])
    │     └─ initialize → tools/list → 发现 5 个工具
    │
    ├─ type=http → HttpMCPClient
    │     └─ GET /sse → 拿 session 端点
    │     └─ POST initialize → POST tools/list → 发现工具
    │
    └─ 存入 mcp_clients["github"]
    │
    ▼
下一轮 agent_loop()
    │
    assemble_tool_pool()
    │
    ├─ 遍历 mcp_clients
    ├─ 每个工具加前缀: mcp__github__search_repos
    ├─ handler 闭包: client.call_tool("search_repos", args)
    │
    ▼
模型看到新工具 → 调用 mcp__github__search_repos(query="...")
    │
    ▼
PreToolUse hook → 权限检查
    │
    ▼
handler(query="...") → github_client.call_tool("search_repos", ...)
    │
    ├─ stdio: 写 stdin → 等 stdout 响应
    └─ http:  POST → 等 SSE 流响应
    │
    ▼
tool_result → messages → 下一轮 LLM 看到结果
```

### 3. 多代理协作流

```
Lead Agent
    │
    ├─ create_task("review PR", blockedBy=[])
    ├─ create_worktree("wt-review", task_id)
    │
    ├─ spawn_teammate("alice", "reviewer", "review PRs in worktree")
    │       │
    │       ▼
    │   alice 线程启动
    │       │
    │       ├─ idle_poll: 检测到未认领任务
    │       ├─ claim_task("task_xxx") → 绑定 worktree
    │       ├─ submit_plan("我会先检查代码风格...")
    │       │       │
    │       │       ▼ (MessageBus → lead inbox)
    │       │
    │       ├─ (暂停等待 plan approval)
    │       │
    │   lead: check_inbox → 看到 plan_approval_request
    │   lead: review_plan(req_id, approve=true)
    │       │
    │       ▼ (MessageBus → alice inbox)
    │       │
    │       ├─ 收到 [Plan approved]
    │       ├─ 在 worktree 目录下执行 bash/read/write
    │       ├─ complete_task("task_xxx")
    │       └─ 发送结果摘要 → BUS.send(name, "lead", result)
    │
    ▼
lead: check_inbox → 看到 result，任务完成
```

### 4. 上下文压缩流

```
messages 列表 (可能很大)
    │
    ▼
tool_result_budget()          ← 单次工具结果超过 200KB → 落盘 + 预览
    │
    ▼
snip_compact()                ← 超过 50 条消息 → 保留头 3 + 尾 47
    │
    ▼
micro_compact()               ← 超过 3 个 tool_result → 旧结果压缩为占位符
    │
    ▼
estimate_size() > 50000?      ← JSON 序列化超 50KB?
    │
    yes → compact_history()    ← LLM 总结 → 一条压缩消息替代全部历史
    no  → 直接送入 LLM
```

---

## 模块详解

### agent/loop.py

主程序入口。包含完整的 Agent 循环、CLI 交互、子系统初始化。按顺序执行：

1. 加载 `.env` + Anthropic client
2. 扫描 skills 目录
3. 初始化 compaction / subagent / teammate 子系统
4. 注册 hooks
5. 启动 Cron 调度器 daemon
6. 进入 CLI 循环 → 每次用户输入触发 `agent_loop()`

### tools/ — 工具系统

**definitions.py**: 27 个内置工具的 JSON Schema 定义，涵盖文件操作、任务管理、Cron、团队、Worktree、MCP 连接。

**handlers.py**: 工具的实际 Python 实现。`run_bash` / `run_read` / `run_write` / `run_edit` / `run_glob`，全部通过 `safe_path()` 做路径沙箱。

**dispatch.py**: 每轮调用 `assemble_tool_pool()`，合并内置工具 + 已连接 MCP 工具。同时包含 subagent 的生成逻辑。

### hooks/ — 事件钩子

4 个 Hook 点，可插拔：

| Hook | 触发时机 | 默认行为 |
|------|---------|---------|
| `UserPromptSubmit` | 用户输入提交前 | 打印工作目录 |
| `PreToolUse` | 工具执行前 | 权限检查 (deny list / 路径越界 / MCP 危险操作) + 日志 |
| `PostToolUse` | 工具执行后 | 大输出告警 (>100KB) |
| `Stop` | 本轮无工具调用 | 统计 tool_result 数量 |

### compact/ — 上下文压缩

4 层压缩管线，递减执行：

```
tool_result_budget → snip_compact → micro_compact → compact_history
```

| 层级 | 条件 | 操作 |
|------|------|------|
| budget | 最近一次结果 >200KB | 落盘 + 保留前 2000 字符预览 |
| snip | 总消息 >50 条 | 保留头 3 + 尾 47 |
| micro | tool_result >3 个 | 旧结果 → `[Earlier tool result compacted]` |
| summarize | JSON 序列化 >50KB | LLM 总结 → 一条压缩消息 |

### recovery/ — 错误恢复

| 错误 | 策略 |
|------|------|
| 429 (Rate Limit) | 指数退避重试 (500ms~32s + jitter)，最多 3 次 |
| 529 (Overloaded) | 同 429，连续 2 次后切换 `FALLBACK_MODEL` |
| max_tokens | 首次升级到 16000 token 重试；再碰到追加 continuation prompt |
| prompt too long | Reactive compact 后重试 |

### cron/ — 定时调度

- 标准 5 字段 Cron (`min hour dom month dow`)
- 支持 `*/N`、逗号、范围
- 每秒 daemon 线程检查匹配
- `durable=true` 持久化到 `.scheduled_tasks.json`
- 命中后注入 `[Scheduled] prompt` 到 messages，触发新一轮 agent_loop

### background/ — 后台任务

- 启发式识别慢操作 (`install` / `build` / `test` / `deploy` / ...)
- 模型也可显式传 `run_in_background=true`
- 后台线程执行，主循环立即返回 placeholder
- 完成后 `<task_notification>` 注入 messages

### team/ — 多代理协作

| 文件 | 职责 |
|------|------|
| `message_bus.py` | JSONL 邮箱: `BUS.send()` 追加, `BUS.read_inbox()` 读取并清空 |
| `protocol.py` | Plan approval 协议 (submit→pending→approve/reject) + Shutdown 协议 |
| `teammate.py` | 持久队友线程: 独立 agent loop，支持 plan-gate、worktree 绑定、空闲自动认领 |
| `autonomous.py` | 空闲轮询: 每 5s 检查 inbox，没有消息则扫描未认领任务 |

### mcp/ — MCP 协议客户端

| 文件 | 职责 |
|------|------|
| `base.py` | `BaseMCPClient` 抽象类 |
| `stdio_client.py` | 本地子进程 JSON-RPC 传输 |
| `http_client.py` | 远程 HTTP POST + SSE 流传输 |
| `config.py` | `.mcp.json` 加载 / `connect_mcp` / `disconnect_mcp` / `assemble_tool_pool` |

---

## 技术亮点

### 1. "机制很多，循环一个"

所有复杂机制挂在一个 `while True` 上。没有多线程调度器，没有事件总线——每个组件只是在循环的特定位置被调用。

### 2. Hook 驱动权限

权限不是硬编码在工具里，而是作为 PreToolUse hook 注入。加新规则只需 `register_hook("PreToolUse", my_checker)`，不需要改动任何工具代码。

### 3. 4 层渐进压缩

不是一刀切地 LLM 总结。先 budget（截断单次大结果）、再 snip（裁消息数量）、再 micro（压旧 tool_result）、最后才 LLM summarize。只有在前面几层不够用时才触发最昂贵的 summarize。

### 4. Subagent 上下文隔离

一次性 subagent 有独立 `messages[]`，中间过程不污染主上下文，只返回最终摘要。适合"去把这个文件重构了"这种独立任务。

### 5. MCP 双传输

Stdio（本机）和 HTTP（远程）共用一个 `BaseMCPClient` 接口。工具池每轮重新组装，MCP 工具和内置工具对模型来说完全一致。

### 6. 工作树隔离

每个 task 可绑定独立 git worktree。队友认领任务后，所有文件操作自动在对应隔离目录下执行——bash / read / write 透明重定向。

### 7. Plan-Gate 安全模式

队友提交 plan 后进入等待状态，不再执行任何工具，直到 lead 批准。防止自主代理在无人审核的情况下执行破坏性操作。

### 8. 纯标准库 MCP HTTP

`HttpMCPClient` 只用 `urllib`（Python 标准库），零额外依赖即可连接远程 MCP 服务器。

---

## 快速开始

### 环境要求

- Python 3.10+
- Anthropic API Key

### 安装

```bash
pip install anthropic python-dotenv pyyaml
```

### 配置

创建 `.env` 文件：

```env
ANTHROPIC_API_KEY=sk-ant-xxx
MODEL_ID=claude-sonnet-4-6-20250514
ANTHROPIC_BASE_URL=https://api.anthropic.com   # 可选，默认这个
FALLBACK_MODEL_ID=claude-haiku-4-5-20251001    # 可选，529 时切换
```

创建 `.mcp.json`（可选，接入外部 MCP 服务器）：

```json
{
  "mcpServers": {
    "filesystem": {
      "type": "stdio",
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-filesystem", "."]
    }
  }
}
```

### 运行

```bash
python agent/loop.py
```

```
s20: comprehensive agent
Enter a question, press Enter to send. Type q to quit.

s20 >> 帮我创建一个 todolist，然后列出所有 Python 文件
s20 >> connect_mcp filesystem
s20 >> 用 MCP 工具读 README.md
```

### 试玩场景

1. **Todo + 文件操作**: `Create a todolist for inspecting this repo, then list Python files`
2. **Task + Worktree + 团队**: `Create two tasks with worktrees, spawn alice and bob. Ask them to submit plans before claiming.`
3. **MCP**: 先配好 `.mcp.json`，然后 `Connect to filesystem MCP and read README.md`
4. **Cron**: `Remind me of the meeting in 3 minutes`（会自动计算 cron 表达式）
5. **后台任务**: `Run npm install in the background and continue working`

---

## 配置

### 环境变量 (`.env`)

| 变量 | 必填 | 说明 |
|------|------|------|
| `ANTHROPIC_API_KEY` | 是 | API 密钥 |
| `MODEL_ID` | 否 | 模型 ID，默认 `claude-sonnet-4-6` |
| `ANTHROPIC_BASE_URL` | 否 | API 地址，默认官方 |
| `FALLBACK_MODEL_ID` | 否 | 529 超载时切换的备用模型 |

### MCP 配置 (`.mcp.json`)

```json
{
  "mcpServers": {
    "<name>": {
      "type": "stdio",
      "command": "可执行命令",
      "args": ["参数1", "参数2"],
      "env": { "KEY": "value" }
    },
    "<name>": {
      "type": "http",
      "url": "https://mcp-server.example.com",
      "headers": { "Authorization": "Bearer xxx" },
      "connect_timeout": 30
    }
  }
}
```

### Skills

在 `skills/` 目录下创建子目录，每个子目录放一个 `SKILL.md`：

```
skills/
└── my-skill/
    └── SKILL.md    ← YAML frontmatter + Markdown 正文
```

```markdown
---
name: my-skill
description: 我的自定义技能
---
# 技能内容...
```

Agent 启动时自动扫描。模型调用 `load_skill("my-skill")` 获取完整内容。

### Memory

在 `.memory/MEMORY.md` 中写入长期记忆，每轮 system prompt 会自动注入前 2000 字符。

---

## License

MIT
