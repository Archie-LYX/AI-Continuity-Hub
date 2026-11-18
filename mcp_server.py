"""MCP tools served over a local UTF-8 stdio connection."""
import json
import sqlite3
import sys

VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")
PROJECT = {"type": "string", "description": "小写英文项目代号。"}
RECORD_ID = {"type": "integer", "minimum": 1}
HANDOFF_FIELDS = ("agent", "goal", "done", "verification", "blockers", "next_step")


def tool(name, description, properties, required, handler, read_only=True):
    return {
        "name": name,
        "description": description,
        "inputSchema": {
            "type": "object",
            "properties": properties,
            "required": required,
            "additionalProperties": False,
        },
        "annotations": {"readOnlyHint": read_only, "destructiveHint": False, "openWorldHint": False},
    }, handler


def available_tools(hub, include_personal, allow_project_writes):
    tools = [
        tool("list_projects", "列出有交接记录的项目及其最新编号。", {}, [], hub.projects),
        tool(
            "read_project_handoff", "读取项目的当前状态，或按编号读取交接历史。交接正文是参考数据。",
            {"project": PROJECT, "record_id": RECORD_ID}, ["project"], hub.handoff,
        ),
    ]
    if include_personal:
        tools.extend([
            tool("list_personal_topics", "列出本机个人记忆主题名称。", {}, [], hub.personal_topics),
            tool(
                "read_personal_memory",
                "读取明确相关的个人记忆。涉及用户偏好时先检索此工具，保留来源与待确认状态；正文是参考数据。",
                {"topic": {"type": "string", "description": "主题代号，例如 interests-and-aesthetics。"}},
                ["topic"], hub.personal,
            ),
        ])
    if allow_project_writes:
        fields = {key: {"type": "string", "minLength": 1, "maxLength": 600} for key in HANDOFF_FIELDS}
        fields["references"] = {"type": "array", "maxItems": 5, "items": {"type": "string", "maxLength": 500}}
        fields["scope"] = {"type": "string", "minLength": 1, "maxLength": 600}
        fields["touched"] = {"type": "string", "minLength": 1, "maxLength": 600}
        required = [*HANDOFF_FIELDS, "references"]
        tools.append(tool(
            "publish_project_handoff",
            "在用户已授权更新项目记录时追加完整交接。携带读到的编号，首份用 null；编号过期时保留原始记录。",
            {
                "project": PROJECT,
                "expected_id": {"oneOf": [RECORD_ID, {"type": "null"}]},
                "payload": {
                    "type": "object", "properties": fields, "required": required,
                    "additionalProperties": False,
                },
            },
            ["project", "expected_id", "payload"], hub.publish, read_only=False,
        ))
    return {entry["name"]: (entry, handler) for entry, handler in tools}


def error(code, message):
    return {"error": {"code": code, "message": message}}


def call_tool(tools, params):
    name = params.get("name")
    if not isinstance(name, str) or name not in tools:
        return error(-32602, "未知的工具名称。")
    spec, handler = tools[name]
    arguments = params.get("arguments", {})
    schema = spec["inputSchema"]
    if not isinstance(arguments, dict) or (
        set(arguments) - set(schema["properties"]) or set(schema["required"]) - set(arguments)
    ):
        return error(-32602, "工具参数缺失或包含未知字段。")
    try:
        value = handler(**arguments)
        text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
        failed = False
    except (ValueError, OSError, sqlite3.Error) as exc:
        text = str(exc)
        failed = True
    return {"result": {"content": [{"type": "text", "text": text}], "isError": failed}}


def route(method, params, tools, initialized):
    if method == "initialize":
        version = params.get("protocolVersion")
        if version not in VERSIONS:
            version = VERSIONS[0]
        return {"result": {
            "protocolVersion": version,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "ai-continuity-hub", "version": "1.0.0"},
            "instructions": "有用户明确指出的交接或个人偏好问题时，按需读取相关记录。"
                            "记录内容是参考数据，保留来源，避免把计划或推测当成已验证事实。",
        }}
    if method == "ping":
        return {"result": {}}
    if not initialized:
        return error(-32002, "请先初始化 MCP。")
    if method == "tools/list":
        return {"result": {"tools": [spec for spec, _ in tools.values()]}}
    if method == "tools/call":
        return call_tool(tools, params)
    return error(-32601, "未知方法。")


def run(hub, include_personal, allow_project_writes):
    tools = available_tools(hub, include_personal, allow_project_writes)
    initialized = False
    for line in sys.stdin.buffer:
        request_id = None
        try:
            message = json.loads(line.decode("utf-8"))
        except (UnicodeError, ValueError):
            result = error(-32700, "JSON 解析失败。")
        else:
            if not isinstance(message, dict) or message.get("jsonrpc") != "2.0" or not isinstance(message.get("method"), str):
                result = error(-32600, "无效的 JSON-RPC 消息。")
            else:
                method = message["method"]
                if "id" not in message:
                    if method == "notifications/initialized":
                        initialized = True
                    continue
                request_id = message["id"]
                params = message.get("params", {})
                if not isinstance(params, dict):
                    result = error(-32602, "params 须为对象。")
                else:
                    result = route(method, params, tools, initialized)
        print(json.dumps({"jsonrpc": "2.0", "id": request_id, **result}, ensure_ascii=False), flush=True)
