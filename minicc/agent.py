"""父 Agent：处理用户问题，反复请求模型、执行工具，直到得到本回合的回复。"""

from copy import deepcopy
from functools import partial
import json
from pathlib import Path

import httpx

from . import background, compact, cron, mcp, memory, tasks, teams, tools
from . import team_state
from .hooks import trigger_hooks
from .model import request_model
from .subagent import TASK_TOOL, spawn_subagent
from .skills import LOAD_SKILL_TOOL, load_skill


# --- s14 修改：内置工具加入 MCP 连接入口，外部工具每轮动态组装 ---
# Lead 常驻的二十六个工具定义；连接后发现的 MCP 工具另行加入当轮请求。
TOOLS = [
    *tools.TOOLS, TASK_TOOL, LOAD_SKILL_TOOL, compact.COMPACT_TOOL,
    *tasks.TOOLS, *cron.TOOLS, *teams.TEAM_TOOLS, mcp.CONNECT_TOOL,
]
# 工具名称 -> 执行函数；每个回合会复制此表并绑定该回合的客户端配置。
TOOL_HANDLERS = {
    **tools.TOOL_HANDLERS, "task": spawn_subagent, "load_skill": load_skill,
    **tasks.TOOL_HANDLERS, **cron.TOOL_HANDLERS,
    "spawn_teammate": teams.run_spawn_teammate, "list_teammates": teams.run_list_teammates,
    "send_message": teams.run_send_message, "request_shutdown": teams.run_request_shutdown,
    "request_plan": teams.run_request_plan, "review_plan": teams.run_review_plan,
    "create_worktree": teams.run_create_worktree,
    "connect_mcp": mcp.run_connect_mcp,
}
# 单次主模型请求因上下文超长而进行补救压缩的最大重试次数。
MAX_REACTIVE_RETRIES = 1

# 累计工具调用轮数；维护计划或发出计划提醒后归零。
rounds_since_todo = 0


# 每个回合开始时交付到期 prompt，接收失败时恢复队列和历史
def agent_loop(
    client: httpx.Client, model: str, messages: list[dict],
    workdir: Path, thinking: str = "",
) -> None:
    """处理用户输入和已到期任务；模型首次接收成功后确认定时任务的交付。"""
    # 1. 确认本回合主工作目录，设置定时任务存储位置。
    if team_state.active_teammates and tools.WORKDIR.resolve() != workdir.resolve():
        raise ValueError("Cannot change workspace while teammates are active")
    tools.WORKDIR = workdir.resolve()
    cron.DURABLE_PATH = tools.safe_path(".scheduled_tasks.json")
    # 2. waiting_for_ack 是本次取出的到期 CronJob 列表，由内层首次主模型回复后清空。
    waiting_for_ack = cron.consume_cron_queue()
    # original_history 保存加入定时 prompt 前的历史；首次模型接收失败时恢复这份内容。
    original_history = deepcopy(messages) if waiting_for_ack else None
    # received_notifications 单独记录首次确认前新收集的后台通知，恢复历史时继续保留。
    received_notifications = []
    # 3. 将每项到期任务转换为 [Scheduled] 用户消息，交给内层模型循环处理。
    for job in waiting_for_ack:
        messages.append({"role": "user", "content": f"[Scheduled] {job.prompt}"})
        print(f"  [cron] delivered {job.id}: {job.prompt[:60]}")
    try:
        _run_agent_loop(client, model, messages, workdir, thinking, waiting_for_ack, received_notifications)
    # 4. 仍有未确认任务时，恢复历史并把任务重新放回待交付队列。
    except BaseException:
        if waiting_for_ack:
            # 恢复本回合交付前的对话，并保留刚收集的后台命令通知。
            messages[:] = original_history + received_notifications
            cron.restore_cron_jobs(waiting_for_ack)
        raise
    finally:
        # Lead 回合结束后释放已完成任务的工作目录
        tasks.release_completed_assignment("agent")


