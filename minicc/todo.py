"""步骤清单：在当前进程中保存、更新并显示 Agent 的执行计划。"""

import ast
import json


# 当前清单，每项记录步骤内容和 pending / in_progress / completed 状态。
# 每项字典包含 content（步骤说明）和 status（执行状态）；每次调用 todo_write 整表替换。
CURRENT_TODOS: list[dict] = []


def _normalize_todos(todos):
    """将输入转换为步骤列表并校验每项，返回 (清单, 错误说明)。"""
    # 字符串输入先按 JSON 解析，失败时再按 Python 列表文本解析。
    if isinstance(todos, str):
        try:
            todos = json.loads(todos)
        except json.JSONDecodeError:
            try:
                todos = ast.literal_eval(todos)
            except (SyntaxError, ValueError):
                return None, "Error: todos must be a list or JSON array string"
    if not isinstance(todos, list):
        return None, "Error: todos must be a list"
    # 检查每项是否含有内容字段和合法的执行状态。
    # i 是步骤索引，用于标明错误位置；t 是这一项的字段字典。
    for i, t in enumerate(todos):
        if not isinstance(t, dict):
            return None, f"Error: todos[{i}] must be an object"
        if "content" not in t or "status" not in t:
            return None, f"Error: todos[{i}] missing 'content' or 'status'"
        if t["status"] not in ("pending", "in_progress", "completed"):
            return None, f"Error: todos[{i}] has invalid status '{t['status']}'"
    return todos, None


def run_todo_write(todos: list) -> str:
    """用传入清单替换当前步骤清单，在终端显示计划，并返回步骤数量。"""
    global CURRENT_TODOS
    # 1. 将输入整理为合法清单；error 非空时直接返回该错误，不更新当前状态。
    todos, error = _normalize_todos(todos)
    if error:
        return error
    # 保存校验通过的清单，按每项状态生成终端显示内容。
    CURRENT_TODOS = todos
    # 2. lines 保存要打印的标题与步骤行，icon 根据每一步的状态选择显示标记。
    lines = ["\n\033[33m## Current Tasks\033[0m"]
    for t in CURRENT_TODOS:
        icon = {"pending": " ", "in_progress": "\033[36m▸\033[0m", "completed": "\033[32m✓\033[0m"}[t["status"]]
        lines.append(f"  [{icon}] {t['content']}")
    print("\n".join(lines))
    return f"Updated {len(CURRENT_TODOS)} tasks"
