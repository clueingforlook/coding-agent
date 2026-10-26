"""任务工作目录：创建与校验任务绑定的 Git worktree，并提供宿主清理入口。"""
from pathlib import Path
import re
import subprocess

from . import tools, tasks
from . import team_state as state

# 任务绑定的 Git worktree
# worktree 名称格式：1 至 64 个字符，允许字母、数字、点、下划线和短横线。
VALID_WORKTREE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")

def validate_worktree_name(name: str) -> str | None:
    """校验 worktree 名称的字符和长度，返回错误说明或 None。"""
    if not isinstance(name, str) or not VALID_WORKTREE_NAME.fullmatch(name):
        return 'worktree name must be 1-64 letters, digits, dots, underscores, or dashes, and start with a letter or digit'
    if name in {'.', '..'} or '..' in name:
        return "worktree name cannot contain '..'"
    return None


def _worktree_path(name: str) -> Path:
    """取得工作区 .worktrees 内的目录路径，越界时抛出异常。"""
    root = tools.safe_path('.worktrees')
    path = (root / name).resolve()
    if not path.is_relative_to(root) or path == root:
        raise ValueError(f'Worktree path escapes directory: {name!r}')
    return path


def _worktree_branch(name: str) -> str:
    """取得 worktree 对应的 wt/ 分支名称。"""
    return f'wt/{name}'


def _run_git(args: list[str], cwd: Path | None=None) -> tuple[bool, str]:
    """在指定目录执行 Git，返回执行状态和完整输出。"""
    try:
        # 执行 args 指定的 Git 子命令，cwd 为命令目录；result 保存退出码和两路输出。
        result = subprocess.run(['git', '-c', 'core.quotepath=false', *args], cwd=cwd or tools.WORKDIR, capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=30)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return (False, f'{type(exc).__name__}: {exc}')
    output = (result.stdout + result.stderr).strip()
    return (result.returncode == 0, output or '(no output)')


def run_git(args: list[str], cwd: Path | None=None) -> tuple[bool, str]:
    """执行 Git，最多向调用方返回 5000 字符输出。"""
    ok, output = _run_git(args, cwd)
    return (ok, output[:5000])


def _registered_worktrees() -> tuple[dict[Path, dict[str, str]], str | None]:
    """读取 Git worktree 注册表，按绝对目录路径组织条目。"""
    # 1. 读取 Git 的 worktree 注册信息，output 是完整的逐行文本。
    ok, output = _run_git(['worktree', 'list', '--porcelain'])
    if not ok:
        return ({}, f'cannot read Git worktree registry: {output}')
    # entries 的键是 checkout 绝对路径，值是该 worktree 的注册字段字典。
    entries: dict[Path, dict[str, str]] = {}
    # current 累积一个 worktree 的字段，例如 worktree、HEAD、branch；空行表示该条目结束。
    current: dict[str, str] = {}
    # 2. 逐行读取注册字段，遇到条目结束时将 current 放入 entries。
    for line in output.splitlines() + ['']:
        if not line:
            raw_path = current.get('worktree')
            if raw_path:
                entries[Path(raw_path).resolve()] = current
            current = {}
            continue
        key, _, value = line.partition(' ')
        current[key] = value
    return (entries, None)


def _registered_worktree(name: str) -> tuple[Path | None, str | None]:
    """检查 worktree 的目录和分支注册信息，返回有效路径或错误。"""
    # 1. 解析候选目录，并读取 Git 注册表。
    try:
        path = _worktree_path(name)
    except ValueError as exc:
        return (None, str(exc))
    entries, error = _registered_worktrees()
    if error:
        return (None, error)
    # 2. 确认目录已注册且实际存在，再核对它是否位于约定的 wt/<名称> 分支。
    if path not in entries:
        return (None, f"worktree '{name}' is not registered with Git")
    if not path.is_dir():
        return (None, f"worktree '{name}' is missing at {path}")
    expected_branch = f'refs/heads/{_worktree_branch(name)}'
    if entries[path].get('branch') != expected_branch:
        return (None, f"worktree '{name}' is not registered on expected branch '{_worktree_branch(name)}'")
    return (path, None)


def task_worktree_cwd(task: tasks.Task) -> tuple[Path, str | None]:
    """解析任务的工作目录；未绑定 worktree 时使用项目目录。"""
    if not task.worktree:
        return (tools.WORKDIR, None)
    path, error = _registered_worktree(task.worktree)
    return (path or tools.WORKDIR, error)


