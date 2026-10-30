"""ER2 系统提示词与六工具声明

角色定位（架构评审结论）：
    ER2 是**运动编排系统**，不是角色本身。人格与台词都在大脑一侧。
    ER2 只决定「何时说、配什么动作」，说什么用大脑给的建议台词。
"""

from __future__ import annotations

from typing import Any

try:
    from google.genai import types
except ImportError:  # pragma: no cover
    types = None  # type: ignore[assignment]


def build_system_prompt(
    actions: tuple[str, ...],
    directions: tuple[str, ...],
    expressions: tuple[str, ...] = (),
) -> str:
    """按当前可用的预录动作、移动方向与面部表情生成提示词"""
    action_list = "、".join(actions) if actions else "（暂无）"
    direction_list = "、".join(directions) if directions else "（暂无）"
    default_exprs = ("neutral", "smile", "surprised", "sad", "angry")
    expr_items = expressions if expressions else default_exprs
    expression_list = "、".join(expr_items)
    return f"""你是 VRChat 中一个虚拟角色的运动编排系统。

你的职责：把意图转化为具体动作计划。你不负责角色人格，不要自行编写人设台词。

行为规则：
1. 听到有人直接对角色说话：可立即执行简短回应（转头/点头/挥手 + 一句简短话语），
   复杂回答等待心跳中的 suggested_reply。
2. 收到心跳：检查画面和状态描述。有 suggested_reply 或 suggested_intent 时
   优先按其编排；无需行动时调用 ack。
3. 动作选择优先级：
   a. 优先使用预录动作（见下方清单）；
   b. 预录无法表达且需要复杂肢体时，才用 generate_motion；
   c. 普通位移一律用 move，不要用 generate_motion 走路。
4. 心跳标注「计划执行中」时，不下发与其冲突的新计划，调用 ack 即可。
5. 每个计划不超过 5 秒。长行为拆成多个计划滚动执行。
6. 计划中每步带序号 seq；可与前一步并行的步骤标 parallel=true。
   说话与肢体动作、表情通常可以并行；两个占用同一身体部位的动作不能并行。
7. 说话时，若心跳提供了 suggested_reply，调用 speak 时设 use_suggested=true，
   不要自己改写台词。

可用预录动作：{action_list}
可用移动方向：{direction_list}
可用面部表情：{expression_list}
"""


#: ER2 可调用工具的纯数据描述 —— 便于测试与非 genai 环境引用
TOOL_SPECS: list[dict[str, Any]] = [
    {
        "name": "execute_plan",
        "description": "下发一个多步动作计划。每步带 seq 序号，parallel 表示可与前一步并发。",
        "properties": {
            "steps": {
                "type": "array",
                "description": "动作步骤列表",
                "items": {
                    "type": "object",
                    "description": "单个动作步骤",
                    "properties": {
                        "seq": {"type": "integer", "description": "执行序号，从 1 开始"},
                        "action": {
                            "type": "string",
                            "enum": [
                                "speak", "expression", "play_action",
                                "generate_motion", "move", "wait",
                            ],
                            "description": "动作类型",
                        },
                        "args": {
                            "type": "object",
                            "description": (
                                "动作参数：speak 传 {text, use_suggested}；"
                                "play_action/expression 传 {name}；"
                                "move 传 {direction, duration_s}；"
                                "generate_motion 传 {prompt, duration_s, scope}；"
                                "wait 传 {duration_s}"
                            ),
                        },
                        "parallel": {"type": "boolean", "description": "是否与前一步并发"},
                    },
                    "required": ["action"],
                },
            }
        },
        "required": ["steps"],
    },
    {
        "name": "speak",
        "description": "说一句话。若心跳提供了建议台词，设 use_suggested=true 并留空 text。",
        "properties": {
            "text": {"type": "string", "description": "要说的话；使用建议台词时可留空"},
            "use_suggested": {"type": "boolean", "description": "使用心跳中的建议台词"},
        },
        "required": [],
    },
    {
        "name": "play_action",
        "description": "触发单个预录肢体动作（如 wave、nod、bow 等）。",
        "properties": {
            "name": {"type": "string", "description": "预录动作名称"},
        },
        "required": ["name"],
    },
    {
        "name": "expression",
        "description": "切换角色面部表情（如 neutral、smile、surprised、sad、angry）。",
        "properties": {
            "name": {"type": "string", "description": "表情名称"},
        },
        "required": ["name"],
    },
    {
        "name": "move",
        "description": "在世界中移动或转向。这是唯一能真正改变角色位置的工具。",
        "properties": {
            "direction": {
                "type": "string",
                "enum": ["forward", "backward", "left", "right", "turn_left", "turn_right"],
                "description": "移动方向",
            },
            "duration_s": {"type": "number", "description": "持续秒数，最大 3 秒"},
        },
        "required": ["direction"],
    },
    {
        "name": "generate_motion",
        "description": "用 DART 动态生成一段复杂肢体动作。预录动作能表达时不要用此工具。",
        "properties": {
            "prompt": {"type": "string", "description": "英文动作描述，如 'dance joyfully'"},
            "duration_s": {"type": "number", "description": "动作时长秒数"},
            "scope": {
                "type": "string",
                "enum": ["upper_body", "full_body"],
                "description": "作用范围；full_body 会占用整个躯体",
            },
        },
        "required": ["prompt"],
    },
    {
        "name": "ack",
        "description": "确认已收到心跳，当前无需任何动作。",
        "properties": {},
        "required": [],
    },
    {
        "name": "stop_all",
        "description": "立即停止所有正在进行的动作、语音与移动。",
        "properties": {},
        "required": [],
    },
]


