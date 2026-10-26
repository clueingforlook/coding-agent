"""事件钩子：在用户输入、工具执行和回合结束时调用已注册的处理函数。"""

from . import tools
from .permissions import permission_hook


# 每个事件对应一组按注册顺序执行的回调。
# 事件名 -> 按注册顺序保存的回调函数列表。
# UserPromptSubmit 接收用户文字；PreToolUse 接收工具名和参数。
# PostToolUse 额外接收工具结果；Stop 接收当前历史消息列表。
HOOKS = {"UserPromptSubmit": [], "PreToolUse": [], "PostToolUse": [], "Stop": []}


def register_hook(event: str, callback):
    """将一个处理函数追加到指定事件的回调列表。"""
    HOOKS[event].append(callback)


def trigger_hooks(event: str, *args):
    """依次触发事件回调，遇到首个非 None 结果就停止并返回该结果。"""
    for callback in HOOKS[event]:
        # result 为当前回调的返回值；None 继续下一回调，其他值交给触发事件的调用方处理。
        result = callback(*args)
        if result is not None:
            return result
    return None


def log_hook(tool_name: str, args: dict):
    """在终端显示当前即将执行的工具名称。"""
    print(f"\033[90m[HOOK] {tool_name}\033[0m")
    return None


def context_inject_hook(query: str):
    """收到用户输入时，在终端显示当前工作目录。"""
    print(f"\033[90m[HOOK] UserPromptSubmit: working in {tools.WORKDIR}\033[0m")
    return None


def summary_hook(messages: list):
    """回合结束时，统计并显示当前历史中保留的工具结果数量。"""
    # tool_count 统计当前历史中的 role=tool 消息；历史压缩后只反映仍保留的记录数。
    tool_count = sum(1 for m in messages if m.get("role") == "tool")
    print(f"\033[90m[HOOK] Stop: session used {tool_count} tool calls\033[0m")
    return None


# 默认事件处理：输入日志、工具权限检查、工具日志和回合统计。
register_hook("UserPromptSubmit", context_inject_hook)
register_hook("PreToolUse", permission_hook)
register_hook("PreToolUse", log_hook)
register_hook("Stop", summary_hook)
