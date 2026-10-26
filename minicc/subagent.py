"""子 Agent：使用独立对话完成委派任务，向父 Agent 返回处理结果。"""

import json
from pathlib import Path

import httpx

from . import tools
from .hooks import trigger_hooks
from .model import request_model
from .tools import ENVIRONMENT_PROMPT, run_bash, run_read, run_write, run_edit, run_glob


# 子 Agent 的任务说明与五个基础工具。
SUB_SYSTEM = (
    "You are a coding agent at {workdir}. "
    f"Environment: {ENVIRONMENT_PROMPT}. "
    "Complete the task you were given, then return a concise summary. "
    "Do not delegate further."
)

# 发送给传统子 Agent 的五个基础工具定义，不包含进一步委派或团队管理工具。
SUB_TOOLS = [
    {"name": "bash", "description": "Run a shell command.",
     "input_schema": {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]}},
    {"name": "read_file", "description": "Read file contents.",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}},
    {"name": "write_file", "description": "Write content to a file.",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}}, "required": ["path", "content"]}},
    {"name": "edit_file", "description": "Replace exact text in a file once.",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "old_text": {"type": "string"}, "new_text": {"type": "string"}}, "required": ["path", "old_text", "new_text"]}},
    {"name": "glob", "description": "Find files matching a glob pattern.",
     "input_schema": {"type": "object", "properties": {"pattern": {"type": "string"}}, "required": ["pattern"]}},
]

# 子 Agent 工具名称 -> 执行函数的对应表。
SUB_HANDLERS = {
    "bash": run_bash, "read_file": run_read, "write_file": run_write,
    "edit_file": run_edit, "glob": run_glob,
}


def extract_text(content) -> str:
    """取得消息中的普通文字；非字符串内容返回空字符串。"""
    return content if isinstance(content, str) else ""


def spawn_subagent(
    description: str, client: httpx.Client, model: str,
    workdir: Path, thinking: str = "",
) -> str:
    """根据 description 执行子任务，最多请求模型 30 次，返回最近的模型文字。"""
    # 1. 用委派描述建立独立对话，设置与父 Agent 相同的工作目录。
    print("\n\033[35m[Subagent spawned]\033[0m")
    workdir = workdir.resolve()
    tools.WORKDIR = workdir
    # messages 是本次子任务的新历史，只从委派描述开始；父对话不复制进来。
    messages = [{"role": "user", "content": description}]
    # finished 表示是否因模型不再请求工具而正常结束，区别于用满 30 轮。
    finished = False

    for _ in range(30):
        # 2. 请求模型；模型没有提出工具调用时，结束子循环。
        message = request_model(
            client, model, SUB_SYSTEM.format(workdir=workdir),
            messages, SUB_TOOLS, thinking,
        )
        messages.append(message)
        tool_calls = message.get("tool_calls") or []
        if not tool_calls:
            finished = True
            break

        # 3. 依次执行模型请求的工具，并收集返回结果。
        # results 保存当前模型回复所请求的工具结果，随后追加到子任务历史。
        results = []
        for call in tool_calls:
            name = call["function"]["name"]
            arguments = json.loads(call["function"]["arguments"])
            # 执行前检查权限和其他钩子；被阻止时返回阻止说明。
            blocked = trigger_hooks("PreToolUse", name, arguments)
            if blocked is not None:
                results.append({"role": "tool", "tool_call_id": call["id"], "content": str(blocked)})
                continue
            handler = SUB_HANDLERS.get(name)
            output = handler(**arguments) if handler else f"Unknown: {name}"
            trigger_hooks("PostToolUse", name, arguments, output)
            print(f"  \033[90m[sub] {name}: {str(output)[:100]}\033[0m")
            results.append({"role": "tool", "tool_call_id": call["id"], "content": output})
        # 4. 将工具结果加入子对话，下一轮继续请求模型。
        messages.extend(results)

    # 5. 从最后一条消息向前查找最近的 assistant 文字，作为子任务返回值。
    # result 是最终交回父 Agent 的文字；从最新消息向前查找可用的 assistant 内容。
    result = ""
    for message in reversed(messages):
        if message["role"] == "assistant":
            result = extract_text(message.get("content"))
            if result:
                break
    if not result:
        # 没有可用文字时，按正常结束或达到轮数上限返回相应说明。
        result = "(no summary)" if finished else "Subagent stopped after 30 turns without final answer."
    print("\033[35m[Subagent done]\033[0m")
    return result


# 父 Agent 通过 task 工具提交子任务描述，收到子 Agent 返回的文字。
TASK_TOOL = {
    "name": "task",
    "description": "Launch a subagent to handle a complex subtask. Returns only the final conclusion.",
    "input_schema": {"type": "object", "properties": {"description": {"type": "string"}}, "required": ["description"]},
}
