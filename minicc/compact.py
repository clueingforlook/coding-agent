"""上下文压缩：减少发送给模型的历史内容，并保存需要留存的大输出和快照。"""

import json
import re
import time
from uuid import uuid4

import httpx

from . import tools
from .model import request_model


# 压缩条件按字符量估算。
CONTEXT_LIMIT = 50000  # 历史超过此大小时，调用模型生成摘要。
KEEP_RECENT = 3  # 保持全文的最近工具结果数量。
PERSIST_THRESHOLD = 30000  # 预算处理时，可以转存到文件的单条结果大小门槛。


def estimate_size(messages):
    """估算消息历史的字符量，供自动摘要的触发条件使用。"""
    return len(str(messages))


def _tool_group_start(messages, index):
    """若边界位于工具结果中，将它前移到这一批工具调用的 assistant 消息处。"""
    while index > 0 and messages[index].get("role") == "tool":
        index -= 1
    return index


def snip_compact(messages, max_messages=50):
    """历史条数超限时，保留开头和结尾，返回中间部分被裁剪提示替换的列表。"""
    if len(messages) <= max_messages:
        return messages
    # head_end 是头部保留范围的右边界，不包含该位置；tail_start 是尾部保留范围的起点。
    # 默认先按开头 3 条、结尾 47 条划分，再调整边界处的工具消息组。
    head_end, tail_start = 3, len(messages) - (max_messages - 3)
    # 调整前后保留范围，将边界处的工具调用和全部对应结果一起保留。
    while head_end < len(messages) and messages[head_end].get("role") == "tool":
        head_end += 1
    tail_start = _tool_group_start(messages, tail_start)
    if head_end >= tail_start:
        return messages
    # 用一条裁剪提示替换中间删除的消息。
    snipped = tail_start - head_end
    return [*messages[:head_end], {"role": "user", "content": f"[snipped {snipped} messages]"}, *messages[tail_start:]]


def collect_tool_results(messages):
    """按出现顺序收集历史中的全部工具结果消息。"""
    return [msg for msg in messages if msg.get("role") == "tool"]


def micro_compact(messages):
    """保留最近三个工具结果的全文，将更早且超过 120 字符的结果改为占位说明。"""
    # tool_results 引用原历史中的工具消息字典；替换其 content 会直接更新 messages。
    tool_results = collect_tool_results(messages)
    for result in tool_results[:-KEEP_RECENT]:
        if len(result.get("content", "")) > 120:
            result["content"] = "[Earlier tool result compacted. Re-run if needed.]"
    return messages


def persist_large_output(tool_use_id, output):
    """将超过大小门槛的工具输出保存为文件，返回文件路径和预览；短输出原样返回。"""
    if len(output) <= PERSIST_THRESHOLD:
        return output
    # 大输出保存到 .task_outputs；返回文本包含文件路径和预览，供模型后续读取。
    directory = tools.safe_path(".task_outputs")
    directory.mkdir(parents=True, exist_ok=True)
    # 生成输出文件名，保存全文，并在返回文本中附上前 2000 个字符。
    safe_id = re.sub(r"[^a-zA-Z0-9_-]", "_", tool_use_id)[:80] or "tool"
    path = directory / f"{safe_id}_{uuid4().hex}.txt"
    path.write_text(output, encoding="utf-8")
    return f"<persisted-output>\nFull output: {path}\nPreview:\n{output[:2000]}\n</persisted-output>"


def tool_result_budget(messages, max_bytes=200_000):
    """检查最新一批工具结果的总量，超限时将其中的大结果替换为文件预览。

    max_bytes 在当前实现中按字符数计算；直接更新并返回传入的 messages。
    """
    # 从历史末尾收集最新一批连续的工具结果。
    # results 只收集历史末尾这一批工具结果，不包含更早的工具批次。
    results = []
    for message in reversed(messages):
        if message.get("role") != "tool":
            break
        results.append(message)
    # 统计本批结果的总长度，从最大的结果开始处理，直到满足预算或处理完毕。
    total = sum(len(str(result.get("content", ""))) for result in results)
    for result in sorted(results, key=lambda msg: len(str(msg.get("content", ""))), reverse=True):
        if total <= max_bytes:
            break
        content = str(result.get("content", ""))
        if len(content) <= PERSIST_THRESHOLD:
            continue
        result["content"] = persist_large_output(result["tool_call_id"], content)
        # 按替换前后长度差更新本批总量，再决定是否需要继续处理下一条结果。
        total += len(result["content"]) - len(content)
    return messages