def _run_agent_loop(client, model, messages, workdir, thinking, waiting_for_ack, received_notifications):
    """持续请求模型并执行工具，更新历史，在回合结束时提取和整理记忆。

    waiting_for_ack 保存本回合尚未确认的定时任务，首次主模型回复后清空。
    received_notifications 收集确认前加入的后台通知，供失败时恢复历史。
    """
    # 准备阶段：设置共享存储、记忆提示与本回合工具处理表。
    global rounds_since_todo
    workdir = workdir.resolve()
    tools.WORKDIR = workdir
    # 设置本次工作目录对应的持久任务存储
    tasks.TASKS_DIR = workdir / ".tasks"
    tasks.TASKS = tasks.TaskStore(tasks.TASKS_DIR)
    # 加载本回合相关的长期记忆，记录当前用户消息。
    memory.MEMORY_DIR = tools.safe_path(".memory")
    # memories_content 是本回合选中的记忆全文；memory_turn 指向用于注入记忆的原用户消息。
    memories_content = memory.load_memories(messages, client, model, thinking)
    memory_turn = messages[-1] if (messages and messages[-1].get("role") == "user"
                                   and isinstance(messages[-1].get("content"), str)) else None
    # 在系统提示词中加入任务创建与依赖设置的使用顺序
    system = memory.build_system() + "\n" + tasks.TASK_SYSTEM_PROMPT
    # 提示模型将独立命令显式交给后台执行
    system += "\n" + background.BACKGROUND_SYSTEM_PROMPT
    # 提示模型使用本地时间安排未来要执行的 prompt
    system += "\n" + cron.CRON_SYSTEM_PROMPT
    # 团队确认、任务分工、消息协作与计划审批说明
    system += "\n" + teams.TEAM_SYSTEM_PROMPT
    system += "\nIf a task needs a worktree, create_worktree while it is pending and unowned, before spawn_teammate or claim_task."
    # 保存本回合的未压缩对话，供结束时提取长期记忆。
    # pre_compress 是记忆提取使用的未压缩副本，会继续追加本回合的模型消息和工具结果。
    pre_compress = deepcopy(messages)

    # 为子 Agent 准备同一个模型客户端、工作目录和思考模式。
    # --- s14 修改：保留已绑定配置的内置处理表，每轮再合并 MCP 工具 ---
    # builtin_handlers 是本回合内置工具的处理表；task 与 spawn_teammate 绑定当前配置。
    builtin_handlers = dict(TOOL_HANDLERS)
    builtin_handlers["task"] = partial(
        builtin_handlers["task"], client=client, model=model, workdir=workdir, thinking=thinking,
    )
    # 给队友启动函数绑定当前客户端与模型配置
    builtin_handlers["spawn_teammate"] = partial(
        builtin_handlers["spawn_teammate"], client=client, model=model, thinking=thinking,
    )

    # 记录本次模型请求因上下文过长而重试的次数。
    reactive_retries = 0
    while True:
        # 1. 处理过大的工具输出、裁剪旧消息、缩短旧结果；仍超限时生成摘要。
        messages[:] = compact.tool_result_budget(messages)
        messages[:] = compact.snip_compact(messages)
        messages[:] = compact.micro_compact(messages)
        if compact.estimate_size(messages) > compact.CONTEXT_LIMIT:
            print("[auto compact]")
            messages[:] = compact.compact_history(messages, client, model, thinking)

        # 连续三轮未维护计划时，追加一次 TODO 更新提醒。
        if rounds_since_todo >= 3 and messages:
            messages.append({"role": "user", "content": "<reminder>Update your todos.</reminder>"})
            rounds_since_todo = 0

        # 每次主模型请求前，收集已经完成的后台命令通知
        notification_count = background.inject_background_results(messages)
        if waiting_for_ack and notification_count:
            received_notifications.append(deepcopy(messages[-1]))
        try:
            # --- s14 新增：读取最新连接，生成本次请求的工具池和系统提示词 ---
            # tool_definitions 发给模型，handlers 按相同名称分发这次回复中的调用。
            tool_definitions, handlers = mcp.assemble_tool_pool(TOOLS, builtin_handlers)
            request_system = mcp.assemble_system_prompt(system)
            # 2. 在本次请求中加入相关记忆，取得模型的下一条回复。
            # request_messages 是实际发送给模型的消息列表，其中临时加入了相关记忆正文。
            request_messages = memory.inject_memories(messages, memories_content, memory_turn)
            message = request_model(client, model, request_system, request_messages, tool_definitions, thinking)
            reactive_retries = 0
        except Exception as error:
            # 上下文过长时重新压缩并重试一次；其他错误交给调用方处理。
            if compact.is_context_error(error) and reactive_retries < MAX_REACTIVE_RETRIES:
                print("[reactive compact]")
                messages[:] = compact.reactive_compact(messages, client, model, thinking)
                reactive_retries += 1
                continue
            raise
        messages.append(message)
        # 主模型接收成功后，确认本批定时任务已交付
        if waiting_for_ack:
            try:
                cron.acknowledge_cron_jobs(waiting_for_ack)
            except Exception as error:
                print(f"  [cron] acknowledgement failed: {error}")
            waiting_for_ack.clear()
        pre_compress.append(deepcopy(message))
        # 3. 检查模型是否请求工具；没有工具请求时，检查 Stop 钩子是否要求继续。
        tool_calls = message.get("tool_calls") or []
        if not tool_calls:
            force = trigger_hooks("Stop", messages)
            if force is not None:
                messages.append({"role": "user", "content": force})
                continue
            # 本回合结束：提取新记忆，整理记忆文件，然后返回终端入口。
            memory.extract_memories(pre_compress, client, model, thinking)
            memory.consolidate_memories(client, model, thinking)
            return

        # 4. 依次执行本批工具，并收集每次调用对应的结果。
        rounds_since_todo += 1
        # results 收集当前模型回复中各个工具调用的结果，之后一起写入历史。
        results = []
        # compact_requested 标记本批是否要求压缩；compact_focus 保存模型指定的摘要重点。
        compact_requested, compact_focus = False, ""
        for call in tool_calls:
            name = call["function"]["name"]
            # arguments 是工具参数字典；call 的 id 会成为对应结果消息的 tool_call_id。
            # --- s14 修改：兼容接口的参数解析错误也作为配对的工具结果回传 ---
            try:
                arguments = json.loads(call["function"]["arguments"])
                if not isinstance(arguments, dict):
                    raise ValueError("Tool arguments must be a JSON object")
            except (TypeError, ValueError) as error:
                results.append({
                    "role": "tool", "tool_call_id": call["id"],
                    "content": f"Error: {type(error).__name__}: {error}",
                })
                continue
            # 工具执行前触发钩子；被阻止的调用直接返回阻止说明。
            blocked = trigger_hooks("PreToolUse", name, arguments)
            if blocked is not None:
                results.append({"role": "tool", "tool_call_id": call["id"], "content": str(blocked)})
                continue
            # 通过执行前钩子的后台命令先返回启动结果
            if background.should_run_background(name, arguments):
                try:
                    task_id = background.start_background_task(name, arguments, call["id"], tasks.assignment_cwd("agent"))
                    output = (
                        f"[Background task {task_id} started] "
                        "The result will be collected on a later turn."
                    )
                except Exception as error:
                    output = f"Error: {error}"
            elif name == "compact":
                # compact 先记录压缩请求，实际压缩在本批工具全部返回后进行。
                compact_requested = True
                compact_focus = arguments.get("focus", "")
                output = "Compaction requested. History will be summarized after this tool batch."
            else:
                handler = handlers.get(name)
                # 执行工具，将正常输出或错误说明作为调用结果
                try:
                    # 基础工具使用 Lead 当前任务的工作目录
                    # call_arguments 是最终传给执行函数的参数；有任务目录分配时会补入该任务的 cwd。
                    call_arguments = arguments
                    if name in {"bash", "read_file", "write_file", "edit_file", "glob"}:
                        cwd = tasks.assignment_cwd("agent")
                        if "agent" in team_state.teammate_assignments:
                            call_arguments = {**arguments, "cwd": cwd}
                    output = handler(**call_arguments) if handler else f"Unknown: {name}"
                except Exception as error:
                    output = f"Error: {error}"
                output = str(output)
            trigger_hooks("PostToolUse", name, arguments, output)
            # 使用 TODO 或持久任务工具后，重置计划提醒计数
            if name == "todo_write" or name in tasks.TOOL_HANDLERS:
                rounds_since_todo = 0
            results.append({"role": "tool", "tool_call_id": call["id"], "content": output})
        # 5. 把工具结果写入历史；下一轮循环将这些结果发送给模型。
        messages.extend(results)
        pre_compress.extend(deepcopy(results))
        # 本批工具结果写入后，若收到 compact 请求，则立即以摘要替换当前历史。
        if compact_requested:
            messages[:] = compact.compact_history(messages, client, model, thinking, compact_focus)
