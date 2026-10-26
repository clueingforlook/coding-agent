"""长期记忆：保存用户偏好和项目事实，在后续对话中加载相关内容。"""

import json
from pathlib import Path
import re
from tempfile import TemporaryDirectory

import yaml

from . import tools
from .model import request_model
from .skills import _parse_frontmatter, build_system as build_skill_system


# 记忆文件目录、记忆分类和触发合并整理的文件数量。
MEMORY_DIR = tools.WORKDIR / ".memory"
# 记忆类型：user 用户偏好，feedback 做事反馈，project 项目事实，reference 外部资料入口。
MEMORY_TYPES = ["user", "feedback", "project", "reference"]
# 记忆文件数量达到此值时，回合结束后的整理流程会尝试合并记忆。
CONSOLIDATE_THRESHOLD = 10


def _memory_path(filename):
    """取得记忆文件的绝对路径；文件必须位于记忆目录内且扩展名为 .md。"""
    path = (MEMORY_DIR / filename).resolve()
    if path.parent != MEMORY_DIR.resolve() or path.suffix.lower() != ".md":
        raise ValueError(f"Invalid memory path: {filename}")
    return path


def _prepare_memory(name, mem_type, description, body):
    """检查一条记忆的字段，返回文件路径和完整 Markdown 文本。"""
    # 检查名称、类型、描述和正文是否有效。
    if not all(isinstance(value, str) and value.strip() for value in (name, mem_type, description, body)):
        raise ValueError("Memory fields must be non-empty strings")
    if mem_type not in MEMORY_TYPES:
        raise ValueError(f"Unknown memory type: {mem_type}")
    # 将记忆名称转换为可在 Windows 中使用的文件名。
    # slug 是记忆名称转换后的文件名主体；真正的内容名称仍保存在文件头中。
    slug = re.sub(r"[^\w-]+", "-", name.lower()).strip("-_")[:80]
    if not slug:
        raise ValueError("Memory name has no usable filename")
    if re.fullmatch(r"memory|con|prn|aux|nul|com[1-9]|lpt[1-9]", slug):
        slug = "memory-" + slug
    # meta 是写入 YAML 文件头的字段字典，body 是文件头之后的 Markdown 正文。
    meta = {"name": name, "description": description, "type": mem_type}
    # 组装文件头的名称、描述、类型，再接上记忆正文。
    frontmatter = yaml.safe_dump(meta, allow_unicode=True, sort_keys=False).strip()
    return _memory_path(f"{slug}.md"), f"---\n{frontmatter}\n---\n\n{body}\n"


def write_memory_file(name, mem_type, description, body):
    """保存一条记忆并更新索引，返回保存后的文件路径；同名记忆会被更新。"""
    # 1. 准备目标路径和完整文件文本，创建记忆目录并写入文件。
    path, text = _prepare_memory(name, mem_type, description, body)
    MEMORY_DIR.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    # 2. 重新生成记忆索引，让后续提示词能够看到本次保存的条目。
    _rebuild_index()
    return path


def _rebuild_index():
    """重新生成 MEMORY.md，每行列出一条记忆的名称、文件链接和描述。"""
    # lines 保存索引的每一行；每行由名称、相对文件链接和单行描述组成。
    lines = []
    for memory in list_memory_files():
        name = " ".join(memory["name"].split())
        desc = " ".join(memory["description"].split())
        lines.append(f"- [{name}]({memory['filename']}) — {desc}")
    # 将收集到的索引行写入 MEMORY.md；没有记忆时写入空索引。
    MEMORY_DIR.mkdir(parents=True, exist_ok=True)
    _memory_path("MEMORY.md").write_text("\n".join(lines) + "\n" if lines else "", encoding="utf-8")


def read_memory_index():
    """读取记忆索引文本；索引文件不存在时返回空字符串。"""
    path = _memory_path("MEMORY.md")
    return path.read_text(encoding="utf-8-sig").strip() if path.is_file() else ""


def read_memory_file(filename):
    """读取指定记忆文件的完整内容；文件不存在时返回 None。"""
    path = _memory_path(filename)
    return path.read_text(encoding="utf-8-sig") if path.is_file() else None


def list_memory_files():
    """列出所有记忆的文件名、名称、描述、类型和正文，不包含索引文件。"""
    # 1. result 保存每条记忆的文件名、名称、描述、类型和正文，跳过 MEMORY.md。
    result = []
    for path in sorted(MEMORY_DIR.glob("*.md")):
        if path.name.lower() == "memory.md" or not path.is_file():
            continue
        raw = read_memory_file(path.name)
        # 2. 解析文件头和正文，再整理为供选择、提取和合并流程使用的条目。
        meta, body = _parse_frontmatter(raw)
        result.append({
            "filename": path.name,
            "name": str(meta.get("name") or path.stem),
            "description": str(meta.get("description") or body.split("\n")[0][:80]),
            "type": str(meta.get("type") or "user"),
            "body": body,
        })
    return result


