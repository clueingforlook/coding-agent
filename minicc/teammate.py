"""持久队友：使用独立历史执行任务，在空闲时等待消息或认领下一项工作。"""

from copy import deepcopy
import json

from . import teams, tasks, tools
from .hooks import trigger_hooks
from .mailbox import BUS
from .model import request_model
from . import team_state as state


# 队友的基础工具、协作消息、计划提交和任务认领工具
TEAMMATE_TOOLS = deepcopy(tools.TOOLS[:5])
TEAMMATE_TOOLS[0]["input_schema"]["properties"].pop("run_in_background", None)
TEAMMATE_TOOLS.extend([
    {"name": "send_message", "description": "Send an intermediate message to 'lead' or an active teammate.",
     "input_schema": {"type": "object", "properties": {"to": {"type": "string"}, "content": {"type": "string"}}, "required": ["to", "content"]}},
    {"name": "submit_plan", "description": "Submit a work plan for Lead approval.",
     "input_schema": {"type": "object", "properties": {"plan": {"type": "string"}}, "required": ["plan"]}},
    *[tool for tool in tasks.TOOLS if tool["name"] in {"list_tasks", "claim_task", "complete_task"}],
])


def _run_teammate_tool(name, call, handlers):
    """检查队友的计划状态和权限，执行工具并返回与本次调用对应的结果文字。"""
    # tool_name 是模型请求的工具名，arguments 是解析后的参数字典。
    tool_name, arguments = call.get("function", {}).get("name", ""), {}
    try:
        tool_name = call["function"]["name"]
        arguments = json.loads(call["function"]["arguments"])
        if state.TEAM_STOP.is_set():
            return "Error: Team runtime is stopping"
        # 1. gate 是当前队友的计划状态；需要审批时，修改型工具在此返回阻止说明。
        gate = state.plan_gates.get(name, "not_required")
        if tool_name in {"bash", "write_file", "edit_file"} and gate not in {"not_required", "approved"}:
            return f"Blocked: plan status is {gate}. Submit or revise the plan and wait for approval."
        # 2. 执行工具前触发权限等钩子；非 None 返回值作为本次调用的阻止结果。
        blocked = trigger_hooks("PreToolUse", tool_name, arguments)
        if blocked is not None:
            return str(blocked)
        # 3. 从该队友的工具表中找到执行函数，返回正常输出或错误文字。
        handler = handlers.get(tool_name)
        output = str(handler(**arguments)) if handler else f"Unknown tool: {tool_name}"
    except Exception as error:
        output = f"Error: {type(error).__name__}: {error}"
    # 成功执行或执行失败后，触发工具完成回调。
    trigger_hooks("PostToolUse", tool_name, arguments, output)
    return output