def write_transcript(messages):
    """将当前消息列表保存为 JSONL 快照，每行一条消息，返回快照文件路径。"""
    directory = tools.safe_path(".transcripts")
    directory.mkdir(parents=True, exist_ok=True)
    # path 是带时间戳的 JSONL 快照文件；保存的是此时仍在 messages 中的内容。
    path = directory / f"transcript_{time.time_ns()}.jsonl"
    with path.open("w", encoding="utf-8") as stream:
        for message in messages:
            stream.write(json.dumps(message, ensure_ascii=False, default=str) + "\n")
    return path


def summarize_history(messages, client, model, thinking="", focus=""):
    """请求模型总结当前目标、结论、文件变更、剩余工作和约束，返回摘要文字。"""
    # 取历史文本的前 80000 个字符，作为本次摘要的材料。
    conversation = json.dumps(messages, ensure_ascii=False, default=str)[:80000]
    prompt = (
        "Summarize this coding-agent conversation so work can continue.\n"
        "Preserve: 1. current goal, 2. key findings/decisions, 3. files read/changed, "
        "4. remaining work, 5. user constraints.\nBe compact but concrete.\n"
    )
    # focus 指定额外需要重点保留的内容。
    if focus:
        prompt += f"Focus on: {focus}\n"
    # 向模型发送摘要要求和历史材料；response 是摘要请求返回的 assistant 消息。
    response = request_model(
        client, model, "You summarize coding-agent conversations.",
        [{"role": "user", "content": prompt + "\n" + conversation}], [], thinking,
        max_tokens=2000,
    )
    summary = (response.get("content") or "").strip()
    if not summary:
        # 模型未返回有效摘要时，报告摘要失败。
        raise ValueError("Compaction returned an empty summary")
    return summary


def compact_history(messages, client, model, thinking="", focus=""):
    """保存当前历史快照，生成摘要，返回只包含摘要的新消息列表。"""
    # transcript 是当前历史快照的文件路径，summary 是随后生成的续接摘要。
    transcript = write_transcript(messages)
    print(f"[transcript saved: {transcript}]")
    summary = summarize_history(messages, client, model, thinking, focus)
    return [{"role": "user", "content": f"[Compacted]\n\n{summary}"}]


def reactive_compact(messages, client, model, thinking=""):
    """处理上下文超长错误，返回摘要与最近五条消息组成的新历史。"""
    # 先保存当前历史并生成摘要，再从历史末尾选取需要继续保留的近期消息。
    transcript = write_transcript(messages)
    print(f"[transcript saved: {transcript}]")
    summary = summarize_history(messages, client, model, thinking)
    # 最近消息的起点若在工具结果中，向前补齐整批工具调用。
    tail_start = _tool_group_start(messages, max(0, len(messages) - 5)) if messages else 0
    return [{"role": "user", "content": f"[Reactive compact]\n\n{summary}"}, *messages[tail_start:]]


def is_context_error(error):
    """检查异常文字和 HTTP 错误正文，判断是否属于上下文过长错误。"""
    text = str(error)
    if isinstance(error, httpx.HTTPStatusError):
        text += " " + error.response.text
    return any(marker in text.lower() for marker in (
        "prompt_too_long", "too many tokens", "context_length_exceeded", "maximum context length",
    ))


# 模型可主动请求压缩，并用 focus 指定摘要重点。
COMPACT_TOOL = {
    "name": "compact",
    "description": "Summarize earlier conversation to free context space.",
    "input_schema": {"type": "object", "properties": {"focus": {"type": "string"}}},
}