def _query_json(prompt, client, model, thinking, max_tokens):
    """向模型发起一次记忆处理请求，将回复中的 JSON 数组解析为列表。"""
    # 本次请求只包含记忆处理指令，没有可调用的工具。
    response = request_model(
        client, model, "You manage persistent memories for a coding agent.",
        [{"role": "user", "content": prompt}], [], thinking, max_tokens=max_tokens,
    )
    # 从回复文字中取出数组；没有数组或格式错误时抛出异常。
    match = re.search(r"\[.*\]", response.get("content") or "", re.DOTALL)
    if not match:
        raise ValueError("Memory response has no JSON array")
    items = json.loads(match.group())
    if not isinstance(items, list):
        raise ValueError("Memory response must be an array")
    return items


def select_relevant_memories(messages, client, model, thinking="", max_items=5):
    """根据最近的用户提问选择相关记忆，返回最多 max_items 个文件名。"""
    # 取得记忆清单，以及最近三条用户文字的前 2000 个字符。
    # files 是磁盘记忆目录的条目列表；模型返回的数字索引对应这个列表的位置。
    files = list_memory_files()
    if not files or max_items <= 0:
        return []
    # 记忆选择跳过后台通知和团队事件
    recent_texts = [msg["content"] for msg in messages
                    if msg.get("role") == "user" and isinstance(msg.get("content"), str)
                    and not msg["content"].startswith(("<task_notification>", "[Team events]"))]
    recent = " ".join(recent_texts[-3:])[:2000]
    if not recent.strip():
        return []
    # 给每条记忆编号，让模型根据名称和描述选择需要加载的条目。
    catalog = "\n".join(f"{i}: {item['name']} — {item['description']}" for i, item in enumerate(files))
    prompt = (
        "Given the recent conversation and the memory catalog below, "
        "select the indices of memories that are clearly relevant. "
        "Return ONLY a JSON array of integers, e.g. [0, 3]. "
        "If none are relevant, return [].\n\n"
        f"Recent conversation:\n{recent}\n\nMemory catalog:\n{catalog}"
    )
    try:
        indices = _query_json(prompt, client, model, thinking, 200)
        # 将有效编号转换为文件名，去掉重复项并限制返回数量。
        # selected 只保存最终选择的文件名，不保存正文；load_memories 再根据文件名读取全文。
        selected = []
        for index in indices:
            if type(index) is int and 0 <= index < len(files):
                filename = files[index]["filename"]
                if filename not in selected:
                    selected.append(filename)
                if len(selected) >= max_items:
                    break
        return selected
    except Exception:
        # 模型选择失败时，改用用户文字中的关键词匹配记忆名称和描述。
        keywords = [word.lower() for word in recent.split() if len(word) > 3]
        return [item["filename"] for item in files
                if any(word in (item["name"] + " " + item["description"]).lower()
                       for word in keywords)][:max_items]


def load_memories(messages, client, model, thinking=""):
    """读取选中记忆的全文，组装为供当前请求使用的 relevant_memories 文本。"""
    selected = select_relevant_memories(messages, client, model, thinking)
    if not selected:
        return ""
    # parts 是选中文件的完整内容；最终组合为一段 relevant_memories 文本。
    parts = [read_memory_file(filename) for filename in selected]
    return "<relevant_memories>\n\n" + "\n\n".join(part for part in parts if part) + "\n\n</relevant_memories>"


def build_system():
    """生成父 Agent 的系统提示词，包含环境、技能目录、记忆索引和使用说明。"""
    # 读取当前索引，作为系统提示词中的记忆清单。
    index = read_memory_index()
    # section 是将 MEMORY.md 索引放入系统提示词的文本段，不包含每条记忆的全文。
    section = f"\n\nMemories available:\n{index}" if index else ""
    return (
        build_skill_system() + section + "\n"
        "Relevant memories may be injected into the current request. "
        "Respect user preferences from memory; follow the current user if they conflict.\n"
        "When the user says 'remember' or expresses a clear preference, "
        "it can be extracted as a memory after this turn."
    )


def inject_memories(messages, memories_content, memory_turn):
    """返回带记忆正文的请求消息列表；memory_turn 是本回合原始的用户消息。"""
    if not memories_content:
        return messages
    # request_messages 是本次请求使用的列表副本；找到目标用户消息后替换该位置的消息字典。
    request_messages = messages.copy()
    # 找到本回合用户消息，把记忆正文放到问题前面；历史中的原消息保持原样。
    for index, message in enumerate(messages):
        if message is memory_turn:
            request_messages[index] = {**message, "content": memories_content + "\n\n" + message["content"]}
            break
    else:
        # 原用户消息已被压缩时，在请求开头新增一条记忆消息。
        request_messages.insert(0, {"role": "user", "content": memories_content})
    return request_messages


