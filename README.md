# MiniClaudeCode

面向代码开发与维护场景的 Python 命令行编程助手，将自然语言需求转化为代码检索、文件修改和命令执行，并通过上下文管理与多 Agent 协作推进复杂任务。

## 核心功能

- **Agent 执行闭环**：模型规划、工具调用、结果回传与迭代修正；支持文件读写、编辑、路径检索、命令执行和步骤清单。
- **Skills 与 Hooks**：启动时扫描技能目录，按需向模型加载正文；通过输入、工具调用前后和停止事件扩展权限检查与日志行为。
- **上下文与长期记忆**：分层处理大工具输出、历史裁剪、结果压缩和模型摘要；保存记忆索引，按当前任务选择并注入相关信息。
- **多 Agent 任务编排**：支持独立上下文的子任务与持久队友、任务依赖、原子认领、消息通信和版本绑定的计划审批；可为任务绑定 Git worktree。
- **后台执行与定时任务**：命令可在后台运行，完成通知回到主循环；支持五字段 Cron、任务定义持久化与待交付状态恢复。
- **动态工具扩展原型**：使用进程内模拟 MCP 服务演示工具发现、动态工具池与宿主权限策略。

## 工作流程

```text
用户输入 / 定时触发 / 团队事件
              ↓
        Lead Agent 主循环
              ↓
   记忆选择 → 上下文整理 → 模型请求
                            ↓
       ┌────────────────────┼───────────────────┐
       ↓                    ↓                   ↓
   文件与命令工具      子 Agent / 持久队友     技能与扩展工具
       └────────────────────┼───────────────────┘
                            ↓
                    工具结果 / 协作消息
                            ↓
                  继续决策或输出最终响应
```

使用 `httpx` 直接调用 OpenAI Chat Completions 兼容接口，按 `tool_call_id` 配对工具结果，并保留模型返回的 `reasoning_content`。

## 快速开始

需要 **Python 3.11+**、**uv**，以及支持工具调用的模型服务。使用 worktree 时还需要 Git。以下命令在项目根目录执行，示例终端为 Windows PowerShell。

```powershell
uv sync
Copy-Item .env.example .env
```

编辑 `.env`，填写自己的服务配置。下面以 DeepSeek 的接口地址为例；模型名称以服务商实际提供的 ID 为准。

```dotenv
LLM_API_KEY=your-api-key
LLM_BASE_URL=https://api.deepseek.com
MODEL_ID=your-model-id
LLM_THINKING=
```

- `LLM_BASE_URL` 填接口基础地址，不包含 `/chat/completions`。
- `LLM_THINKING` 默认留空；仅在服务支持时填写其要求的值。
- `.env` 中的配置优先于同名环境变量；密钥只保存在本地。

创建工作目录并启动：

```powershell
mkdir sandbox
uv run minicc --workdir sandbox
```

也可以使用 `uv run python -m minicc --workdir sandbox`。`--workdir` 必须指向已存在的目录；Windows 命令使用 PowerShell，其他平台使用系统 shell。

## 使用示例

启动后直接输入任务，例如：

```text
读取当前目录的 Python 文件，解释入口与模块关系。
在当前目录创建一个 JSON 校验脚本，运行示例并检查结果。
把代码检查和文档检查拆成两个任务，先给出团队分工方案。
压缩当前对话，重点保留未完成事项和已经确认的约束。
```

团队协作由 Lead 提出分工，确认后启动队友。任务依赖满足后，空闲队友可以自动认领任务。使用 worktree 时，工作目录须为**已有提交的 Git 仓库根目录**，各任务的修改需要自行审阅和合并。

自定义技能放在 `<工作目录>/skills/<技能名>/SKILL.md`，通过 YAML frontmatter 提供 `name` 和 `description`，正文编写执行指引。启动时扫描并缓存技能；修改后重启生效。

任务、记忆、工具输出和定时记录按需保存在所选工作目录的 `.tasks/`、`.memory/`、`.task_outputs/`、`.transcripts/` 等位置。输入 `q`、`exit` 或空行退出。

## 代码结构

| 模块 | 职责 |
| --- | --- |
| `__main__.py`、`runtime.py` | 配置、终端交互、定时与团队事件入口 |
| `agent.py`、`model.py` | Lead 主循环、模型请求与工具协议适配 |
| `tools.py`、`permissions.py`、`hooks.py` | 文件与命令工具、权限策略、生命周期事件 |
| `skills.py`、`compact.py`、`memory.py` | 技能加载、上下文压缩与长期记忆 |
| `subagent.py`、`todo.py`、`tasks.py` | 一次性子任务、步骤清单、持久任务依赖与认领 |
| `teams.py`、`teammate.py`、`team_state.py` | 团队编排、队友循环、计划审批与共享状态 |
| `mailbox.py`、`worktrees.py` | 文件消息通信与任务工作目录管理 |
| `background.py`、`cron.py`、`mcp.py` | 后台命令、定时调度与模拟动态工具扩展 |

上述模块均位于 `minicc/`。

## 当前边界

- **执行权限**：文件工具限制在工作目录内，命令工具以当前用户权限运行；命令拒绝列表与 Git worktree 均不构成操作系统沙箱。
- **上下文管理**：压缩预算按字符估算，尚未使用精确 tokenizer；子 Agent 与持久队友尚未接入压缩。记忆选择、提取和整理会产生额外模型请求。
- **团队恢复**：任务记录可以持久化，队友历史与审批状态不支持重启恢复；worktree 不自动合并代码。
- **后台与定时**：依赖 CLI 宿主进程持续运行。后台命令完成后，在下一次主模型请求前收集结果，不会单独唤醒 Agent；定时任务不补跑停机期间错过的时间点，交付重试可能重复执行。
- **MCP 原型**：`docs` / `deploy` 仅返回进程内模拟结果，未实现真实 stdio / HTTP transport、JSON-RPC 握手或外部服务连接。

第三方来源与许可说明见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。
