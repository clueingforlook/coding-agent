"""终端入口：配置、用户输入、模型循环和最终回复。"""

import argparse
from functools import partial
import os
from pathlib import Path
import signal

import httpx
from dotenv import load_dotenv

from . import cron, runtime, skills, teams, tools


try:
    import readline
    # 设置终端的输入编辑和中文字符处理。
    readline.parse_and_bind('set bind-tty-special-chars off')
    readline.parse_and_bind('set input-meta on')
    readline.parse_and_bind('set output-meta on')
    readline.parse_and_bind('set convert-meta off')
except ImportError:
    readline = None


def main() -> None:
    """启动命令行程序，读取配置，并循环接收用户问题、运行 Agent、显示回复。"""
    # --- s14 修改：显示 MCP 章节，解析工作目录参数 ---
    parser = argparse.ArgumentParser(description="MiniClaudeCode · s14 MCP Tools")
    parser.add_argument("--workdir", type=Path, default=Path.cwd(), help="命令工作目录")
    # 1. args 是命令行参数对象，workdir 是解析后的主工作目录。
    args = parser.parse_args()
    workdir = args.workdir.resolve()
    if not workdir.is_dir():
        parser.error(f"工作目录不存在：{workdir}")
    tools.WORKDIR = workdir
    # 设置当前工作目录的定时任务文件
    cron.DURABLE_PATH = tools.safe_path(".scheduled_tasks.json")
    # 扫描所选工作目录中的技能文件，建立本次运行的技能目录。
    skills.SKILLS_DIR = workdir / "skills"
    skills._scan_skills()

    # 读取模型服务配置，.env 中的配置覆盖同名环境变量。
    load_dotenv(".env", override=True, encoding="utf-8")
    # 2. api_key 为服务凭据，base_url 为接口根地址，model 和 thinking 为模型调用选项。
    api_key = os.environ["LLM_API_KEY"]
    base_url = os.environ["LLM_BASE_URL"]
    model = os.environ["MODEL_ID"]
    thinking = os.getenv("LLM_THINKING", "")

    print("s14: MCP Tools — discovery and dynamic tool calls (mock servers)")
    print("输入问题，按回车发送。输入 q / exit 或空行退出。\n")
    # 建立会话历史和模型客户端，供后续多次用户提问共用。
    # history 是终端用户、定时唤醒与团队事件共用的 Lead 对话列表。
    history = []
    # 运行期间处理终止信号，离开命令行时清理已登记的 shell
    # previous_handler 保存启动前的 SIGTERM 处理函数，程序退出时恢复。
    previous_handler = signal.signal(signal.SIGTERM, tools._handle_termination_signal)
    try:
        with httpx.Client(
            base_url=base_url.rstrip("/") + "/",
            headers={"Authorization": f"Bearer {api_key}"}, timeout=120,
        ) as client:
            # 终端与定时线程共用客户端、会话历史和回合入口
            # 3. run_turn 是已绑定当前配置与 history 的回合入口，终端和事件线程均调用它。
            run_turn = partial(runtime.run_agent_turn_locked, client, model, history, workdir, thinking)
            try:
                # 4. 启动时间检查与事件处理服务，再持续读取终端输入。
                runtime.start_runtime_threads(run_turn)
                while True:
                    # 等待用户输入时，定时线程可以运行已到期的任务。
                    try:
                        prompt = "\001\033[36m\002s14 >> \001\033[0m\002" if readline else "\033[36ms14 >> \033[0m"
                        query = input(prompt)
                    except (EOFError, KeyboardInterrupt):
                        break
                    if query.strip().lower() in ("q", "exit", ""):
                        break
                    # 用户回合和定时回合轮流取得同一把锁。
                    with runtime.agent_lock:
                        run_turn(user_query=query)
            finally:
                # 5. 离开输入循环时，先停止运行服务，再通知队友退出。
                runtime.stop_runtime_threads()
                # 关闭客户端前通知队友停止工作
                teams.stop_teammates()
    finally:
        # 6. 清理仍登记的命令进程，最后恢复原信号处理函数。
        tools._stop_all_shell_processes()
        signal.signal(signal.SIGTERM, previous_handler)


if __name__ == "__main__":
    main()
