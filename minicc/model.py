"""模型请求：发送系统提示词、对话历史和工具定义，取得模型的回复消息。"""

import httpx


def request_model(
    client: httpx.Client, model: str, system: str,
    messages: list[dict], tool_definitions: list[dict], thinking: str = "",
    *, max_tokens: int = 8000,
) -> dict:
    """发起一次 Chat Completions 请求，返回接口中的 assistant 消息字典。

    tool_definitions 为空时省略工具；max_tokens 设置回复长度上限。
    """
    # 将系统提示词放在对话历史之前，设置模型名称和回复长度。
    # payload 是发送给服务的请求体；messages 在 system 提示之后，stream=False 表示一次性接收回复。
    payload = {
        "model": model,
        "messages": [{"role": "system", "content": system}, *messages],
        "max_tokens": max_tokens, "stream": False,
    }
    if tool_definitions:
        # 将工具的名称、用途和参数要求加入请求。
        payload["tools"] = [{"type": "function", "function": {
            "name": tool["name"],
            "description": tool["description"],
            "parameters": tool["input_schema"],
        }} for tool in tool_definitions]
    if thinking:
        # 有思考模式配置时，将该配置一同发送给服务。
        payload["thinking"] = {"type": thinking}
    # 发送请求并检查 HTTP 状态；请求失败时向调用方抛出异常。
    response = client.post("chat/completions", json=payload)
    response.raise_for_status()
    # 返回第一个候选消息，其中包含普通回复、工具调用及可能的思考内容。
    return response.json()["choices"][0]["message"]