def extract_memories(messages, client, model, thinking=""):
    """在父回合结束后，从对话中提取新的长期信息并保存为记忆文件。"""
    # 1. 收集最近十条用户或模型的普通文字，作为待提取的对话。
    # 长期记忆提取跳过后台通知和团队事件
    dialogue_parts = [f"{msg['role']}: {msg['content']}" for msg in messages
                      if msg.get("role") in ("user", "assistant")
                      and isinstance(msg.get("content"), str) and msg["content"].strip()
                      and not msg["content"].startswith(("<task_notification>", "[Team events]"))]
    dialogue = "\n".join(dialogue_parts[-10:])
    if not dialogue.strip():
        return
    try:
        # 2. 将已有记忆清单和对话一起交给模型，要求只提取新的偏好、约束或事实。
        existing = list_memory_files()
        # existing_desc 是已有记忆的名称和描述清单，与待提取对话一起发送给模型。
        existing_desc = "\n".join(f"- {item['name']}: {item['description']}" for item in existing) or "(none)"
        prompt = (
            "Extract user preferences, constraints, or project facts from this dialogue.\n"
            "Return a JSON array. Each item: {name, type, description, body}.\n"
            "- name: short kebab-case identifier (e.g. 'user-preference-tabs')\n"
            "- type: one of 'user' (user preference), 'feedback' (guidance), "
            "'project' (project fact), 'reference' (external pointer)\n"
            "- description: one-line summary for index lookup\n"
            "- body: full detail in markdown\n"
            "If nothing new or already covered by existing memories, return [].\n\n"
            f"Existing memories:\n{existing_desc}\n\nDialogue:\n{dialogue[:4000]}"
        )
        items = _query_json(prompt, client, model, thinking, 800)
        # 3. 逐条保存模型返回的记忆，并统计本次写入数量。
        count = 0
        for item in items:
            write_memory_file(item["name"], item["type"], item["description"], item["body"])
            count += 1
        if count:
            print(f"\n\033[33m[Memory: extracted {count} new memories]\033[0m")
    except Exception as error:
        # 提取或保存失败时输出提示，结束本次记忆提取。
        print(f"[Memory: extraction skipped ({type(error).__name__})]")


def consolidate_memories(client, model, thinking=""):
    """请求模型合并重复或过时的记忆，并用整理结果更新文件和索引。"""
    try:
        # 1. 检查记忆数量；达到整理阈值后，收集全部记忆内容。
        files = list_memory_files()
        if len(files) < CONSOLIDATE_THRESHOLD:
            return
        # catalog 包含本次所有记忆的文件名、元信息和正文，是交给模型整理的完整输入。
        catalog = "\n\n".join(
            f"## {item['filename']}\nname: {item['name']}\ntype: {item['type']}\n"
            f"description: {item['description']}\n{item['body']}" for item in files
        )
        # 待整理内容超过 16000 个字符时，跳过本次整理。
        if len(catalog) > 16000:
            print("[Memory: consolidation skipped (catalog exceeds 16000 characters)]")
            return
        # 2. 请求模型合并重复条目、去除过时内容，返回新的记忆列表。
        prompt = (
            "Consolidate the following memory files. Rules:\n"
            "1. Merge duplicates into one\n"
            "2. Remove outdated/contradicted memories\n"
            "3. Keep the total under 30 memories\n"
            "4. Preserve important user preferences above all\n"
            "Return a JSON array. Each item: {name, type, description, body}.\n\n" + catalog
        )
        items = _query_json(prompt, client, model, thinking, 3000)
        if not items:
            return  # 整理结果为空时，保留现有文件并结束。
        # 3. 检查每条新记忆的字段和文件名，重复文件名会被判为错误。
        # prepared 是通过校验的 (目标文件路径, 完整文件文本) 列表。
        prepared = [_prepare_memory(item["name"], item["type"], item["description"], item["body"])
                    for item in items]
        # names 是新文件名忽略大小写后的集合，用于检测冲突和识别要删除的旧文件。
        names = {path.name.casefold() for path, _ in prepared}
        if len(names) != len(prepared):
            raise ValueError("Consolidated memory filenames collide")
        # 4. 写入全部临时文件，再将它们替换为正式记忆文件。
        with TemporaryDirectory(prefix="consolidate-", dir=MEMORY_DIR) as folder:
            # staging 是本次合并的临时目录，先存放全部新文件，再替换到正式记忆目录。
            staging = Path(folder)
            for path, text in prepared:
                (staging / path.name).write_text(text, encoding="utf-8")
            for path, _ in prepared:
                (staging / path.name).replace(path)
            # 5. 删除未出现在整理结果中的旧记忆。
            for item in files:
                if item["filename"].casefold() not in names:
                    _memory_path(item["filename"]).unlink()
        # 6. 更新索引，并显示整理前后的记忆数量。
        _rebuild_index()
        print(f"\n\033[33m[Memory: consolidated {len(files)} → {len(items)} memories]\033[0m")
    except Exception as error:
        print(f"[Memory: consolidation skipped ({type(error).__name__})]")