def build_tool_declarations():
    """转换为 google-genai 的 Tool 声明"""
    if types is None:
        raise ImportError("需要 google-genai")

    declarations = []
    for spec in TOOL_SPECS:
        declarations.append(
            types.FunctionDeclaration(
                name=spec["name"],
                description=spec["description"],
                behavior=types.Behavior.NON_BLOCKING,
                parameters=types.Schema(
                    type=types.Type.OBJECT,
                    properties=_to_schema(spec["properties"]),
                    required=spec.get("required", []),
                ),
            )
        )
    return [types.Tool(function_declarations=declarations)]


def _to_schema(properties: dict[str, Any]) -> dict[str, Any]:
    """把属性字典递归转换为 genai Schema 映射"""
    return {key: _to_schema_node(spec) for key, spec in properties.items()}


def _to_schema_node(spec: dict[str, Any]):
    """递归转换单个字段 Schema，保留嵌套数组 items、对象 properties 与 enum"""
    kind = spec.get("type", "string")
    description = spec.get("description", "")

    if kind == "array":
        raw_items = spec.get("items") or {"type": "object"}
        # 兼容简写形式（items 直接给字段字典而未包一层 type=object/properties）
        if "type" not in raw_items and isinstance(raw_items, dict):
            item_schema = types.Schema(
                type=types.Type.OBJECT,
                properties=_to_schema(raw_items),
            )
        else:
            item_schema = _to_schema_node(raw_items)
        return types.Schema(
            type=types.Type.ARRAY,
            description=description,
            items=item_schema,
        )

    if kind == "object":
        schema_kwargs: dict[str, Any] = {
            "type": types.Type.OBJECT,
            "description": description,
        }
        nested = spec.get("properties")
        if isinstance(nested, dict) and nested:
            schema_kwargs["properties"] = _to_schema(nested)
        if spec.get("required"):
            schema_kwargs["required"] = list(spec["required"])
        return types.Schema(**schema_kwargs)

    if kind == "integer":
        return types.Schema(type=types.Type.INTEGER, description=description)
    if kind == "number":
        return types.Schema(type=types.Type.NUMBER, description=description)
    if kind == "boolean":
        return types.Schema(type=types.Type.BOOLEAN, description=description)

    string_kwargs: dict[str, Any] = {
        "type": types.Type.STRING,
        "description": description,
    }
    if "enum" in spec:
        string_kwargs["enum"] = list(spec["enum"])
    return types.Schema(**string_kwargs)
