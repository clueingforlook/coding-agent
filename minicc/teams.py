"""团队控制：队友消息、计划审批、关机协议和共享任务发现。"""
import random
import threading

from . import tools, tasks
from .mailbox import BUS, is_valid_agent_name, RESERVED_TEAMMATE_NAMES
from .team_state import (
    ProtocolState, task_lock, team_lock, teammate_assignments, assignment_versions,
    active_teammates, plan_gates, plan_request_ids, pending_requests, teammate_threads,
    current_work_identity, TEAM_STOP,
)
from .tasks import (
    Task, load_task, list_tasks, can_start, claim_task, complete_task,
    task_worktree_cwd, assignment_cwd, release_completed_assignment,
    release_teammate_assignment, run_list_tasks, _owner_in_progress,
)
from .worktrees import create_worktree

# 团队事件、控制协议和空闲任务发现
# 空闲队友等待消息的间隔，单位为秒；超时后扫描一次共享任务板。
IDLE_SCAN_INTERVAL = 2.0

def new_request_id() -> str:
    """生成尚未使用的协议请求编号。"""
    while True:
        request_id = f'req_{random.randint(0, 999999):06d}'
        if request_id not in pending_requests:
            return request_id


def match_response(response_type: str, request_id: str, approve: bool, from_agent: str, to_agent: str) -> bool:
    """校验回复类型、通信双方和 pending 状态，记录批准或拒绝结果。"""
    with team_lock:
        # 1. 用回复携带的 request_id 找到原协议记录 state。
        state = pending_requests.get(request_id)
        if not state:
            print(f'  [protocol] unknown request_id: {request_id}')
            return False
        # 2. expected 是原请求对应的回复类型；接着核对消息双方和待处理状态。
        expected = {'shutdown': 'shutdown_response', 'plan_approval': 'plan_approval_response'}[state.type]
        if response_type != expected:
            print(f'  [protocol] expected {expected}, got {response_type}')
            return False
        if from_agent != state.target or to_agent != state.sender:
            print(f'  [protocol] {request_id} responder mismatch')
            return False
        if state.status != 'pending':
            print(f'  [protocol] {request_id} already {state.status}')
            return False
        # 3. 将合法回复写为 approved 或 rejected，完成这次协议状态更新。
        state.status = 'approved' if approve else 'rejected'
    print(f'  [protocol] {request_id} -> {state.status}')
    return True


def consume_lead_inbox() -> list[dict]:
    """消费 Lead 收件箱，先更新控制协议状态，再返回全部事件。"""
    # 1. 取出 Lead 收件箱中的全部消息，包括普通消息、任务结果和协议回复。
    msgs = BUS.read_inbox('lead')
    # 2. 对带请求编号的控制回复更新协议状态；其余消息仍保留在返回列表中。
    for msg in msgs:
        metadata = msg.get('metadata', {})
        request_id = metadata.get('request_id', '')
        if request_id and msg.get('type', '').endswith('_response'):
            match_response(msg['type'], request_id, metadata.get('approve', False), msg.get('from', ''), msg.get('to', ''))
    return msgs


def format_team_events(msgs: list[dict]) -> str:
    """将团队事件整理为交给 Lead 的一条文字消息。"""
    # lines 保存发给 Lead 的事件摘要；每条消息占一行，协议消息额外显示请求编号。
    lines = []
    for msg in msgs:
        metadata = msg.get('metadata', {})
        request_id = metadata.get('request_id')
        suffix = f' request_id={request_id}' if request_id else ''
        lines.append(f"[{msg['type']}{suffix}] {msg['from']}: {msg['content']}")
    return '[Team events]\n' + '\n'.join(lines)


def _teammate_submit_plan(from_name: str, plan: str) -> str:
    """保存队友当前任务的计划审批请求，将计划交给 Lead。"""
    # 1. 取得提交者的任务目录记录 assignment、当前任务 ID 和任务分配版本。
    with task_lock:
        assignment = teammate_assignments.get(from_name)
        task_id = str(assignment['task_id']) if assignment else None
        work_version = assignment_versions.get(from_name, 0)
        # 2. 检查是否已有待审批计划，再创建当前工作身份对应的新请求。
        with team_lock:
            if plan_gates.get(from_name) == 'pending':
                return 'A plan is already waiting for review.'
            request_id = new_request_id()
            # 将计划与当前任务 ID、任务版本一起保存。
            pending_requests[request_id] = ProtocolState(
                request_id=request_id, type='plan_approval', sender=from_name,
                target='lead', status='pending', payload=plan,
                work_version=work_version, task_id=task_id,
            )
            # 3. 将计划闸门设为 pending，登记队友正在等待的审批编号和生命周期状态。
            plan_gates[from_name] = 'pending'
            plan_request_ids[from_name] = request_id
            active_teammates[from_name] = 'waiting_approval'
    # 4. 将计划正文和 request_id 发到 Lead 收件箱。
    BUS.send(from_name, 'lead', plan, 'plan_approval_request', {'request_id': request_id})
    return f"Plan submitted ({request_id}). Wait for Lead's decision."


