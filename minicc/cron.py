"""定时任务：解析执行时间，保存任务定义，并管理到期 prompt 的交付状态。"""

from dataclasses import asdict, dataclass
from datetime import datetime
import json
import os
import secrets
import threading

from . import tools


# 定时任务记录、持久文件和待交付队列
# 当前工作目录中的定时任务文件，保存 durable=True 的任务定义和交付状态。
DURABLE_PATH = tools.WORKDIR / ".scheduled_tasks.json"
CRON_SYSTEM_PROMPT = "Use schedule_cron for work that should start at a future local time."


@dataclass
class CronJob:
    """一项定时 prompt，记录执行规则、持久化选项及当前交付状态。"""
    # 定时任务的唯一编号，用于列举、取消以及关联待交付记录。
    id: str
    # 五字段时间规则，顺序为：分钟、小时、日期、月份、星期。
    cron: str
    # 到期后交给 Agent 的任务文字，不是由调度器直接执行的 shell 命令。
    prompt: str
    recurring: bool  # True 为周期任务；False 为只交付一次。
    durable: bool  # True 保存到文件；False 只存在于当前进程。
    pending_delivery: bool = False  # 已经到期，等待主模型接收。
    last_fired: str | None = None  # 最近一次入队的本地日期和分钟。


# 任务编号 -> CronJob；保存当前进程已注册的全部任务，包括仅会话任务。
scheduled_jobs: dict[str, CronJob] = {}
# 已经到期、等待交给模型的 CronJob 对象列表；出队后仍需确认接收。
cron_queue: list[CronJob] = []
# 保护任务注册表、待交付队列及持久化修改的进程内可重入锁。
cron_lock = threading.RLock()


# 五段式 cron 的校验与时间匹配
def _cron_field_matches(field: str, value: int) -> bool:
    """判断单个字段是否匹配当前数值，支持通配符、步长、数字、范围和列表。"""
    # 先匹配任意值和步长；步长通过当前数值是否为 N 的倍数判断。
    if field == "*":
        return True
    if field.startswith("*/"):
        return value % int(field[2:]) == 0
    # 再处理逗号列表和闭区间范围；普通数字只与当前值作相等比较。
    if "," in field:
        return any(_cron_field_matches(part.strip(), value) for part in field.split(","))
    if "-" in field:
        start, end = field.split("-", 1)
        return int(start) <= value <= int(end)
    return value == int(field)


def cron_matches(cron_expr: str, moment: datetime) -> bool:
    """判断已校验的表达式是否匹配指定本地时间；星期日为 0，星期六为 6。"""
    # 1. 将表达式拆成五个字段，提取待匹配的本地时间分量。
    fields = cron_expr.strip().split()
    if len(fields) != 5:
        return False
    minute, hour, day, month, weekday = fields
    # 将 Python 的星期一为 0，转换为 cron 使用的星期日为 0。
    cron_weekday = (moment.weekday() + 1) % 7
    # 先检查分钟、小时和月份。
    if not (
        _cron_field_matches(minute, moment.minute)
        and _cron_field_matches(hour, moment.hour)
        and _cron_field_matches(month, moment.month)
    ):
        return False
    # 日期和星期都有限制时，满足其中一项即可。
    day_matches = _cron_field_matches(day, moment.day)
    weekday_matches = _cron_field_matches(weekday, cron_weekday)
    if day == "*" and weekday == "*":
        return True
    if day == "*":
        return weekday_matches
    if weekday == "*":
        return day_matches
    return day_matches or weekday_matches


def _validate_cron_field(field: str, minimum: int, maximum: int) -> str | None:
    """校验一个字段的语法和取值范围，返回错误说明或 None。"""
    # 1. 通配符直接通过；步长要求为大于 0 的整数。
    if field == "*":
        return None
    if field.startswith("*/"):
        step = field[2:]
        if not step.isdigit() or int(step) <= 0:
            return f"Invalid step: {field}"
        return None
    # 2. 列表逐项使用同样的字段规则校验，返回第一项错误。
    if "," in field:
        for part in field.split(","):
            error = _validate_cron_field(part.strip(), minimum, maximum)
            if error:
                return error
        return None
    # 3. 范围要求起止值均为整数、起点不大于终点，并处于允许区间内。
    if "-" in field:
        start, end = field.split("-", 1)
        if not start.isdigit() or not end.isdigit():
            return f"Invalid range: {field}"
        start_value, end_value = int(start), int(end)
        if start_value > end_value:
            return f"Range start is greater than end: {field}"
        if start_value < minimum or end_value > maximum:
            return f"Range {field} is outside [{minimum}-{maximum}]"
        return None
    # 4. 其余输入按单个整数校验，minimum 和 maximum 是此字段允许的边界。
    if not field.isdigit():
        return f"Invalid field: {field}"
    value = int(field)
    if value < minimum or value > maximum:
        return f"Value {value} is outside [{minimum}-{maximum}]"
    return None


