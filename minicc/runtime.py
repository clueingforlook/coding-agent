"""定时运行入口：检查到期任务，在 Agent 空闲时启动回合，并与终端共用会话。"""

from datetime import datetime
import threading

from . import cron, teams
from .agent import agent_loop
from .hooks import trigger_hooks


# 运行线程、停止事件和用户／定时回合共用的锁
# 调度线程和事件处理线程的停止信号，与团队队友的 TEAM_STOP 分开。
RUNTIME_STOP = threading.Event()
# 运行服务线程列表，包含时间检查线程与定时／团队事件处理线程。
runtime_threads: list[threading.Thread] = []
# 运行服务是否已启动，或停止时仍有线程尚未结束。
runtime_started = False
# 保护服务线程的启动、停止和 runtime_threads 登记。
runtime_lock = threading.Lock()
# 用户、定时和团队唤醒回合共用的锁；取得它的入口可以使用共享会话历史。
agent_lock = threading.Lock()


def cron_scheduler_loop(stop_event: threading.Event = RUNTIME_STOP):
    """每秒检查一次本地时间，将当前到期的任务放入交付队列。"""
    # 每次等候一秒；未收到停止信号时，按当前本地时间检查到期任务。
    while not stop_event.wait(1.0):
        cron.poll_due_jobs(datetime.now())


def print_latest_assistant_text(messages: list):
    """在终端显示会话中最近一条模型消息的普通文字。"""
    for message in reversed(messages):
        if message.get("role") != "assistant":
            continue
        content = message.get("content")
        if isinstance(content, str):
            print(content)
        return


def run_agent_turn_locked(client, model, messages, workdir, thinking="", user_query=None):
    """在调用方已取得 agent_lock 时运行一次回合，显示最终回复或错误。"""
    # 1. 用户触发时记录输入并加入历史；自动唤醒时 user_query 为 None，不额外添加用户输入。
    if user_query is not None:
        trigger_hooks("UserPromptSubmit", user_query)
        messages.append({"role": "user", "content": user_query})
    # 消费 Lead 收件箱，将团队结果和控制事件加入当前回合
    # 2. inbox 是本次取出的 Lead 团队事件，先应用协议状态再将事件文字加入历史。
    inbox = teams.consume_lead_inbox()
    if inbox:
        messages.append({"role": "user", "content": teams.format_team_events(inbox)})
        print(f"[wake: {len(inbox)} team event(s) -> new turn]")
    # 3. 运行共享 Agent 回合，成功后显示最近回复，失败时显示错误。
    try:
        agent_loop(client, model, messages, workdir, thinking)
    except Exception as error:
        print(f"  [error] {type(error).__name__}: {error}")
        return
    print_latest_assistant_text(messages)
    print()


def queue_processor_loop(run_turn, stop_event: threading.Event = RUNTIME_STOP):
    """每 0.2 秒检查定时队列与 Lead 收件箱，Agent 空闲时启动新回合。"""
    # 1. 每隔 0.2 秒检查是否有定时任务或团队事件，并尝试取得 Agent 使用权。
    while not stop_event.wait(0.2):
        if not has_runtime_events() or not agent_lock.acquire(blocking=False):
            continue
        # 2. 取得锁后再次检查事件和停止状态，仍有工作时调用已绑定配置的 run_turn。
        try:
            if not stop_event.is_set() and has_runtime_events():
                run_turn()
        except Exception as error:
            print(f"  [cron] scheduled turn failed: {type(error).__name__}: {error}")
        # 3. 本次尝试结束后释放 agent_lock，让其他回合继续执行。
        finally:
            agent_lock.release()


# 团队事件和定时任务共用一个 Lead 唤醒入口
def has_runtime_events():
    """判断是否有到期定时任务或尚未消费的 Lead 收件箱消息。"""
    return cron.has_cron_queue() or teams.BUS.peek("lead")


def start_runtime_threads(run_turn):
    """加载持久任务，启动调度和队列处理线程；重复启动时保持现有线程。"""
    global runtime_started
    with runtime_lock:
        # 1. 检查上一轮退出后是否还有未结束的线程，以及当前服务是否已启动。
        if RUNTIME_STOP.is_set():
            runtime_threads[:] = [thread for thread in runtime_threads if thread.is_alive()]
            runtime_started = bool(runtime_threads)
            if runtime_started:
                raise RuntimeError("Previous scheduled turn is still stopping")
        if runtime_started:
            return
        # 2. 加载保存的定时任务，清除停止标志，准备两个服务线程。
        cron.load_durable_jobs()
        RUNTIME_STOP.clear()
        # run_turn 是主入口绑定了客户端、模型和 history 的回调，供队列线程启动回合。
        runtime_threads.extend([
            threading.Thread(target=cron_scheduler_loop, name="cron-scheduler", daemon=True),
            threading.Thread(target=queue_processor_loop, args=(run_turn,), name="cron-queue-processor", daemon=True),
        ])
        # 启动失败时，通知已启动的线程停止，并保留仍存活的线程记录。
        try:
            # 3. 启动所有服务线程；若中途失败，后续异常分支停止已启动的线程。
            for thread in runtime_threads:
                thread.start()
        except Exception:
            RUNTIME_STOP.set()
            for thread in runtime_threads:
                if thread.is_alive():
                    thread.join(timeout=1)
            runtime_threads[:] = [thread for thread in runtime_threads if thread.is_alive()]
            runtime_started = bool(runtime_threads)
            raise
        runtime_started = True


def stop_runtime_threads():
    """通知调度线程停止，每个线程最多等待一秒，并保留尚未退出的线程记录。"""
    global runtime_started
    with runtime_lock:
        # 1. 发出停止信号，并对每个已登记线程短暂等待。
        RUNTIME_STOP.set()
        for thread in runtime_threads:
            thread.join(timeout=1)
        # 2. 仅保留仍活着的线程，并据此更新服务是否仍处于运行／停止中的状态。
        runtime_threads[:] = [thread for thread in runtime_threads if thread.is_alive()]
        runtime_started = bool(runtime_threads)
