"""基础工具：执行命令、读取和修改文件、查找路径，以及更新步骤清单。"""

import atexit
import os
import signal
import subprocess
import threading
import time
from pathlib import Path

from .todo import run_todo_write


# 当前工作目录，以及需要告诉模型的命令运行环境。
WORKDIR = Path.cwd()
ENVIRONMENT_PROMPT = (
    "Windows: the bash tool runs through PowerShell; use PowerShell syntax, not "
    "Unix Bash or cmd.exe syntax, and prefer dedicated file tools for file operations"
    if os.name == "nt"
    else "Unix-like: the bash tool runs the system shell"
)


def safe_path(p: str, cwd: Path | None = None) -> Path:
    """将文件路径解析为工作目录内的绝对路径；越界时抛出异常。"""
    # 按调用者指定的任务目录解析文件路径
    # base 是本次文件访问允许的根目录；有 cwd 时取任务目录，否则取主工作目录。
    base = (cwd or WORKDIR).resolve()
    path = (base / p).resolve()
    if not path.is_relative_to(base):
        raise ValueError(f"Path escapes workspace: {p}")
    return path


# 登记 shell 进程，并在命令结束或程序退出时执行清理
# 已经创建且尚未完成清理的 Popen 对象集合，供退出流程统一停止命令。
_shell_processes: set[subprocess.Popen] = set()
# 保护进程登记、集合快照和移除操作的线程锁。
_shell_process_lock = threading.RLock()


def _stop_process_group(process: subprocess.Popen):
    """停止命令进程；POSIX 同时停止其进程组，Windows 停止直接 shell 进程。"""
    # Windows 分支检查并停止直接 shell 进程，每次停止操作后短暂等待退出。
    if os.name == "nt":
        for stop in (process.terminate, process.kill):
            if process.poll() is not None:
                return
            try:
                stop()
            except OSError:
                return
            try:
                process.wait(timeout=0.05)
            except subprocess.TimeoutExpired:
                continue
        return

    # 向进程组发送终止信号，然后发送强制结束信号。
    for sig in (signal.SIGTERM, getattr(signal, "SIGKILL", signal.SIGTERM)):
        try:
            os.killpg(process.pid, sig)
        except OSError:
            return
        time.sleep(0.05)


def _stop_all_shell_processes():
    """停止当前登记的所有 shell 进程。"""
    with _shell_process_lock:
        # 先取得当前登记表的快照，再逐个执行停止操作。
        processes = list(_shell_processes)
    for process in processes:
        _stop_process_group(process)


def _handle_termination_signal(signum, _frame):
    """收到终止信号时清理命令进程，并以对应状态码退出。"""
    _stop_all_shell_processes()
    raise SystemExit(128 + signum)


atexit.register(_stop_all_shell_processes)


# 同步与后台命令共用进程执行函数，返回输出及退出码
def _run_bash_process(command: str, workdir: Path | None = None) -> tuple[str, int | None]:
    """在指定目录执行命令，最多等待 120 秒，返回输出文字和退出码。"""
    # 1. 确定命令的工作目录，并按平台组装 shell 启动参数。
    workdir = WORKDIR.resolve() if workdir is None else workdir.resolve()
    if os.name == "nt":
        # Windows 使用 PowerShell 执行命令，输出编码设为 UTF-8。
        prefix = (
            "[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new(); "
            "$OutputEncoding = [Console]::OutputEncoding; $ErrorActionPreference = 'Stop'; "
        )
        args = ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", prefix + command]
        popen_kwargs = {"creationflags": subprocess.CREATE_NO_WINDOW}
    else:
        # 其他平台使用系统默认 shell 执行命令。
        args = command
        popen_kwargs = {"start_new_session": True}
    # process 保存启动后的 Popen 对象；尚未创建成功时为 None。
    process = None
    # collected 表示标准输出和错误输出是否已经完整收集完毕。
    collected = False
    try:
        # 创建命令进程并登记，然后收集标准输出和错误输出。
        with _shell_process_lock:
            process = subprocess.Popen(
                args, shell=os.name != "nt", cwd=workdir,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                encoding="utf-8", errors="replace",
                env=dict(os.environ, PYTHONIOENCODING="utf-8"), **popen_kwargs,
            )
            _shell_processes.add(process)
        # 2. 等待进程输出完成；stdout 与 stderr 分别保存标准输出和错误输出文字。
        stdout, stderr = process.communicate(timeout=120)
        collected = True
        # 合并输出，最多返回前 50000 个字符。
        output = (stdout + stderr).strip()
        return (output[:50000] if output else "(no output)"), process.returncode
    except subprocess.TimeoutExpired:
        return "Error: Timeout (120s)", None
    except OSError as error:
        return f"Error: {type(error).__name__}: {error}", None
    finally:
        # 停止仍在运行的命令，关闭已收集的输出管道，并移除进程记录。
        if process is not None:
            _stop_process_group(process)
            try:
                process.wait(timeout=0.2)
            except subprocess.TimeoutExpired:
                pass
            if collected or os.name != "nt":
                for stream in (process.stdout, process.stderr):
                    if stream is not None:
                        stream.close()
            with _shell_process_lock:
                _shell_processes.discard(process)


