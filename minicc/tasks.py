"""持久任务：创建任务、记录依赖、认领执行并更新完成状态。"""

from dataclasses import asdict, dataclass
from contextlib import contextmanager
import json
import os
from pathlib import Path
import re
import secrets
import threading
import time

from . import tools
from . import team_state as state


# 任务存储位置、ID 格式和任务工具使用说明
# 共享任务的存储目录；每个 task_<编号>.json 保存一条任务。
TASKS_DIR = tools.WORKDIR / ".tasks"
# 任务 ID 的格式约束，校验调用参数和任务文件名称。
TASK_ID_PATTERN = re.compile(r"^task_[0-9a-f]{8}$")
# 提供给 Lead 的任务工具说明：先创建节点取得 ID，再用这些 ID 建立依赖。
TASK_SYSTEM_PROMPT = (
    "Use task tools to track dependencies and progress. Create all task nodes "
    "first. After create_task returns runtime-generated IDs, use update_task "
    "with those exact IDs to add dependencies."
)


@dataclass
class Task:
    """一条持久任务，包含标题、详细要求、执行状态、负责人和前置任务。"""
    # 任务的持久 ID，既用于工具参数，也用于定位对应的 JSON 文件。
    id: str
    # 任务短标题，用于清单展示、认领结果和任务摘要。
    subject: str
    # 任务的完整要求，供认领该任务的 Agent 了解要完成的工作。
    description: str
    status: str  # pending：待开始；in_progress：执行中；completed：已完成。
    owner: str | None  # 认领任务的负责人；None 表示尚未认领。
    blockedBy: list[str]  # 开始本任务前必须完成的任务 ID。
    # 任务可绑定一个独立 Git 工作目录
    # 可选的 .worktrees 子目录名称；None 表示使用主工作目录。
    worktree: str | None = None