class TeammateRuntime:
    """保存一个队友的模型配置、独立对话及工具处理函数，循环执行 WORK 和 IDLE。"""

    def __init__(self, name, role, prompt, task_id, require_plan, client, model, thinking=""):
        """建立队友身份和独立消息，加入初始任务描述及计划要求。"""
        # 1. 设置队友身份：name 是收件箱与任务负责人名称，role 是提示词中的职责说明。
        self.name = name
        # client 为共享 HTTP 客户端，model 为模型名称，thinking 为本队友的思考模式配置。
        self.client, self.model, self.thinking = client, model, thinking
        # system 是本队友每次模型请求使用的系统提示词，包含工具使用和团队协作规则。
        self.system = (
            f"You are '{name}', a {role}. Environment: {tools.ENVIRONMENT_PROMPT}. "
            "Use tools to complete the assigned Task, then call complete_task and report a concise result. "
            "If the first user message contains [Assigned task], that Task is already claimed; do not claim it again. "
            "When asked for a plan, call submit_plan and wait for approval before bash or file changes. "
            "File and shell tools use the Task's working directory; that directory is not a sandbox. "
            "The runtime delivers your final text to Lead. Use send_message only for intermediate coordination, "
            "and address the coordinator as 'lead'."
        )
        # 2. messages 是该队友独立的对话历史，初始内容是 Lead 传入的 prompt。
        # task_id 对应启动流程已经认领的任务；这里补充任务标题、描述和工作目录。
        self.messages = [{"role": "user", "content": prompt}]
        if task_id:
            task = tasks.load_task(task_id)
            cwd = tasks.assignment_cwd(name)
            self.messages[0]["content"] += f"\n\n[Assigned task {task.id}] {task.subject}\n{task.description}\nWork directory: {cwd}"
        # require_plan 表示启动后先提交计划；此处把要求加入初始对话。
        if require_plan:
            self.messages[0]["content"] += "\n\n[Plan required] Submit a plan and wait for Lead approval before changing files or using bash."
        # 3. handlers 将工具名称映射到本队友的执行函数，任务操作使用该队友自身名称。
        self.handlers = {
            "bash": self.bash, "read_file": self.read, "write_file": self.write,
            "edit_file": self.edit, "glob": self.glob,
            "send_message": lambda to, content: teams._teammate_send_message(name, to, content),
            "submit_plan": lambda plan: teams._teammate_submit_plan(name, plan),
            "list_tasks": tasks.run_list_tasks, "claim_task": self.claim, "complete_task": self.complete,
        }

    def current_cwd(self):
        """返回队友当前任务目录；未认领任务或目录绑定无效时返回错误说明。"""
        try:
            # 1. 从任务记录取得或恢复队友目录；cwd 是当前任务实际使用的 Path。
            cwd = tasks.assignment_cwd(self.name)
            # 2. 未取得任务目录记录时返回未认领错误；已认领时返回目录和空错误。
            if self.name not in state.teammate_assignments:
                return None, "Error: Claim a Task before using workspace tools."
            return cwd, None
        except (FileNotFoundError, ValueError) as error:
            return None, f"Error: Invalid task assignment: {error}"

    def bash(self, command):
        """在队友当前任务目录同步执行命令。"""
        cwd, error = self.current_cwd()
        return error or tools.run_bash(command, cwd=cwd)

    def read(self, path, limit=None):
        """读取队友当前任务目录内的文件。"""
        cwd, error = self.current_cwd()
        return error or tools.run_read(path, limit=limit, cwd=cwd)

    def write(self, path, content):
        """写入队友当前任务目录内的文件。"""
        cwd, error = self.current_cwd()
        return error or tools.run_write(path, content, cwd=cwd)

    def edit(self, path, old_text, new_text):
        """修改队友当前任务目录内的指定文本。"""
        cwd, error = self.current_cwd()
        return error or tools.run_edit(path, old_text, new_text, cwd=cwd)

    def glob(self, pattern):
        """在队友当前任务目录中查找匹配的路径。"""
        cwd, error = self.current_cwd()
        return error or tools.run_glob(pattern, cwd=cwd)

    def claim(self, task_id):
        """以队友自己的名称认领一个共享任务。"""
        return tasks.claim_task(task_id, owner=self.name)

    def complete(self, task_id):
        """以队友自己的名称完成当前任务，返回完成结果。"""
        return tasks.complete_task(task_id, owner=self.name)

    def handle_inbox(self, inbox):
        """将工作消息加入对话，应用计划回复；收到有效关机请求时返回 True。"""
        # work_messages 收集需要交给模型继续处理的文字，稍后合并为一条 user 消息。
        work_messages = []
        for message in inbox:
            kind = message.get("type", "message")
            # 1. 关机请求通过校验后立即回复确认，并通知外层结束队友循环。
            if kind == "shutdown_request":
                accepted, notice = teams.apply_shutdown_request(self.name, message)
                if not accepted:
                    work_messages.append(notice)
                    continue
                BUS.send(self.name, "lead", "Shutdown acknowledged.", "shutdown_response",
                         {"request_id": notice, "approve": True})
                return True
            # 2. 审批回复更新计划闸门；计划要求和普通消息转换为模型可读的说明。
            if kind == "plan_approval_response":
                _, notice = teams.apply_plan_response(self.name, message)
                work_messages.append(notice)
            elif kind == "plan_request":
                work_messages.append(f"[Plan required] {message['content']}")
            else:
                work_messages.append(f"[Message from {message['from']}] {message['content']}")
        # 3. 把本次积累的工作说明追加到队友历史，供下一次模型请求读取。
        if work_messages:
            self.messages.append({"role": "user", "content": "\n".join(work_messages)})
        return False

    def work(self):
        """执行一次模型请求和工具处理，返回 continue、idle 或 stop 状态。"""
        # 1. 先处理收件箱，再向模型请求本轮动作。
        if self.handle_inbox(BUS.read_inbox(self.name)) or state.TEAM_STOP.is_set():
            return "stop"
        with state.team_lock:
            state.active_teammates[self.name] = "working"
        try:
            message = request_model(self.client, self.model, self.system, self.messages, TEAMMATE_TOOLS, self.thinking)
        except Exception as error:
            BUS.send(self.name, "lead", f"{type(error).__name__}: {error}", "error")
            return "stop"
        if state.TEAM_STOP.is_set():
            return "stop"
        self.messages.append(message)
        # 2. 执行本批工具，将每个结果按 tool_call_id 回传到队友对话。
        # calls 是当前回复中的全部工具请求；有工具时处理完后仍处于 WORK 阶段。
        calls = message.get("tool_calls") or []
        if calls:
            self.messages.extend({"role": "tool", "tool_call_id": call["id"],
                                  "content": _run_teammate_tool(self.name, call, self.handlers)} for call in calls)
            return "continue"
        # 3. 把文字结果交给 Lead，再报告待审批或空闲状态。
        content = message.get("content")
        summary = content.strip() if isinstance(content, str) else ""
        # gate 决定结束本轮后是等待审批，还是报告结果并进入普通空闲状态。
        gate = state.plan_gates.get(self.name, "not_required")
        if gate != "pending" and summary:
            BUS.send(self.name, "lead", summary, "result")
        # 待审批时保留当前任务目录；普通空闲时释放已完成任务的目录，并单独报告 idle。
        if gate == "pending":
            with state.team_lock:
                state.active_teammates[self.name] = "waiting_approval"
        else:
            tasks.release_completed_assignment(self.name)
            with state.team_lock:
                state.active_teammates[self.name] = "idle"
            BUS.send(self.name, "lead", "Waiting for more work.", "idle_notification")
        return "idle"

    def wait_for_work(self):
        """等待直接消息；没有消息时扫描并认领就绪任务，收到关机请求时返回 False。"""
        # 1. 持续等待消息，每次超时后才检查任务板。
        while not state.TEAM_STOP.is_set():
            inbox = BUS.wait_for_messages(self.name, teams.IDLE_SCAN_INTERVAL)
            if inbox:
                # before 是处理消息前的历史长度；追加了工作说明就表示可以重新进入 WORK。
                before = len(self.messages)
                if self.handle_inbox(inbox):
                    return False
                if len(self.messages) > before:
                    return True
                continue
            if state.TEAM_STOP.is_set():
                return False
            # 2. 没有消息且未要求停止时，尝试认领一项 ready task。
            task = teams.claim_next_task(self.name)
            if task:
                cwd = tasks.assignment_cwd(self.name)
                # 3. 把新任务的 ID、标题、描述和目录加入对话，通知外层开始工作。
                self.messages.append({"role": "user", "content":
                    f"[Auto-claimed task {task.id}] {task.subject}\n{task.description}\nWork directory: {cwd}"})
                print(f"  [idle] {self.name} claimed {task.id}: {task.subject}")
                return True
        return False

    def run(self):
        """持续执行 WORK／IDLE 循环，退出时归还未完成任务并移除队友注册信息。"""
        try:
            # phase 控制循环：continue 表示继续 WORK，idle 表示等待工作，stop 表示退出。
            phase = "continue"
            # 1. 在 WORK 和 IDLE 之间切换，直到队友收到停止要求。
            while phase != "stop" and not state.TEAM_STOP.is_set():
                if phase == "idle" and not self.wait_for_work():
                    break
                phase = self.work()
        # 2. 未处理的异常作为 error 事件发送给 Lead。
        except Exception as error:
            BUS.send(self.name, "lead", f"{type(error).__name__}: {error}", "error")
        # 3. 退出时归还未完成任务，再清除活跃状态、计划关联和线程登记。
        finally:
            try:
                tasks.release_teammate_assignment(self.name)
            except Exception as error:
                print(f"[team] assignment cleanup failed for {self.name}: {error}")
            with state.team_lock:
                state.active_teammates.pop(self.name, None)
                state.plan_gates.pop(self.name, None)
                state.plan_request_ids.pop(self.name, None)
                state.teammate_threads.pop(self.name, None)
            print(f"  [teammate] {self.name} finished")
