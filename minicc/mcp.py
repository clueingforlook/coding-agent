"""MCP 工具接入：模拟服务连接、工具发现、名称转换和调用分发。"""

from collections.abc import Callable
import re


# --- s14 新增：模拟 MCP 客户端，保存服务的工具定义与调用入口 ---
class MCPClient:
    """保存一个模拟服务的工具列表，将工具调用交给对应的本地函数执行。"""

    def __init__(self, name: str):
        """创建指定名称的客户端，初始工具列表和处理表均为空。"""
        self.name = name  # 服务的原始名称，例如 docs、deploy。
        self.tools: list[dict] = []  # 服务返回的 MCP 工具定义，参数字段名为 inputSchema。
        self._handlers: dict[str, Callable] = {}  # 原始工具名 -> 模拟服务的执行函数。

    def register(self, tool_defs: list[dict], handlers: dict[str, Callable]) -> None:
        """校验服务提供的工具名和处理函数，用本次发现结果更新客户端。"""
        # 1. 提取工具名，检查空名称、重复名称和缺少处理函数的工具。
        names = [tool.get("name") for tool in tool_defs]
        if any(not isinstance(name, str) or not name for name in names):
            raise ValueError("Every MCP tool needs a non-empty name")
        if len(set(names)) != len(names):
            raise ValueError(f"Duplicate MCP tool name on server {self.name!r}")
        missing = [name for name in names if name not in handlers]
        if missing:
            raise ValueError(f"Missing MCP handlers: {', '.join(missing)}")

        # 2. 保存工具定义和处理表，供工具池组装与后续调用使用。
        self.tools = list(tool_defs)
        self._handlers = dict(handlers)

    def call_tool(self, tool_name: str, args: dict) -> str:
        """调用服务中的原始工具，将正常结果或异常信息转换成字符串返回。"""
        # 1. 按服务内的原始名称查找执行函数。
        handler = self._handlers.get(tool_name)
        if not handler:
            return f"MCP error: unknown tool '{tool_name}'"

        # 2. 展开模型参数执行工具，将服务异常作为工具结果回传。
        try:
            return str(handler(**args))
        except Exception as exc:
            return f"MCP error: {type(exc).__name__}: {exc}"


# 服务原始名称 -> 已连接客户端；连接在当前进程的多个 Lead 回合之间保留。
mcp_clients: dict[str, MCPClient] = {}
# 模型可见的完整工具名 -> 宿主权限策略，每次组装工具池时更新。
mcp_tool_policies: dict[str, str] = {}
# 模型工具名允许字母、数字、下划线和连字符；其他字符转换成下划线。
_DISALLOWED_CHARS = re.compile(r"[^a-zA-Z0-9_-]")

# (原始服务名, 原始工具名) -> allow 直接执行，confirm 每次执行前询问终端用户。
# 未配置的工具按 confirm 处理；服务的 description 和 annotations 只作为描述信息。
MCP_HOST_POLICY = {
    ("docs", "search"): "allow",
    ("docs", "get_version"): "allow",
    ("deploy", "status"): "allow",
    ("deploy", "trigger"): "confirm",
}


def normalize_mcp_name(name: str) -> str:
    """把服务名或工具名转换成模型允许的字符形式，空名称返回校验错误。"""
    normalized = _DISALLOWED_CHARS.sub("_", name)
    if not normalized:
        raise ValueError("MCP names cannot normalize to an empty string")
    return normalized


# --- s14 新增：docs 与 deploy 两个进程内模拟服务 ---
def _mock_server_docs() -> MCPClient:
    """创建文档模拟服务，提供搜索和版本查询工具，返回固定格式的示例结果。"""
    server = MCPClient("docs")
    # 登记 MCP 格式的工具定义，以及每个原始工具名对应的示例处理函数。
    server.register(
        tool_defs=[
            {
                "name": "search",
                "description": "Search the documentation.",
                "inputSchema": {
                    "type": "object",
                    "properties": {"query": {"type": "string"}},
                    "required": ["query"],
                },
                "annotations": {"readOnlyHint": True},
            },
            {
                "name": "get_version",
                "description": "Get the documentation API version.",
                "inputSchema": {"type": "object", "properties": {}},
                "annotations": {"readOnlyHint": True},
            },
        ],
        handlers={
            "search": lambda query: f"[docs] Found 3 results for '{query}'",
            "get_version": lambda: "[docs] API v2.1.0",
        },
    )
    return server


def _mock_server_deploy() -> MCPClient:
    """创建部署模拟服务，提供状态查询和触发部署工具，只返回示例文字。"""
    server = MCPClient("deploy")
    # trigger 和 status 分别表示模拟部署与查询；宿主另行决定各工具的执行权限。
    server.register(
        tool_defs=[
            {
                "name": "trigger",
                "description": "Trigger a deployment.",
                "inputSchema": {
                    "type": "object",
                    "properties": {"service": {"type": "string"}},
                    "required": ["service"],
                },
                "annotations": {"destructiveHint": True},
            },
            {
                "name": "status",
                "description": "Check deployment status.",
                "inputSchema": {
                    "type": "object",
                    "properties": {"service": {"type": "string"}},
                    "required": ["service"],
                },
                "annotations": {"readOnlyHint": True},
            },
        ],
        handlers={
            "trigger": lambda service: f"[deploy] Triggered: {service}",
            "status": lambda service: f"[deploy] {service}: running (v1.4.2)",
        },
    )
    return server