def apply_plan_response(name: str, msg: dict) -> tuple[bool, str]:
    """将匹配当前任务版本的 Lead 审批结果应用到队友计划闸门。"""
    # 1. 读取回复中的请求编号，并取得队友现在的任务版本与任务 ID。
    metadata = msg.get('metadata', {})
    request_id = metadata.get('request_id', '')
    work_version, task_id = current_work_identity(name)
    with team_lock:
        state = pending_requests.get(request_id)
        # expected_id 是该队友当前等待回复的计划编号，state 是编号对应的完整协议记录。
        expected_id = plan_request_ids.get(name)
        # 核对消息双方、请求编号、任务身份和审批状态。
        # 2. 检查回复是否对应当前计划，且决定与 Lead 保存的审批状态一致。
        valid = (
            msg.get('from') == 'lead' and msg.get('to') == name
            and request_id == expected_id and state is not None
            and state.type == 'plan_approval'
            and state.sender == name and state.target == 'lead'
            and state.work_version == work_version and state.task_id == task_id
            and state.status in {'approved', 'rejected'}
            and metadata.get('approve', False) == (state.status == 'approved')
        )
        if not valid:
            return (False, '[Ignored plan response: request mismatch]')
        # 3. 将决定应用到计划闸门，标记队友可以继续处理，并清除已完成的请求关联。
        plan_gates[name] = state.status
        active_teammates[name] = 'working'
        plan_request_ids.pop(name, None)
        outcome = state.status
    return (True, f"[Plan {outcome}] {msg['content']}")


def apply_shutdown_request(name: str, msg: dict) -> tuple[bool, str]:
    """校验 Lead 的关机请求，将有效请求对应的队友标为 stopping。"""
    # 1. 取得关机消息中的 request_id，查找原请求。
    request_id = msg.get('metadata', {}).get('request_id', '')
    with team_lock:
        state = pending_requests.get(request_id)
        # 2. 检查请求来自 Lead、目标是当前队友，并且尚未处理。
        valid = (
            msg.get('from') == 'lead' and msg.get('to') == name and state is not None
            and state.type == 'shutdown' and state.sender == 'lead' and state.target == name
            and state.status == 'pending' and active_teammates.get(name) != 'stopping'
        )
        if not valid:
            return (False, '[Ignored shutdown request: request mismatch]')
        # 3. 标记队友正在停止；调用方随后发送确认并退出队友循环。
        active_teammates[name] = 'stopping'
    return (True, request_id)


def _teammate_send_message(from_name: str, to: str, content: str) -> str:
    """向 Lead 或活跃队友发送中间协作消息。"""
    with team_lock:
        if to != 'lead' and to not in active_teammates:
            return f"Agent '{to}' is not active"
    BUS.send(from_name, to, content)
    return f'Sent to {to}'


def scan_unclaimed_tasks() -> list[Task]:
    """找出无人认领、依赖已完成且工作目录有效的待开始任务。"""
    with task_lock:
        # ready 保存本次发现的候选 Task 对象；真正的所有权变更发生在 claim_task 中。
        ready = []
        for task in list_tasks():
            # 1. 筛选待开始、无人负责且前置任务已完成的任务。
            if task.status != 'pending' or task.owner is not None or (not can_start(task.id)):
                continue
            # 2. 检查任务目录或 worktree 绑定，目录有效才加入候选列表。
            _, error = task_worktree_cwd(task)
            if not error:
                ready.append(task)
        return ready


def claim_next_task(name: str) -> Task | None:
    """为空闲队友认领第一个仍可用的任务，没有可用任务时返回 None。"""
    # 1. 当前还有任务目录记录或进行中的任务时，本轮不认领新任务。
    with task_lock:
        if teammate_assignments.get(name) or _owner_in_progress(name):
            return None
    # 2. 依次尝试候选任务，返回第一个真正认领成功的任务。
    for task in scan_unclaimed_tasks():
        result = claim_task(task.id, owner=name)
        if result.startswith('Claimed '):
            return load_task(task.id)
    return None


def run_list_teammates() -> str:
    """返回当前活跃队友的名称和生命周期状态。"""
    with team_lock:
        if not active_teammates:
            return 'No active teammates.'
        return '\n'.join((f'{name}: {status}' for name, status in sorted(active_teammates.items())))


def run_send_message(to: str, content: str) -> str:
    """由 Lead 向指定活跃队友发送普通消息。"""
    if to not in active_teammates:
        return f"Teammate '{to}' is not active"
    BUS.send('lead', to, content)
    return f'Sent to {to}'


