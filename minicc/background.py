"""后台命令：登记命令、在线程中执行，并在后续模型请求前收集完成通知。"""

from pathlib import Path
import threading
from xml.sax.saxutils import escape

from . import tools


# 后台命令的运行状态、结果和完成队列
class BackgroundManager:
    """管理当前进程中的后台命令，保存运行状态和待收集的结果。"""

    def __init__(self):
        """建立任务表、结果表、完成队列和任务编号计数器。"""
        # 后台编号 -> 命令元信息字典，记录原 tool_use_id、command 和 running/completed/failed 状态。
        self.tasks: dict[str, dict] = {}
        # 后台编号 -> 命令结束后的结果文字；收集通知时从此取出。
        self.results: dict[str, str] = {}
        # 已经结束但尚未收集的后台编号队列；与持久任务板中的 task ID 不同。
        self._ready: list[str] = []
        # 本管理器分配 bg_0001 等编号时使用的递增计数。
        self._counter = 0
        # 保护任务表、结果表、完成队列和编号分配的线程锁。
        self._lock = threading.Lock()

    def start(self, tool_name: str, tool_input: dict, tool_use_id: str, workdir: Path | None = None) -> str:
        """登记并启动一条后台 bash 命令，立即返回 bg 编号。"""
        # 1. 检查工具和命令，记录本次命令使用的工作目录。
        if tool_name != "bash":
            raise ValueError("Only Bash commands can run in the background")
        command = tool_input.get("command")
        if not isinstance(command, str) or not command.strip():
            raise ValueError("Bash command cannot be empty")
        workdir = (workdir or tools.WORKDIR).resolve()

        # 2. 分配编号，在任务表中登记 running 状态。
        with self._lock:
            self._counter += 1
            task_id = f"bg_{self._counter:04d}"
            self.tasks[task_id] = {
                "tool_use_id": tool_use_id, "command": command, "status": "running",
            }

        # 3. 启动后台线程；线程启动失败时，撤销任务记录。
        thread = threading.Thread(target=self._run, args=(task_id, command, workdir), daemon=True)
        try:
            thread.start()
        except Exception:
            with self._lock:
                self.tasks.pop(task_id, None)
            raise
        print(f"  [background] started {task_id}: {command[:60]}")
        return task_id

    def _run(self, task_id: str, command: str, workdir: Path):
        """执行命令，保存 completed 或 failed 状态，并将编号加入完成队列。"""
        # 1. output 是命令输出，exit_code 是退出码；生成结果文字并确定最终状态。
        try:
            output, exit_code = tools._run_bash_process(command, workdir)
            result = tools._format_bash_result(output, exit_code)
            status = "completed" if exit_code == 0 else "failed"
        except Exception as error:
            result = f"Error: {type(error).__name__}: {error}"
            status = "failed"

        # 2. 将最终状态、输出和完成编号一起写入管理器，等待主循环收集。
        with self._lock:
            task = self.tasks.get(task_id)
            if task is None:
                return
            task["status"] = status
            self.results[task_id] = result
            self._ready.append(task_id)

    def collect(self) -> list[str]:
        """取出尚未收集的完成结果，返回通知列表，并清除已收集的任务记录。"""
        # 1. 取出本次已完成的任务及输出，清空这些任务的内存记录。
        with self._lock:
            # ready 暂存本次取出的 (后台编号, 任务元信息, 结果文字)，离开锁后再生成通知。
            ready = []
            for task_id in self._ready:
                task = self.tasks.pop(task_id, None)
                result = self.results.pop(task_id, "")
                if task is not None:
                    ready.append((task_id, task, result))
            self._ready.clear()

        # 2. 生成通知，包含任务编号、状态、原命令和前 500 字符的结果。
        notifications = []
        for task_id, task, result in ready:
            notifications.append(
                f"<task_notification>\n"
                f"  <task_id>{task_id}</task_id>\n"
                f"  <status>{task['status']}</status>\n"
                f"  <command>{escape(task['command'])}</command>\n"
                f"  <summary>{escape(result[:500])}</summary>\n"
                f"</task_notification>"
            )
            print(f"  [background] collected {task_id}: {task['status']}")
        return notifications


# 当前进程共用的后台管理器；以下两个变量引用它的任务表和结果表。
BACKGROUND = BackgroundManager()
background_tasks = BACKGROUND.tasks
background_results = BACKGROUND.results
BACKGROUND_SYSTEM_PROMPT = "Set run_in_background to true only for independent Bash commands."


# 后台执行判断、启动入口和结果通知
def should_run_background(tool_name: str, tool_input: dict) -> bool:
    """判断是否为显式设置 run_in_background=true 的 bash 调用。"""
    return tool_name == "bash" and tool_input.get("run_in_background") is True


def start_background_task(tool_name: str, tool_input: dict, tool_use_id: str, workdir: Path | None = None) -> str:
    """向当前后台管理器提交命令，返回任务编号。"""
    # 后台命令使用 Lead 当前认领任务的工作目录
    return BACKGROUND.start(tool_name, tool_input, tool_use_id, workdir)


def collect_background_results() -> list[str]:
    """收集当前管理器中已完成且尚未发送的后台通知。"""
    return BACKGROUND.collect()


def inject_background_results(messages: list) -> int:
    """将已完成的后台通知追加为一条 user 消息，返回本次通知数量。"""
    notifications = collect_background_results()
    if not notifications:
        return 0
    messages.append({"role": "user", "content": "\n\n".join(notifications)})
    return len(notifications)