def validate_cron(cron_expr: str) -> str | None:
    """校验分钟、小时、日期、月份、星期五个字段，返回错误说明或 None。"""
    # 1. 检查表达式是字符串，且拆分后恰好有五个字段。
    if not isinstance(cron_expr, str):
        return "Cron expression must be a string"
    fields = cron_expr.strip().split()
    if len(fields) != 5:
        return f"Expected 5 fields, got {len(fields)}"
    # 2. rules 为各字段的名称和上下界；逐项校验，并在错误中指出具体字段。
    rules = [("minute", 0, 59), ("hour", 0, 23), ("day-of-month", 1, 31),
             ("month", 1, 12), ("day-of-week", 0, 6)]
    for field, (name, minimum, maximum) in zip(fields, rules):
        error = _validate_cron_field(field, minimum, maximum)
        if error:
            return f"{name}: {error}"
    return None


# 持久任务的保存、加载和任务编号
def save_durable_jobs():
    """将所有 durable 任务写入临时文件，再替换正式的定时任务文件。"""
    with cron_lock:
        path = tools.safe_path(str(DURABLE_PATH))
        # 1. payload 是本次写入的任务字典列表，只保留需要跨进程重启保存的任务。
        payload = [asdict(job) for job in scheduled_jobs.values() if job.durable]
        # temporary 是本次进程／线程专用的临时路径，path 是正式存储文件。
        temporary = tools.safe_path(str(path.with_name(
            f"{path.name}.{os.getpid()}.{threading.get_ident()}.tmp"
        )))
        try:
            # 2. 写入完整 JSON，再用该文件替换正式文件。
            temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(temporary, path)
        finally:
            # 3. 清理本次写入的临时路径。
            temporary.unlink(missing_ok=True)


def load_durable_jobs():
    """读取持久任务，将已到期但尚未交付的任务恢复到队列；错误记录会显示提示。"""
    # 1. 找到并读取持久文件，确认文件顶层是任务列表。
    path = tools.safe_path(str(DURABLE_PATH))
    if not path.exists():
        return
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
        if not isinstance(payload, list):
            raise ValueError("expected a JSON list")
    except (OSError, ValueError) as error:
        print(f"  [cron] could not load {path.name}: {error}")
        return

    # loaded 统计本次新加入内存的任务数量，重复 ID 不计入。
    loaded = 0
    with cron_lock:
        for item in payload:
            try:
                # 2. 将每条记录还原为 CronJob，校验时间规则、字段类型和必要内容。
                job = CronJob(**item)
                error = validate_cron(job.cron)
                if error:
                    raise ValueError(error)
                if not isinstance(job.id, str) or not job.id.startswith("cron_"):
                    raise ValueError("invalid job ID")
                if not isinstance(job.prompt, str) or not job.prompt.strip():
                    raise ValueError("prompt cannot be empty")
                if any(type(value) is not bool for value in (job.recurring, job.durable, job.pending_delivery)):
                    raise ValueError("job flags must be booleans")
                if job.last_fired is not None and not isinstance(job.last_fired, str):
                    raise ValueError("last_fired must be a string or null")
            except (TypeError, ValueError) as error:
                print(f"  [cron] skipped invalid saved job: {error}")
                continue
            # 3. 跳过已加载任务；新增任务若处于 pending_delivery，则恢复到待交付队列。
            if job.id in scheduled_jobs:
                continue
            scheduled_jobs[job.id] = job
            if job.pending_delivery:
                cron_queue.append(job)
            loaded += 1
    if loaded:
        print(f"  [cron] loaded {loaded} durable job(s)")