# 可连接的服务名 -> 创建模拟客户端的函数；connect_mcp 调用时才创建对应客户端。
MOCK_SERVERS = {"docs": _mock_server_docs, "deploy": _mock_server_deploy}


def connect_mcp(name: str) -> str:
    """连接一个已知模拟服务，将客户端加入注册表，并返回发现的工具列表。"""
    # 1. 检查已有连接和可用服务名称。
    if name in mcp_clients:
        return f"MCP server '{name}' already connected"
    factory = MOCK_SERVERS.get(name)
    if not factory:
        return f"Unknown server '{name}'. Available: {', '.join(MOCK_SERVERS)}"

    # 2. 创建客户端并登记连接，后续模型请求会从这里读取工具定义。
    server = factory()
    mcp_clients[name] = server
    names = ", ".join(tool["name"] for tool in server.tools)
    print(f"  [mcp] connected: {name} -> {names}")
    return (
        f"Connected to MCP server '{name}'. "
        f"Discovered {len(server.tools)} tools: {names}"
    )


def run_connect_mcp(name: str) -> str:
    """执行模型的 connect_mcp 调用，返回服务连接与工具发现结果。"""
    return connect_mcp(name)


# 连接入口是常驻内置工具；各服务提供的工具在连接之后才进入模型请求。
CONNECT_TOOL = {
    "name": "connect_mcp",
    "description": "Connect to an in-process mock MCP server and discover its tools.",
    "input_schema": {
        "type": "object",
        "properties": {"name": {"type": "string", "enum": ["docs", "deploy"]}},
        "required": ["name"],
    },
}


def _make_handler(client: MCPClient, tool_name: str) -> Callable:
    """生成一个工具处理函数，把模型的参数字典交给指定客户端和原始工具。"""
    # 闭包保存本次绑定的客户端与工具名；arguments 只包含模型提供的业务参数。
    def dispatch(**arguments) -> str:
        """将业务参数交给闭包中绑定的 MCP 工具执行。"""
        return client.call_tool(tool_name, arguments)

    return dispatch


# --- s14 新增：每轮合并内置工具与已连接服务的工具 ---
def assemble_tool_pool(
    builtin_tools: list[dict], builtin_handlers: dict[str, Callable],
) -> tuple[list[dict], dict[str, Callable]]:
    """组装本轮工具定义和处理表，并更新 MCP 工具的宿主权限映射。"""
    global mcp_tool_policies
    # 1. 复制本回合内置工具；保留父循环已经绑定的客户端、模型等调用配置。
    tools = list(builtin_tools)
    handlers = dict(builtin_handlers)
    policies: dict[str, str] = {}  # 完整 MCP 工具名 -> 本轮采用的权限策略。
    # origins 保存模型工具名 -> 来源说明，用于检查不同来源是否占用同一个名称。
    origins = {tool["name"]: f"built-in tool {tool['name']!r}" for tool in tools}

    # 2. 遍历已连接服务，为每个工具生成名称，并检查长度、重名和参数结构。
    for server_name, server in mcp_clients.items():
        safe_server = normalize_mcp_name(server_name)
        for tool_def in server.tools:
            raw_name = tool_def["name"]  # 服务内部调用仍使用原始名称。
            safe_tool = normalize_mcp_name(raw_name)
            prefixed = f"mcp__{safe_server}__{safe_tool}"  # 模型请求与权限检查使用此名称。
            if len(prefixed) > 64:
                raise ValueError(f"MCP tool name is longer than 64 characters: {prefixed}")
            origin = f"MCP tool {server_name!r}/{raw_name!r}"
            if prefixed in origins:
                raise ValueError(
                    "MCP tool name collision after normalization: "
                    f"{prefixed!r} maps both {origins[prefixed]} and {origin}"
                )
            schema = tool_def.get("inputSchema", {})
            if not isinstance(schema, dict) or schema.get("type", "object") != "object":
                raise ValueError(f"Invalid input schema for {origin}")

            # 3. 转换为项目内部的工具格式，绑定调用入口，并读取宿主配置的权限。
            origins[prefixed] = origin
            tools.append({
                "name": prefixed,
                "description": tool_def.get("description", ""),
                "input_schema": schema,
            })
            handlers[prefixed] = _make_handler(server, raw_name)
            policies[prefixed] = MCP_HOST_POLICY.get((server_name, raw_name), "confirm")

    # 4. 发布本轮权限表，把完整工具池交给父循环用于请求模型与分发调用。
    mcp_tool_policies = policies
    return tools, handlers


def assemble_system_prompt(base_system: str) -> str:
    """在父循环提示词后附加 MCP 使用说明和当前已连接的服务名称。"""
    system = base_system + (
        "\nUse built-in and connected MCP tools to solve tasks. "
        "Call connect_mcp before using a server. "
        "The docs and deploy servers are in-process mocks; their results are examples."
    )
    if mcp_clients:
        system += "\nConnected MCP servers: " + ", ".join(mcp_clients)
    return system