# 任务文件的创建、读取、校验和保存
class TaskStore:
    """管理一个目录中的任务 JSON 文件，提供任务读写和依赖更新操作。"""

    def __init__(self, directory: Path):
        """指定任务文件目录；实际目录在写入任务时创建。"""
        # 本实例管理的任务文件目录；多个实例或进程可以指向同一个目录。
        self.directory = directory

    # 同一任务目录内的线程锁和跨进程文件锁
    @contextmanager
    def lock(self):
        """串行执行任务修改，嵌套调用共用本线程已经取得的文件锁。"""
        # 1. 先取得进程内任务锁，读取本线程的嵌套深度 depth。
        with state.task_lock:
            depth = getattr(_task_store_state, "depth", 0)
            # 2. depth 为 0 表示最外层进入，准备 .lock 文件并取得跨进程锁。
            if depth == 0:
                root = self._root(create=True)
                lock_path = (root / ".lock").resolve()
                if lock_path.parent != root:
                    raise ValueError("Task lock escapes the task directory")
                # handle 是本线程打开的锁文件对象，外层退出时释放文件锁并关闭它。
                handle = lock_path.open("a+b")
                try:
                    # Windows 锁定文件的第一个字节；遇到锁占用时等待后继续尝试。
                    if os.name == "nt":
                        import msvcrt
                        if handle.seek(0, os.SEEK_END) == 0:
                            handle.write(b"\0")
                            handle.flush()
                        handle.seek(0)
                        while True:
                            try:
                                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                                break
                            except OSError as error:
                                if error.errno not in (11, 13, 36):
                                    raise
                                time.sleep(0.05)
                    else:
                        import fcntl
                        # POSIX 对锁文件取得排他锁，成功后进入任务修改阶段。
                        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
                except BaseException:
                    handle.close()
                    raise
                _task_store_state.handle = handle
            # 3. 记录进入的新层数，yield 将执行权交给 with 块中的任务操作。
            _task_store_state.depth = depth + 1
            try:
                yield
            finally:
                # 4. with 块结束后减少层数；仅最外层退出时解锁并关闭锁文件。
                _task_store_state.depth -= 1
                if _task_store_state.depth == 0:
                    handle = _task_store_state.handle
                    try:
                        if os.name == "nt":
                            import msvcrt
                            handle.seek(0)
                            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                        else:
                            import fcntl
                            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                    finally:
                        handle.close()
                        del _task_store_state.handle

    def _root(self, create: bool = False) -> Path:
        """返回工作区内的任务目录；create 为 True 时创建缺失的目录。"""
        # 先取得绝对目录并检查工作区范围；create 为 True 时补建目录。
        root = self.directory.resolve()
        if not root.is_relative_to(tools.WORKDIR.resolve()):
            raise ValueError("Task store escapes the workspace")
        if create:
            root.mkdir(parents=True, exist_ok=True)
        return root

    def _path(self, task_id: str, create_root: bool = False) -> Path:
        """校验任务 ID，返回该任务在存储目录内的 JSON 文件路径。"""
        # 1. 校验任务 ID 的字符串类型与编号格式。
        if not isinstance(task_id, str) or not TASK_ID_PATTERN.fullmatch(task_id):
            raise ValueError(f"Invalid task ID: {task_id!r}")
        # 2. 取得存储目录，组合任务文件名，并确认解析后的文件仍在该目录内。
        root = self._root(create=create_root)
        path = (root / f"{task_id}.json").resolve()
        if not path.is_relative_to(root):
            raise ValueError(f"Invalid task ID: {task_id!r}")
        return path

    def exists(self, task_id: str) -> bool:
        """判断指定 ID 的任务文件是否存在。"""
        return self._path(task_id).is_file()

    def create(self, subject: str, description: str = "") -> Task:
        """创建并保存一个待开始的任务，返回包含新 ID 的 Task 对象。"""
        with self.lock():
            # 检查任务标题，并准备任务存储目录。
            subject = subject.strip()
            if not subject:
                raise ValueError("Task subject cannot be empty")
            self._root(create=True)
            # 为新任务分配 ID，初始状态为 pending，负责人和依赖均为空。
            for _ in range(100):
                task = Task(
                    id=f"task_{secrets.token_hex(4)}",
                    subject=subject, description=description,
                    status="pending", owner=None, blockedBy=[],
                )
                try:
                    # 保存新任务；如果 ID 已被占用，则重新分配 ID。
                    with self._path(task.id, create_root=True).open("x", encoding="utf-8") as handle:
                        json.dump(asdict(task), handle, ensure_ascii=False, indent=2)
                    return task
                except FileExistsError:
                    continue
            raise RuntimeError("Could not allocate a unique task ID")

    def _depends_on(self, task_id: str, target_id: str) -> bool:
        """判断 task_id 是否直接或间接依赖 target_id。"""
        # pending 保存尚待检查的任务 ID；每轮取出一个任务，再加入它的前置任务。
        pending = [task_id]
        # visited 保存已经检查过的任务 ID；再次遇到时跳过。
        visited = set()
        # 逐个检查前置任务，找到目标 ID 时返回 True。
        while pending:
            current = pending.pop()
            if current == target_id:
                return True
            if current in visited:
                continue
            visited.add(current)
            pending.extend(self.load(current).blockedBy)
        return False

    def update_dependencies(self, task_id: str, add_blocked_by: list[str]) -> Task:
        """给尚未认领的待开始任务添加前置依赖，保存后返回更新的 Task。"""
        with self.lock():
            # 检查传入的依赖列表，以及目标任务是否允许修改依赖。
            if not isinstance(add_blocked_by, list):
                raise ValueError("addBlockedBy must be a list of task IDs")
            task = self.load(task_id)
            if task.status != "pending" or task.owner is not None:
                raise ValueError(
                    f"Task {task_id} dependencies can only be updated while pending and unowned"
                )
            # dependencies 是本次新增依赖去重后的 ID 列表；全部校验通过后才追加到任务。
            dependencies = list(dict.fromkeys(add_blocked_by))
            # 检查所有前置任务是否存在，以及新增依赖是否形成自依赖或循环。
            for dependency in dependencies:
                if dependency == task_id:
                    raise ValueError("Task cannot depend on itself")
                if not self.exists(dependency):
                    raise ValueError(f"Dependency not found: {dependency}")
                if dependency not in task.blockedBy and self._depends_on(dependency, task_id):
                    raise ValueError(f"Dependency cycle detected: {task_id} -> {dependency}")
            # 将尚未记录的依赖追加到 blockedBy，然后保存任务。
            task.blockedBy.extend(dependency for dependency in dependencies if dependency not in task.blockedBy)
            self.save(task)
            return task

    def save(self, task: Task) -> None:
        """将 Task 的当前字段写入对应的 JSON 文件。"""
        # 在任务锁内写入临时文件，再替换正式记录
        with self.lock():
            # 1. path 是正式任务文件，temporary 是本次写入使用的临时文件。
            path = self._path(task.id, create_root=True)
            temporary = tools.safe_path(str(path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")))
            try:
                # 2. 把当前 Task 序列化成 JSON 写入临时文件，再替换正式文件。
                temporary.write_text(json.dumps(asdict(task), ensure_ascii=False, indent=2), encoding="utf-8")
                os.replace(temporary, path)
            finally:
                # 3. 清理本次临时文件；正式文件已替换成功时，该临时路径通常已不存在。
                temporary.unlink(missing_ok=True)

    def load(self, task_id: str) -> Task:
        """读取任务文件，校验文件内的 ID 和状态，返回 Task 对象。"""
        # 1. data 是从 JSON 读取的字段字典，随后还原为 Task 对象。
        data = json.loads(self._path(task_id).read_text(encoding="utf-8"))
        task = Task(**data)
        # 2. 检查文件里的 ID 与请求的 ID 一致，并验证状态取值后返回对象。
        if task.id != task_id:
            raise ValueError(f"Task file ID does not match {task_id}")
        if task.status not in ("pending", "in_progress", "completed"):
            raise ValueError(f"Invalid task status: {task.status}")
        return task

    def list(self) -> list[Task]:
        """按文件名顺序读取全部任务；任务目录不存在时返回空列表。"""
        if not self.directory.exists():
            return []
        root = self._root()
        return [self.load(path.stem) for path in sorted(root.glob("task_*.json"))]


# 线程本地的锁上下文：depth 表示嵌套层数，handle 是最外层取得的锁文件句柄。
_task_store_state = threading.local()
# 当前主工作目录对应的 TaskStore；Agent 回合入口按所选目录绑定这个实例。
TASKS = TaskStore(TASKS_DIR)


@contextmanager
def task_store_lock():
    """取得当前任务存储的线程锁和文件锁。"""
    with TASKS.lock():
        yield


def save_task(task: Task):
    """保存当前任务的状态及 worktree 绑定。"""
    TASKS.save(task)


def _task_path(task_id: str) -> Path:
    """返回当前任务存储中指定 ID 的文件路径。"""
    return TASKS._path(task_id)


# 创建、查询任务与更新前置依赖
def create_task(subject: str, description: str = "") -> Task:
    """在当前任务存储中创建任务，返回新任务。"""
    with task_store_lock():
        return TASKS.create(subject, description)


def update_task(task_id: str, addBlockedBy: list[str]) -> Task:
    """为指定任务添加 addBlockedBy 中的前置任务，返回更新后的任务。"""
    with task_store_lock():
        return TASKS.update_dependencies(task_id, addBlockedBy)


def load_task(task_id: str) -> Task:
    """按 ID 取得当前任务存储中的 Task 对象。"""
    return TASKS.load(task_id)


def list_tasks() -> list[Task]:
    """取得当前任务存储中的全部 Task 对象。"""
    return TASKS.list()


def get_task(task_id: str) -> str:
    """返回指定任务的完整 JSON 文本，包含描述、状态、负责人和依赖。"""
    return json.dumps(asdict(load_task(task_id)), ensure_ascii=False, indent=2)


def incomplete_dependencies(task: Task) -> list[str]:
    """列出尚未完成、文件缺失或读取校验失败的前置任务 ID。"""
    incomplete = []
    for dependency in task.blockedBy:
        try:
            if load_task(dependency).status != "completed":
                incomplete.append(dependency)
        except (FileNotFoundError, ValueError):
            # 缺失或校验失败的前置任务也加入未完成列表。
            incomplete.append(dependency)
    return incomplete


def can_start(task_id: str) -> bool:
    """判断前置任务是否全部完成；任务自身的状态由 claim_task 检查。"""
    return not incomplete_dependencies(load_task(task_id))


# pending -> in_progress -> completed，完成后提示新解锁任务
# 任务所有权、计划状态与目录租约
def claim_task(task_id: str, owner: str='agent') -> str:
    """原子认领一个可开始的任务，记录负责人、任务目录和任务版本。"""
    with task_store_lock():
        # 1. 读取目标任务，检查它仍为 pending 且尚未分配负责人。
        task = load_task(task_id)
        if task.status != 'pending':
            return f'Task {task_id} is {task.status}, cannot claim'
        if task.owner:
            return f'Task {task_id} is already owned by {task.owner}'
        # 2. assignment 是当前回合仍占用的目录记录；current 是负责人另一个进行中的任务。
        assignment = state.teammate_assignments.get(owner)
        if assignment:
            return f"Owner {owner} must finish the current work turn for {assignment['task_id']} before claiming another task"
        current = _owner_in_progress(owner)
        if current:
            return f'Owner {owner} must complete {current.id} before claiming another task'
        # 3. 确认前置依赖完成，再解析并校验任务的实际工作目录 cwd。
        if not can_start(task_id):
            return f'Blocked by: {incomplete_dependencies(task)}'
        cwd, error = task_worktree_cwd(task)
        if error:
            return f'Cannot claim {task_id}: {error}'
        # 4. 保存负责人和 in_progress 状态，登记本回合目录，并递增任务版本。
        task.owner = owner
        task.status = 'in_progress'
        save_task(task)
        state.teammate_assignments[owner] = {'task_id': task.id, 'cwd': cwd}
        state.advance_assignment_version(owner)
    print(f'  [claim] {task.subject} -> in_progress (owner: {owner})')
    return f'Claimed {task.id} ({task.subject})'


# 任务所有权、计划状态与目录租约
def complete_task(task_id: str, owner: str='agent') -> str:
    """完成负责人已认领且计划允许执行的任务，保留当前回合的目录租约。"""
    with task_store_lock():
        # 1. 检查任务正在执行、负责人相符，并且计划状态允许完成。
        task = load_task(task_id)
        if task.status != 'in_progress':
            return f'Task {task_id} is {task.status}, cannot complete'
        if task.owner != owner:
            return f'Task {task_id} is owned by {task.owner}, not {owner}; cannot complete'
        gate = state.plan_gates.get(owner, 'not_required')
        if gate in {'required', 'pending', 'rejected'}:
            return f'Task {task_id} cannot complete while plan status is {gate}'
        # 2. 确认当前任务目录记录存在；记录缺失或属于其他任务时，从当前任务恢复。
        assignment = state.teammate_assignments.get(owner)
        if not assignment or assignment.get('task_id') != task.id:
            cwd, error = task_worktree_cwd(task)
            if error:
                return f'Task {task_id} cannot complete: {error}'
            state.teammate_assignments[owner] = {'task_id': task.id, 'cwd': cwd}
        # 3. ready_before 保存完成操作前已满足依赖的任务 ID；unblocked 是本次新解锁的任务标题。
        ready_before = {t.id for t in list_tasks() if t.status == 'pending' and t.blockedBy and can_start(t.id)}
        task.status = 'completed'
        save_task(task)
        unblocked = [t.subject for t in list_tasks() if t.status == 'pending' and t.blockedBy and t.id not in ready_before and can_start(t.id)]
    print(f'  [complete] {task.subject}')
    # 4. 返回当前任务完成说明，附上本次新解锁的下游任务。
    msg = f'Completed {task.id} ({task.subject})'
    if unblocked:
        msg += f"\nUnblocked: {', '.join(unblocked)}"
        print(f"  [unblocked] {', '.join(unblocked)}")
    return msg


# 供模型调用的任务工具及其文字结果
def run_create_task(subject: str, description: str = "") -> str:
    """创建任务，向模型返回新任务 ID 和标题。"""
    task = create_task(subject, description)
    print(f"  [create] {task.subject}")
    return f"Created {task.id}: {task.subject}"


def run_update_task(task_id: str, addBlockedBy: list[str]) -> str:
    """添加前置依赖，向模型返回任务 ID 和更新后的依赖列表。"""
    task = update_task(task_id, addBlockedBy)
    dependencies = ", ".join(task.blockedBy) or "(none)"
    print(f"  [update] {task.subject} blockedBy: {dependencies}")
    return f"Updated {task.id} blockedBy: {dependencies}"


def run_list_tasks() -> str:
    """生成任务清单，每行包含状态标记、ID、标题、负责人和依赖。"""
    tasks = list_tasks()
    if not tasks:
        return "No tasks. Use create_task to add some."
    lines = []
    for task in tasks:
        marker = {"pending": "[ ]", "in_progress": "[>]", "completed": "[x]"}.get(task.status, "[?]")
        dependencies = f" (blockedBy: {', '.join(task.blockedBy)})" if task.blockedBy else ""
        owner = f" [{task.owner}]" if task.owner else ""
        worktree = f" (worktree: {task.worktree})" if task.worktree else ""
        lines.append(f"{marker} {task.id}: {task.subject} [{task.status}]{owner}{dependencies}{worktree}")
    return "\n".join(lines)


def run_get_task(task_id: str) -> str:
    """向模型返回指定任务的完整 JSON 文本。"""
    return get_task(task_id)


def run_claim_task(task_id: str) -> str:
    """以 agent 身份认领任务，向模型返回执行结果。"""
    return claim_task(task_id, owner="agent")


def run_complete_task(task_id: str) -> str:
    """以 agent 身份完成任务，向模型返回完成结果及新解锁任务。"""
    return complete_task(task_id, owner="agent")


# 模型可调用的六个任务工具，以及各工具接受的参数。
TOOLS = [
    {"name": "create_task", "description": "Create a task and return its runtime-generated ID.",
     "input_schema": {"type": "object", "properties": {"subject": {"type": "string"}, "description": {"type": "string"}}, "required": ["subject"], "additionalProperties": False}},
    {"name": "update_task", "description": "Add dependencies using IDs returned by create_task.",
     "input_schema": {"type": "object", "properties": {"task_id": {"type": "string", "pattern": "^task_[0-9a-f]{8}$"}, "addBlockedBy": {"type": "array", "items": {"type": "string", "pattern": "^task_[0-9a-f]{8}$"}, "minItems": 1}}, "required": ["task_id", "addBlockedBy"], "additionalProperties": False}},
    {"name": "list_tasks", "description": "List tasks with status, owner, and dependencies.",
     "input_schema": {"type": "object", "properties": {}}},
    {"name": "get_task", "description": "Get a task by ID.",
     "input_schema": {"type": "object", "properties": {"task_id": {"type": "string"}}, "required": ["task_id"]}},
    {"name": "claim_task", "description": "Claim a pending task whose dependencies are complete.",
     "input_schema": {"type": "object", "properties": {"task_id": {"type": "string"}}, "required": ["task_id"]}},
    {"name": "complete_task", "description": "Complete the task claimed by this agent.",
     "input_schema": {"type": "object", "properties": {"task_id": {"type": "string"}}, "required": ["task_id"]}},
]

# 工具名称与实际执行函数的对应关系。
TOOL_HANDLERS = {
    "create_task": run_create_task, "update_task": run_update_task,
    "list_tasks": run_list_tasks, "get_task": run_get_task,
    "claim_task": run_claim_task, "complete_task": run_complete_task,
}


def _owner_in_progress(owner: str) -> Task | None:
    """查找指定负责人当前尚未完成的任务，没有时返回 None。"""
    return next((task for task in list_tasks() if task.status == 'in_progress' and task.owner == owner), None)


def assignment_cwd(owner: str) -> Path:
    """取得或恢复负责人的任务目录，绑定失效时返回错误而不切换目录。"""
    with state.task_lock:
        # 1. 读取内存目录记录 assignment，同时查找持久任务里属于该 owner 的进行中任务。
        assignment = state.teammate_assignments.get(owner)
        task = _owner_in_progress(owner)
        # 2. 记录缺失或任务已变化时，从持久任务恢复目录；没有任何分配则返回主目录。
        if task and (not assignment or assignment.get('task_id') != task.id):
            cwd, error = task_worktree_cwd(task)
            if error:
                raise ValueError(error)
            assignment = {'task_id': task.id, 'cwd': cwd}
            state.teammate_assignments[owner] = assignment
        elif not assignment:
            return tools.WORKDIR
        # 3. 核对目录记录对应的任务仍由该 owner 持有，并且处于执行中或已完成状态。
        task = load_task(str(assignment['task_id']))
        if task.status not in {'in_progress', 'completed'} or task.owner != owner:
            raise ValueError(f'Assignment for {owner} is no longer active')
        cwd, error = task_worktree_cwd(task)
        if error:
            raise ValueError(error)
        # 4. 确认任务重新解析出的目录与本回合记录一致，再返回该目录。
        if cwd.resolve() != Path(assignment['cwd']).resolve():
            raise ValueError(f'Assignment cwd changed for task {task.id}')
        return cwd


def release_completed_assignment(owner: str) -> bool:
    """在回合结束时释放已完成任务的目录租约，清除本任务的计划要求。"""
    with state.task_lock:
        # 1. 检查负责人当前的目录记录及对应任务，只有已完成且仍属该 owner 才释放。
        assignment = state.teammate_assignments.get(owner)
        if not assignment:
            return False
        task = load_task(str(assignment['task_id']))
        if task.status != 'completed' or task.owner != owner:
            return False
        # 2. 移除目录记录，推进任务版本，并把本任务的计划要求恢复为 not_required。
        state.teammate_assignments.pop(owner, None)
        state.advance_assignment_version(owner)
        if owner in state.plan_gates:
            state.plan_gates[owner] = 'not_required'
        return True


def release_teammate_assignment(owner: str):
    """队友退出时将未完成任务退回 pending，并释放目录租约。"""
    with task_store_lock():
        try:
            # 1. 查找退出队友持有的进行中任务，将其恢复为 pending、清空 owner 并保存。
            task = _owner_in_progress(owner)
            if task:
                task.status = 'pending'
                task.owner = None
                save_task(task)
        # 2. 结束任务处理后，释放目录记录并清理本任务的计划状态。
        finally:
            state.teammate_assignments.pop(owner, None)
            state.advance_assignment_version(owner)
            if owner in state.plan_gates:
                state.plan_gates[owner] = 'not_required'


def task_worktree_cwd(task: Task):
    """解析任务的 worktree 或默认项目目录，返回路径和错误说明。"""
    from .worktrees import task_worktree_cwd as resolve_cwd
    return resolve_cwd(task)