def run_request_shutdown(teammate: str) -> str:
    """建立关机协议请求并发送给指定队友，返回请求编号。"""
    # 1. 确认目标队友仍活跃，建立 pending 状态的关机请求。
    if teammate not in active_teammates:
        return f"Teammate '{teammate}' is not active"
    with team_lock:
        request_id = new_request_id()
        pending_requests[request_id] = ProtocolState(request_id=request_id, type='shutdown', sender='lead', target=teammate, status='pending', payload='')
    # 2. 把请求编号发送给队友，后续关机回复使用同一编号关联原请求。
    BUS.send('lead', teammate, 'Finish the current step and shut down.', 'shutdown_request', {'request_id': request_id})
    return f'Shutdown requested from {teammate} ({request_id})'


def run_request_plan(teammate: str, task: str) -> str:
    """要求队友提交计划，并将其计划闸门设为 required。"""
    # task 是需要规划的工作说明文本；此时只要求计划，审批编号在队友提交计划时生成。
    if teammate not in active_teammates:
        return f"Teammate '{teammate}' is not active"
    with team_lock:
        plan_gates[teammate] = 'required'
    BUS.send('lead', teammate, task, 'plan_request')
    return f'Plan requested from {teammate}'


def run_review_plan(request_id: str, approve: bool, feedback: str='') -> str:
    """批准或拒绝队友当前任务的计划，将决定发送给队友。"""
    # 1. 校验批准／拒绝决定，查找计划申请者当前的工作身份。
    if type(approve) is not bool:
        return 'Error: approve must be a boolean'
    state = pending_requests.get(request_id)
    if not state:
        return f'Request {request_id} not found'
    work_version, task_id = current_work_identity(state.sender)
    # 2. 核对请求类型、待处理状态、任务版本和当前计划编号。
    with team_lock:
        state = pending_requests.get(request_id)
        if not state:
            return f'Request {request_id} not found'
        if state.type != 'plan_approval':
            return f'Request {request_id} is not a plan'
        if state.status != 'pending':
            return f'Request {request_id} already {state.status}'
        if state.work_version != work_version or state.task_id != task_id:
            return f'Request {request_id} belongs to an earlier assignment'
        if plan_request_ids.get(state.sender) != request_id:
            return f'Request {request_id} is not the current plan'
        # 3. 保存 Lead 的审批决定；队友消费回复时再更新自己的计划闸门。
        state.status = 'approved' if approve else 'rejected'
    # 4. 使用反馈正文或默认说明，发送带 request_id 和 approve 的审批回复。
    content = feedback or ('Plan approved.' if approve else 'Revise the plan and submit it again.')
    BUS.send('lead', state.sender, content, 'plan_approval_response', {'request_id': request_id, 'approve': approve})
    return f'Plan {state.status} ({request_id})'


def run_create_worktree(name: str, task_id: str) -> str:
    """为指定任务创建并绑定一个 Git worktree。"""
    return create_worktree(name, task_id)


# 绑定当前模型配置，启动与停止持久队友
def spawn_teammate_thread(name, role, prompt, task_id=None, require_plan=False, *, client, model, thinking=""):
    """登记队友、认领初始任务并启动线程，返回启动结果；失败时归还任务。"""
    from .teammate import TeammateRuntime

    # 1. 校验队友名称和运行状态，登记初始生命周期与计划要求。
    if not is_valid_agent_name(name):
        return "Invalid teammate name: use 1-64 letters, digits, underscores, or dashes"
    if name.lower() in RESERVED_TEAMMATE_NAMES:
        return f"Invalid teammate name: '{name}' is reserved by the runtime"
    with team_lock:
        if TEAM_STOP.is_set() and active_teammates:
            return "Team runtime is stopping"
        if any(existing.casefold() == name.casefold() for existing in active_teammates):
            return f"Teammate '{name}' already exists"
        TEAM_STOP.clear()
        active_teammates[name] = "working"
        plan_gates[name] = "required" if require_plan else "not_required"
    # claimed_initial 记录本次启动是否已经成功认领初始任务，启动失败时据此归还任务。
    claimed_initial = False
    try:
        # 2. 有初始任务时先取得任务所有权；没有初始任务时，队友可在后续 IDLE 阶段认领。
        if task_id:
            claimed = claim_task(task_id, owner=name)
            if not claimed.startswith("Claimed "):
                raise ValueError(claimed)
            claimed_initial = True
        # 3. runtime 保存该队友的模型配置、独立历史和工具函数。
        # thread 是执行该对象 WORK／IDLE 循环的守护线程。
        runtime = TeammateRuntime(name, role, prompt, task_id, require_plan, client, model, thinking)
        thread = threading.Thread(target=runtime.run, name=f"teammate-{name}", daemon=True)
        with team_lock:
            teammate_threads[name] = thread
        thread.start()
    # 4. 启动失败时归还本次认领的任务，撤销队友和线程登记。
    except Exception as error:
        if claimed_initial:
            release_teammate_assignment(name)
        with team_lock:
            active_teammates.pop(name, None)
            plan_gates.pop(name, None)
            plan_request_ids.pop(name, None)
            teammate_threads.pop(name, None)
        return f"Cannot spawn teammate '{name}': {type(error).__name__}: {error}"
    print(f"  [teammate] {name} spawned as {role}")
    # 5. assigned 用于返回初始任务说明；实际工作结果将在后续通过收件箱交给 Lead。
    assigned = f" for {task_id}" if task_id else " without an initial Task"
    return f"Teammate '{name}' spawned as {role}{assigned}. End this turn; the runtime will deliver its events."