def new_cron_id() -> str:
    """分配尚未使用的 cron 编号，最多尝试 100 次。"""
    for _ in range(100):
        job_id = f"cron_{secrets.token_hex(4)}"
        if job_id not in scheduled_jobs:
            return job_id
    raise RuntimeError("Could not allocate a cron job ID")


def schedule_job(cron: str, prompt: str, recurring: bool = True,
                 durable: bool = True) -> CronJob | str:
    """创建定时任务，返回 CronJob；表达式或参数无效时返回错误说明。"""
    # 1. 检查时间规则、任务正文，以及周期和持久化选项的类型。
    error = validate_cron(cron)
    if error:
        return error
    if not isinstance(prompt, str) or not prompt.strip():
        return "Prompt cannot be empty"
    if type(recurring) is not bool or type(durable) is not bool:
        return "recurring and durable must be booleans"
    with cron_lock:
        # 2. 分配编号并注册任务；持久任务随后保存到文件。
        job = CronJob(new_cron_id(), cron, prompt, recurring, durable)
        scheduled_jobs[job.id] = job
        try:
            if durable:
                save_durable_jobs()
        # 3. 保存失败时撤销本次新任务，并把错误交给调用方处理。
        except Exception:
            scheduled_jobs.pop(job.id, None)
            raise
    print(f"  [cron] scheduled {job.id}: {cron} -> {prompt[:60]}")
    return job


def cancel_job(job_id: str) -> str:
    """删除任务及其待交付记录，更新持久文件，返回取消结果。"""
    with cron_lock:
        # 1. 找到待取消任务；不存在时直接返回未找到说明。
        job = scheduled_jobs.get(job_id)
        if job is None:
            return f"Job {job_id} not found"
        # 2. previous_queue 保存取消前的队列快照；随后移除注册记录及该任务的排队项。
        previous_queue = list(cron_queue)
        scheduled_jobs.pop(job_id)
        cron_queue[:] = [queued for queued in cron_queue if queued.id != job_id]
        # 3. 保存取消结果；保存失败时恢复任务和原队列。
        try:
            if job.durable:
                save_durable_jobs()
        except Exception:
            scheduled_jobs[job_id] = job
            cron_queue[:] = previous_queue
            raise
    print(f"  [cron] cancelled {job_id}")
    return f"Cancelled {job_id}"


# 到期入队、成功确认和失败恢复
def _enqueue_due_job(job: CronJob, minute_marker: str | None = None):
    """记录任务已到期和触发分钟，保存成功后将任务加入待交付队列。"""
    # 1. 备份入队前的交付状态与触发分钟，随后标记本次到期。
    old_pending, old_last_fired = job.pending_delivery, job.last_fired
    job.pending_delivery = True
    if minute_marker is not None:
        job.last_fired = minute_marker
    # 2. 持久任务先保存待交付状态；保存失败时还原备份。
    try:
        if job.durable:
            save_durable_jobs()
    except Exception:
        job.pending_delivery, job.last_fired = old_pending, old_last_fired
        raise
    # 3. 状态保存完成后，将任务交给内存队列等待 Agent 消费。
    cron_queue.append(job)


def poll_due_jobs(moment: datetime):
    """检查指定分钟到期的任务，每项任务在同一分钟最多入队一次。"""
    # minute_marker 是本地时间的日期＋分钟标识，用于判断该分钟是否已经触发过。
    minute_marker = moment.strftime("%Y-%m-%d %H:%M")
    with cron_lock:
        # 逐个检查已注册任务；等待交付或本分钟已触发的任务跳过，其余任务匹配 cron 后入队。
        for job in list(scheduled_jobs.values()):
            try:
                if job.pending_delivery or job.last_fired == minute_marker:
                    continue
                if cron_matches(job.cron, moment):
                    _enqueue_due_job(job, minute_marker)
                    print(f"  [cron] due {job.id}: {job.prompt[:60]}")
            except Exception as error:
                print(f"  [cron] could not enqueue {job.id}: {error}")


def consume_cron_queue() -> list[CronJob]:
    """取出当前全部待交付任务，并清空队列；任务仍保持 pending_delivery 状态。"""
    with cron_lock:
        # jobs 是本批出队对象的列表；清空的只是队列，注册表和 pending_delivery 仍保留。
        jobs = list(cron_queue)
        cron_queue.clear()
    return jobs


