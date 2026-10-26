"""团队共享状态：队友生命周期、任务目录租约，以及计划与关机请求。"""

from dataclasses import dataclass, field
import threading
import time


# 任务与团队状态共用的锁和注册表
# task_lock：当前进程内，对共享任务和任务目录记录的访问锁；允许同一线程重复取得。
task_lock = threading.RLock()
# team_lock：保护队友状态、计划闸门、协议请求和线程登记表。
team_lock = threading.RLock()
# 负责人名称 -> 当前目录记录，例如 {"task_id": "task_...", "cwd": Path(...)}。
# 已完成任务的目录记录仍保留到当前工作回合结束。
teammate_assignments: dict[str, dict] = {}
# 负责人名称 -> 任务分配版本号；认领或释放任务时递增，计划申请记录这个版本。
assignment_versions: dict[str, int] = {}
# 队友名称 -> 生命周期状态：working / waiting_approval / idle / stopping。
active_teammates: dict[str, str] = {}
# 队友名称 -> 计划状态：not_required / required / pending / approved / rejected。
# 工具执行时读取这个状态，判断是否允许命令执行和文件修改。
plan_gates: dict[str, str] = {}
# 队友名称 -> 当前等待审批的 request_id；回复生效后移除这一关联。
plan_request_ids: dict[str, str] = {}
# 队友名称 -> 执行该队友 WORK／IDLE 循环的线程对象。
teammate_threads: dict[str, threading.Thread] = {}
# 宿主退出时的整队停止信号；单个队友的关闭由 shutdown 协议处理。
TEAM_STOP = threading.Event()


@dataclass
class ProtocolState:
    """一次计划审批或关机请求，记录通信双方、请求状态及所关联的任务版本。"""
    request_id: str  # 控制请求编号，回复用它查找原请求。
    type: str  # shutdown 表示关机，plan_approval 表示计划审批。
    sender: str  # 请求发起方：Lead 或队友名称。
    target: str  # 接收并处理请求的一方。
    status: str  # pending 等待处理；approved 已同意；rejected 已拒绝。
    payload: str  # 请求正文，例如待审批的完整计划。
    work_version: int | None = None  # 提交计划时的任务分配版本；关机请求通常不使用。
    task_id: str | None = None  # 提交计划时正在处理的任务 ID。
    created_at: float = field(default_factory=time.time)  # 请求建立时的 Unix 时间戳，单位为秒。


# request_id -> 协议记录；同时保留等待处理和已经批准／拒绝的请求。
pending_requests: dict[str, ProtocolState] = {}


def advance_assignment_version(owner: str):
    """递增负责人的任务版本，清除旧审批关联，并将已有计划要求恢复为 required。"""
    with task_lock, team_lock:
        # 1. 更新任务身份，后续新计划将记录新的版本号。
        assignment_versions[owner] = assignment_versions.get(owner, 0) + 1
        # 2. 保留已有的计划要求，清除上一项工作的审批状态和请求关联。
        if owner in plan_gates and plan_gates[owner] != "not_required":
            plan_gates[owner] = "required"
        plan_request_ids.pop(owner, None)


def current_work_identity(owner: str) -> tuple[int, str | None]:
    """取得负责人当前的任务版本和任务 ID。"""
    with task_lock:
        # 将当前任务 ID 与版本号配成工作身份；未认领时任务 ID 为 None。
        assignment = teammate_assignments.get(owner)
        return assignment_versions.get(owner, 0), str(assignment["task_id"]) if assignment else None
