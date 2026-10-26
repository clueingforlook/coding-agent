"""工具权限检查：命令拒绝列表，以及 MCP 工具的宿主策略与终端确认。"""

import threading

from . import mcp

# bash 命令拒绝片段；命令字符串包含其中任意片段时，权限钩子返回拒绝说明。
DENY_LIST = ["rm -rf /", "sudo", "shutdown", "reboot", "mkfs", "dd if="]


def permission_hook(tool_name: str, args: dict) -> str | None:
    """检查命令与 MCP 工具权限；返回 None 表示允许，其他字符串说明阻止原因。"""
    # 1. 检查 bash 命令是否包含拒绝片段。
    if tool_name == "bash":
        # 逐个检查拒绝片段 p；命中时记录日志并结束本次权限检查。
        for p in DENY_LIST:
            if p in args.get("command", ""):
                print(f"\n\033[31m⛔ Blocked: '{p}'\033[0m")
                return "Permission denied"

    # --- s14 新增：按宿主配置检查 MCP 工具，待确认调用交给前台用户 ---
    if tool_name.startswith("mcp__"):
        # 2. policy 来自当前工具池的宿主权限表；未配置的外部工具需要逐次确认。
        policy = mcp.mcp_tool_policies.get(tool_name, "confirm")
        if policy == "allow":
            return None

        # 3. 定时或团队事件线程返回待前台确认的说明，本次调用结束。
        if threading.current_thread() is not threading.main_thread():
            return (
                "Permission denied: external tool requires foreground confirmation. "
                "Ask the user to retry in a foreground turn."
            )

        # 4. 前台显示实际工具名和参数，y / yes 放行，其余输入或中断均拒绝。
        print(f"\n[permission] External tool {tool_name}({args})")
        try:
            answer = input("允许本次调用？[y/N] ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            return "Permission denied by user"
        if answer not in {"y", "yes"}:
            return "Permission denied by user"
    return None