def acknowledge_cron_jobs(jobs: list[CronJob]):
    """确认主模型已接收任务；删除一次性任务，将周期任务恢复为等待下次触发。"""
    # changed 保存已修改任务及修改前的 pending_delivery 值。
    # removed 保存本次从注册表删除的一次性任务；两者用于持久化失败时恢复状态。
    changed, removed = [], []
    with cron_lock:
        # 1. 更新本批任务的交付状态。
        for delivered in jobs:
            # current 是注册表里的最新任务；已被取消的任务不再确认。
            current = scheduled_jobs.get(delivered.id)
            if current is None:
                continue
            changed.append((current, current.pending_delivery))
            if current.recurring:
                current.pending_delivery = False
            else:
                removed.append(current)
                scheduled_jobs.pop(current.id)
        # 2. 保存持久状态；保存失败时恢复任务并重新入队。
        try:
            if any(job.durable for job, _ in changed):
                save_durable_jobs()
        except Exception:
            for job in removed:
                scheduled_jobs[job.id] = job
            for job, pending in changed:
                job.pending_delivery = pending
            restore_cron_jobs([job for job, _ in changed])
            raise


def restore_cron_jobs(jobs: list[CronJob]):
    """将未确认交付的任务重新加入队列，忽略已取消任务和已在队列中的任务。"""
    with cron_lock:
        # 1. 收集已经在队列中的 ID，作为本次恢复的去重集合。
        queued_ids = {job.id for job in cron_queue}
        # 2. 对仍存在的任务重新标记待交付，仅追加尚未排队的任务。
        for delivered in jobs:
            current = scheduled_jobs.get(delivered.id)
            if current is None:
                continue
            current.pending_delivery = True
            if current.id not in queued_ids:
                cron_queue.append(current)
                queued_ids.add(current.id)


def has_cron_queue() -> bool:
    """判断当前是否有等待交付的定时任务。"""
    with cron_lock:
        return bool(cron_queue)


# 供模型调用的三个定时工具
def run_schedule_cron(cron: str, prompt: str, recurring: bool = True, durable: bool = True) -> str:
    """创建定时任务，向模型返回任务编号、规则和 prompt，或错误说明。"""
    result = schedule_job(cron, prompt, recurring, durable)
    if isinstance(result, str):
        return f"Error: {result}"
    return f"Scheduled {result.id}: {cron} -> {prompt}"


def run_list_crons() -> str:
    """列出定时任务的编号、规则、prompt 摘要、周期选项和持久化选项。"""
    with cron_lock:
        # 先取得任务列表快照，后续用它生成展示文本。
        jobs = list(scheduled_jobs.values())
    if not jobs:
        return "No cron jobs."
    lines = []
    for job in jobs:
        # frequency 描述一次性／周期模式，storage 描述文件持久化／仅会话模式。
        frequency = "recurring" if job.recurring else "one-shot"
        storage = "durable" if job.durable else "session"
        lines.append(f"{job.id}: {job.cron} -> {job.prompt[:60]} [{frequency}, {storage}]")
    return "\n".join(lines)


def run_cancel_cron(job_id: str) -> str:
    """按任务编号取消定时任务，返回取消结果。"""
    return cancel_job(job_id)


# 模型可调用的三种定时操作；参数描述与内部 CronJob 字段对应。
TOOLS = [
    {"name": "schedule_cron", "description": "Schedule a prompt with a 5-field cron expression.",
     "input_schema": {"type": "object", "properties": {
         "cron": {"type": "string"}, "prompt": {"type": "string"},
         "recurring": {"type": "boolean"}, "durable": {"type": "boolean"}},
         "required": ["cron", "prompt"]}},
    {"name": "list_crons", "description": "List scheduled cron jobs.",
     "input_schema": {"type": "object", "properties": {}, "required": []}},
    {"name": "cancel_cron", "description": "Cancel a cron job by ID.",
     "input_schema": {"type": "object", "properties": {"job_id": {"type": "string"}}, "required": ["job_id"]}},
]
# 定时工具名称 -> 实际处理函数，注册到 Lead 的工具分发表中。
TOOL_HANDLERS = {
    "schedule_cron": run_schedule_cron, "list_crons": run_list_crons, "cancel_cron": run_cancel_cron,
}
