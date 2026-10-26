"""团队收件箱：将消息保存为 JSONL，并在消费时取出并删除对应收件箱文件。"""

import json
import re
import threading
import time

from . import tools


# 文件收件箱和消息等待通知
VALID_AGENT_NAME = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
# 运行时保留的角色名称，不能作为新队友的名称。
RESERVED_TEAMMATE_NAMES = {"lead", "agent"}


def is_valid_agent_name(name: str) -> bool:
    """检查名称是否由 1 至 64 个字母、数字、下划线或短横线组成。"""
    return isinstance(name, str) and bool(VALID_AGENT_NAME.fullmatch(name))


class MessageBus:
    """在线程间传递普通消息、结果和协议消息，收件箱位于工作目录 .mailboxes。"""

    def __init__(self):
        """建立收件箱读写锁和消息到达条件变量。"""
        self._lock = threading.RLock()  # 同一 MessageBus 内的收件箱读写锁。
        self._changed = threading.Condition(self._lock)  # send 发出通知，wait_for_messages 在此等待。

    def _path(self, agent: str):
        """取得指定收件人的 JSONL 路径，校验名称和目录范围。"""
        # 1. 校验收件人名称，确定 .mailboxes 下对应的消息文件。
        if not is_valid_agent_name(agent):
            raise ValueError(f"Invalid mailbox recipient: {agent!r}")
        root = tools.safe_path(".mailboxes")
        path = (root / f"{agent}.jsonl").resolve()
        # 2. 检查解析后的文件是否仍在邮箱目录中。
        if not path.is_relative_to(root):
            raise ValueError(f"Mailbox path escapes directory: {agent!r}")
        return path

    def _read_unlocked(self, agent: str) -> list[dict]:
        """在持有收件箱锁时取出全部消息，并删除已经消费的文件。"""
        # 1. 查找收件箱；尚未收到消息时返回空列表。
        inbox = self._path(agent)
        if not inbox.exists():
            return []
        # 2. 每行解析为一条消息，再删除已经全部消费的文件。
        messages = [json.loads(line) for line in inbox.read_text(encoding="utf-8").splitlines() if line.strip()]
        inbox.unlink()
        return messages

    def send(self, from_agent, to_agent, content, msg_type="message", metadata=None):
        """追加一条含发送者、类型、时间和元数据的消息，唤醒正在等消息的线程。"""
        # 1. 组装消息信封：from/to 是双方名称，type 是事件种类，ts 是发送时间。
        # content 为正文；metadata 为附加协议字段，例如 request_id、approve。
        message = {"from": from_agent, "to": to_agent, "content": content,
                   "type": msg_type, "ts": time.time(), "metadata": metadata or {}}
        # 2. 把消息追加为一行 JSON，再通知等待该总线消息的线程。
        with self._changed:
            path = self._path(to_agent)
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(message, ensure_ascii=False) + "\n")
            self._changed.notify_all()
        print(f"  [bus] {from_agent} -> {to_agent}: ({msg_type}) {content[:50]}")

    def read_inbox(self, agent: str) -> list[dict]:
        """取出指定收件人的全部消息；空收件箱返回空列表。"""
        with self._lock:
            return self._read_unlocked(agent)

    def peek(self, agent: str) -> bool:
        """检查是否有未消费消息，不读取或删除消息。"""
        with self._lock:
            inbox = self._path(agent)
            return inbox.exists() and inbox.stat().st_size > 0

    def wait_for_messages(self, agent: str, timeout: float | None = None) -> list[dict]:
        """等待消息到达后取出消息；等待超时返回空列表。"""
        # 1. deadline 是本次等待的截止时刻；None 表示不设置超时。
        deadline = None if timeout is None else time.monotonic() + timeout
        with self._changed:
            while not self.peek(agent):
                # 2. 邮箱为空时计算剩余等待秒数，到期就返回空列表。
                remaining = None if deadline is None else deadline - time.monotonic()
                if remaining is not None and remaining <= 0:
                    return []
                # 等待期间释放邮箱锁，收到通知后重新取得锁并检查邮箱。
                self._changed.wait(remaining)
            # 3. 邮箱已有消息，取出当前收到的全部消息。
            return self._read_unlocked(agent)


# Lead 和所有持久队友共用的消息总线。
BUS = MessageBus()