def run_spawn_teammate(name, role, prompt, task_id=None, require_plan=False, *, client, model, thinking=""):
    """使用 Lead 绑定的模型客户端启动一个队友。"""
    return spawn_teammate_thread(name, role, prompt, task_id, require_plan, client=client, model=model, thinking=thinking)


def stop_teammates():
    """宿主退出时请求所有队友停止，并各等待最多一秒。"""
    # 1. 取得当前队友名称和线程对象的快照。
    with team_lock:
        names = list(active_teammates)
        threads = list(teammate_threads.values())
    # 2. 逐个发送关机请求，再发出整队停止信号。
    for name in names:
        run_request_shutdown(name)
    TEAM_STOP.set()
    # 3. 对其他线程各等待最多一秒，当前线程不等待自己。
    for thread in threads:
        if thread is not threading.current_thread():
            thread.join(timeout=1)

# 面向 Lead 的团队协作规则，包括确认分工、创建任务、委派和等待团队事件。
TEAM_SYSTEM_PROMPT = "Create all task nodes first. Only after create_task returns runtime-generated IDs, use update_task with those exact IDs to add dependencies. Only the Lead changes task dependencies.\nWhen parallel work would help, first propose a small team with clear responsibilities and wait for the user's confirmation. Do not call spawn_teammate before the user confirms. After confirmation, delegate independent work by creating a Task for each parallel change. Pass task_id to spawn_teammate when assigning ready work, then create a task-bound worktree only when a separate working directory would prevent conflicting edits. A teammate must complete its current Task before claiming another. A worktree changes tool default cwd only; it is not a sandbox. Worktree removal stays with the host or user. After spawning a teammate, end the current turn instead of polling its status; the runtime will deliver team events and wake the Lead. React to those events, and shut teammates down when coordination is complete."

# Lead 可调用的七个团队工具
TEAM_TOOLS = [{'name': 'spawn_teammate',
  'description': 'Spawn a persistent teammate.',
  'input_schema': {'type': 'object',
                   'properties': {'name': {'type': 'string', 'pattern': '^[A-Za-z0-9_-]{1,64}$'},
                                  'role': {'type': 'string'},
                                  'prompt': {'type': 'string'},
                                  'task_id': {'type': 'string', 'pattern': '^task_[0-9a-f]{8}$'},
                                  'require_plan': {'type': 'boolean'}},
                   'required': ['name', 'role', 'prompt']}},
 {'name': 'list_teammates',
  'description': 'List active teammates.',
  'input_schema': {'type': 'object', 'properties': {}}},
 {'name': 'send_message',
  'description': 'Message a teammate.',
  'input_schema': {'type': 'object',
                   'properties': {'to': {'type': 'string'}, 'content': {'type': 'string'}},
                   'required': ['to', 'content']}},
 {'name': 'request_shutdown',
  'description': 'Ask a teammate to shut down.',
  'input_schema': {'type': 'object',
                   'properties': {'teammate': {'type': 'string'}},
                   'required': ['teammate']}},
 {'name': 'request_plan',
  'description': 'Require a teammate plan before workspace changes.',
  'input_schema': {'type': 'object',
                   'properties': {'teammate': {'type': 'string'}, 'task': {'type': 'string'}},
                   'required': ['teammate', 'task']}},
 {'name': 'review_plan',
  'description': 'Approve or reject a plan.',
  'input_schema': {'type': 'object',
                   'properties': {'request_id': {'type': 'string'},
                                  'approve': {'type': 'boolean'},
                                  'feedback': {'type': 'string'}},
                   'required': ['request_id', 'approve']}},
 {'name': 'create_worktree',
  'description': 'Create and bind a task worktree.',
  'input_schema': {'type': 'object',
                   'properties': {'name': {'type': 'string',
                                           'pattern': '^(?!.*\\.\\.)[A-Za-z0-9][A-Za-z0-9._-]{0,63}$',
                                           'maxLength': 64},
                                  'task_id': {'type': 'string'}},
                   'required': ['name', 'task_id'],
                   'additionalProperties': False}}]