def _format_bash_result(output: str, exit_code: int | None) -> str:
    """将命令输出和退出码转换为工具结果，非零退出码附带失败说明。"""
    # exit_code 为 0 表示成功，None 表示未取得退出码，此时 output 通常已含错误说明。
    if exit_code in (0, None):
        return output
    return f"Error: command exited with status {exit_code}\n{output}"


def run_bash(command: str, run_in_background: bool = False, cwd: Path | None = None) -> str:
    """同步执行命令并返回文字结果；后台请求由父循环另行分发。"""
    return _format_bash_result(*_run_bash_process(command, cwd))


def run_read(path: str, limit: int | None = None, cwd: Path | None = None) -> str:
    """读取工作目录内的文本文件；limit 指定最多返回的行数。"""
    try:
        # 先校验路径并读取文件全文，再拆为 lines；后续按 limit 限制展示的文件行数。
        lines = safe_path(path, cwd).read_text(encoding="utf-8").splitlines()
        if limit and limit < len(lines):
            # 文件行数超过限制时，返回前 limit 行和剩余行数提示。
            lines = lines[:limit] + [f"... ({len(lines) - limit} more lines)"]
        return "\n".join(lines)
    except Exception as e:
        return f"Error: {e}"


def run_write(path: str, content: str, cwd: Path | None = None) -> str:
    """将 content 写入指定文件，创建缺失的父目录，返回写入的字节数。"""
    try:
        # 校验文件位置，补建父目录，再将本次 content 写为文件的完整内容。
        file_path = safe_path(path, cwd)
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text(content, encoding="utf-8")
        # 读取写入后文件的大小，生成工具结果。
        return f"Wrote {file_path.stat().st_size} bytes to {path}"
    except Exception as e:
        return f"Error: {e}"


def run_edit(path: str, old_text: str, new_text: str, cwd: Path | None = None) -> str:
    """将文件中首次出现的 old_text 替换为 new_text，并返回修改结果。"""
    try:
        # 1. 读取目标文件，检查待替换的 old_text 是否出现。
        file_path = safe_path(path, cwd)
        text = file_path.read_text(encoding="utf-8")
        if old_text not in text:
            return f"Error: text not found in {path}"
        # 2. 仅替换第一次匹配的文字，将结果写回原文件。
        file_path.write_text(text.replace(old_text, new_text, 1), encoding="utf-8")
        return f"Edited {path}"
    except Exception as e:
        return f"Error: {e}"


def run_glob(pattern: str, cwd: Path | None = None) -> str:
    """按 pattern 查找工作目录中的路径，以每行一个路径的形式返回结果。"""
    import glob as g
    try:
        # 收集匹配结果，只保留解析后仍在工作目录内的路径。
        results = []
        # base 是本次查找的任务目录或主目录，results 保存通过目录范围检查的相对路径。
        base = (cwd or WORKDIR).resolve()
        for match in g.glob(pattern, root_dir=base):
            if (base / match).resolve().is_relative_to(base):
                results.append(match)
        return "\n".join(results) if results else "(no matches)"
    except Exception as e:
        return f"Error: {e}"


# 提供给模型的命令、文件和步骤清单工具，包括名称、用途和参数要求。
TOOLS = [
    # bash 增加显式后台执行参数
    {"name": "bash", "description": "Run a shell command.",
     "input_schema": {"type": "object", "properties": {"command": {"type": "string"}, "run_in_background": {"type": "boolean"}}, "required": ["command"]}},
    {"name": "read_file", "description": "Read file contents.",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "limit": {"type": "integer"}}, "required": ["path"]}},
    {"name": "write_file", "description": "Write content to a file.",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}}, "required": ["path", "content"]}},
    {"name": "edit_file", "description": "Replace exact text in a file once.",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "old_text": {"type": "string"}, "new_text": {"type": "string"}}, "required": ["path", "old_text", "new_text"]}},
    {"name": "glob", "description": "Find files matching a glob pattern.",
     "input_schema": {"type": "object", "properties": {"pattern": {"type": "string"}}, "required": ["pattern"]}},
    {"name": "todo_write", "description": "Create and manage a task list for your current coding session.",
     "input_schema": {"type": "object", "properties": {"todos": {"type": "array", "items": {"type": "object", "properties": {"content": {"type": "string"}, "status": {"type": "string", "enum": ["pending", "in_progress", "completed"]}}, "required": ["content", "status"]}}}, "required": ["todos"]}},
]

# 工具名称与实际执行函数的对应关系。
TOOL_HANDLERS = {
    "bash": run_bash, "read_file": run_read, "write_file": run_write,
    "edit_file": run_edit, "glob": run_glob, "todo_write": run_todo_write,
}
