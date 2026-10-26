"""技能加载：启动时建立技能目录，模型需要某项技能时再取得完整说明。"""

import yaml

from . import tools


# 技能文件所在目录，以及保存技能名称、描述和全文的注册表。
SKILLS_DIR = tools.WORKDIR / "skills"
# 技能名称 -> {name: 注册名称, description: 简短用途, content: SKILL.md 完整内容}。
SKILL_REGISTRY: dict[str, dict] = {}


def _parse_frontmatter(text: str) -> tuple[dict, str]:
    """拆分文件头部的 YAML 元信息与正文，返回 (元信息字典, 正文)。"""
    # 没有完整文件头时，将全文作为正文返回。
    if not text.startswith("---"):
        return {}, text
    # parts[1] 为两条 --- 之间的 YAML 文件头，parts[2] 为后续正文。
    parts = text.split("---", 2)
    if len(parts) < 3:
        return {}, text
    try:
        meta = yaml.safe_load(parts[1]) or {}
    except yaml.YAMLError:
        meta = {}
    # 解析失败或元信息不是字典时，使用空元信息。
    if not isinstance(meta, dict):
        meta = {}
    return meta, parts[2].strip()


def _scan_skills():
    """扫描 skills 下各直属子目录中的 SKILL.md，更新内存中的技能注册表。"""
    # 清除旧注册信息；技能目录不存在时结束扫描。
    SKILL_REGISTRY.clear()
    if not SKILLS_DIR.exists():
        return
    # 逐个检查子目录，读取其中的技能说明文件。
    for d in sorted(SKILLS_DIR.iterdir()):
        if not d.is_dir():
            continue
        # manifest 是当前子目录中的技能说明文件路径，raw 保存读取到的原文。
        manifest = d / "SKILL.md"
        if manifest.exists():
            # 读取技能文件全文，并提取文件头中的名称和描述。
            raw = manifest.read_text(encoding="utf-8-sig")
            meta, body = _parse_frontmatter(raw)
            # 名称缺失或无效时使用目录名；描述缺失时使用文件首行。
            name = meta.get("name", d.name)
            if not isinstance(name, str) or not name.strip():
                name = d.name
            desc = meta.get("description", raw.split("\n")[0].lstrip("#").strip())
            # 以技能名称为键，缓存名称、描述和完整文件内容。
            SKILL_REGISTRY[name] = {"name": name, "description": desc, "content": raw}


def list_skills() -> str:
    """生成只含名称和描述的技能清单，供系统提示词使用。"""
    if not SKILL_REGISTRY:
        return "(no skills found)"
    return "\n".join(f"- **{s['name']}**: {s['description']}" for s in SKILL_REGISTRY.values())


def build_system() -> str:
    """生成包含工作目录、命令环境、技能清单和技能加载说明的系统提示词。"""
    # catalog 是仅含名称和描述的技能清单；正文等 load_skill 被调用后才交给模型。
    catalog = list_skills()
    return (
        f"You are a coding agent at {tools.WORKDIR}. "
        f"Environment: {tools.ENVIRONMENT_PROMPT}. "
        f"Skills available:\n{catalog}\n"
        "Use load_skill to get full details when needed."
    )


def load_skill(name: str) -> str:
    """按注册名称返回技能全文；未找到时返回技能不存在的说明。"""
    skill = SKILL_REGISTRY.get(name)
    if not skill:
        return f"Skill not found: {name}"
    return skill["content"]


# 模型通过 load_skill 提交技能名称，取得该技能的完整说明。
LOAD_SKILL_TOOL = {
    "name": "load_skill",
    "description": "Load the full content of a skill by name.",
    "input_schema": {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]},
}