def create_worktree(name: str, task_id: str) -> str:
    """为待认领任务创建 Git worktree，成功后保存任务绑定。"""
    # 检查名称、任务状态与目标路径。
    error = validate_worktree_name(name)
    if error:
        return f'Error: {error}'
    try:
        path = _worktree_path(name)
        task_path = tasks._task_path(task_id)
    except ValueError as exc:
        return f'Error: {exc}'
    # branch 是将要建立的 wt/<名称> 分支；path 是 checkout 目录，task_path 是任务 JSON 文件。
    branch = _worktree_branch(name)
    # 1. 在任务锁内确认任务待认领、尚未绑定目录，并检查名称和路径是否已被占用。
    with tasks.task_store_lock():
        if not task_path.exists():
            return f'Error: Task {task_id} not found'
        task = tasks.load_task(task_id)
        if task.status != 'pending' or task.owner is not None:
            return f'Error: Task {task_id} must be pending and unowned'
        if task.worktree:
            return f"Error: Task {task_id} already uses worktree '{task.worktree}'"
        if any((t.worktree == name for t in tasks.list_tasks() if t.id != task_id)):
            return f"Error: Worktree '{name}' is already bound to another task"
        if path.exists():
            return f'Error: Worktree path already exists: {path}'
        # 检查仓库根目录、分支和 Git 注册记录。
        # 2. 检查 Git 仓库根目录、分支名称与已有注册记录。
        ok, root = run_git(['rev-parse', '--show-toplevel'])
        if not ok or Path(root).resolve() != tools.WORKDIR.resolve():
            return 'Error: Working directory must be the root of a Git repository'
        ok, branch_check = run_git(['check-ref-format', '--branch', branch])
        if not ok:
            return f"Error: Invalid worktree branch '{branch}': {branch_check}"
        exists, _ = run_git(['show-ref', '--verify', '--quiet', f'refs/heads/{branch}'])
        if exists:
            return f"Error: Branch '{branch}' already exists"
        entries, registry_error = _registered_worktrees()
        if registry_error:
            return f'Error: {registry_error}'
        if path in entries:
            return f'Error: Worktree path is already registered: {path}'
        # 创建 checkout，然后把 worktree 名称写入任务。
        # 3. 创建容器目录，从 HEAD 建立新分支和对应 checkout。
        tools.safe_path('.worktrees').mkdir(parents=True, exist_ok=True)
        ok, result = run_git(['worktree', 'add', '-b', branch, str(path), 'HEAD'])
        if not ok:
            entries, registry_error = _registered_worktrees()
            branch_exists, _ = run_git(['show-ref', '--verify', '--quiet', f'refs/heads/{branch}'])
            # artifacts 记录 Git 报错后实际留下的目录、注册项和分支，供返回部分执行结果。
            artifacts = []
            if path.exists():
                artifacts.append(f"checkout path '{path}'")
            if registry_error is None and path in entries:
                artifacts.append('registered Git worktree')
            if branch_exists:
                artifacts.append(f"branch '{branch}'")
            if artifacts:
                return f"Partial operation: git worktree add reported an error after leaving {', '.join(artifacts)}. Task {task_id} remains unbound and no Git data was deleted. Run `git worktree list`, inspect '{path}' and '{branch}', then keep or remove those artifacts manually after preserving any work. Git error: {result}"
            return f'Git error: {result}'
        try:
            # 4. Git 创建成功后，将 worktree 名称绑定到任务并保存。
            task.worktree = name
            tasks.save_task(task)
        except Exception as exc:
            return f"Partial success: Worktree '{name}' was created at {path} on branch '{branch}', but task binding failed: {exc}. Git data was retained for manual recovery."
    print(f'  \x1b[33m[worktree] created: {name} at {path}\x1b[0m')
    return f"Worktree '{name}' created at {path} for task {task_id}"


def remove_worktree(name: str, discard_changes: bool=False) -> str:
    """供宿主移除已完成且未占用的 worktree，保留分支并解除任务绑定。

    discard_changes=True 表示丢弃未提交内容，仅用于宿主已另行取得用户确认的清理。
    """
    # 检查名称、任务状态与目标路径。
    error = validate_worktree_name(name)
    if error:
        return f'Error: {error}'
    with tasks.task_store_lock():
        path, error = _registered_worktree(name)
        if error:
            return f'Error: {error}'
        # 1. bound 是绑定此目录的任务；active 是其中尚未完成的任务。
        bound = [task for task in tasks.list_tasks() if task.worktree == name]
        if not bound:
            return f"Error: Worktree '{name}' is not bound to a task"
        active = [task for task in bound if task.status != 'completed']
        if active:
            return f"Error: Worktree '{name}' is bound to active task {active[0].id}; complete it before removal"
        # 2. leased 保存当前仍在使用此目录的负责人名称，有使用者时返回占用说明。
        leased = [owner for owner, assignment in state.teammate_assignments.items() if Path(assignment['cwd']).resolve() == path.resolve()]
        if leased:
            return f"Error: Worktree '{name}' is still in use by {', '.join(sorted(leased))}; wait for the turn to end"
        # 3. 查询 checkout 的改动状态，包含未跟踪和已忽略文件；按 discard_changes 决定是否允许继续。
        ok, status = run_git(['status', '--porcelain', '--ignored'], cwd=path)
        if not ok:
            return f"Error: Cannot verify worktree '{name}' status: {status}"
        if status != '(no output)' and (not discard_changes):
            changed = len([line for line in status.splitlines() if line.strip()])
            return f"Error: Worktree '{name}' has {changed} uncommitted change(s); preserve or discard them manually"
        # 4. 组装移除 checkout 的 Git 参数；discard_changes 为 True 时附加强制移除选项。
        args = ['worktree', 'remove']
        if discard_changes:
            args.append('--force')
        args.append(str(path))
        ok, result = run_git(args)
        if not ok:
            return f'Git error: {result}'
        try:
            # 5. Git 移除成功后，清空任务中的 worktree 绑定；分支继续保留。
            for task in bound:
                task.worktree = None
                tasks.save_task(task)
        except Exception as exc:
            return f"Partial success: Worktree '{name}' was removed and branch '{_worktree_branch(name)}' retained, but task unbinding failed: {exc}. Manual recovery is required."
    print(f'  [worktree] removed: {name}; branch retained')
    return f"Worktree '{name}' removed; branch '{_worktree_branch(name)}' retained"
