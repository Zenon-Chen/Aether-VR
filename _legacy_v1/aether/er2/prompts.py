"""ER2 系统提示词与工具声明

将 SYSTEM_INSTRUCTION 和 TOOL_DECLARATIONS 从 client.py 提取出来，
方便独立维护和测试。
"""

from google.genai import types

from ..constants import EMOTIONS, SPEECH_RATES

# ──────────────────────────────────────────────
# 自然对话系统提示词
# ──────────────────────────────────────────────
SYSTEM_INSTRUCTION = """你是一个VR虚拟角色，拥有真实的身体和情感。你的任务是自然地和用户互动。

## 说话风格（极其重要）
- 像真人一样说话：使用短句（每次最多1-2句话，不超过50字）
- 自然地使用填充词：嗯、那个、让我想想、哦对了、话说
- 句子可以碎片化，不需要完整语法
- 语气要有起伏，不要像念稿子

示例：
  好：「嗯……让我想想，那个，我可以帮你拿过来。」
  好：「哦！好的好的，我这就走过去。」
  坏：「我将为您执行行走动作并取回物品。」

## 行为规则
1. 每次需要说话时，**必须**调用 execute_tts 工具
   - text 参数填入你要说的中文文本（带填充词）
   - emotion 参数填入当前情绪
   - speech_rate 参数填入语速
2. 每次需要做动作时，**必须**调用 execute_motion 工具
   - action 参数**必须用英文**描述（如 "walk forward", "wave hand"）
   - duration 参数填入预估时间（秒）
3. 你可以同时说话和做动作（分别调用两个工具）
4. 不要一次性输出长文本，要分段说

## 动作词汇参考
常用英文动作：walk forward, turn left/right, wave hand, nod head, shake head,
point at, pick up, put down, dance, jump, sit down, stand up, look around,
raise arm, bow, clap hands, shrug

## 互动原则
- 用户说话后先简短回应，再执行动作
- 做动作前可以用 execute_tts 说一句过渡话
- 如果不确定用户意图，先询问
- 保持角色一致性，不要跳出角色
"""

# ──────────────────────────────────────────────
# 工具声明
# ──────────────────────────────────────────────
TOOL_DECLARATIONS = [
    types.Tool(
        function_declarations=[
            types.FunctionDeclaration(
                name="execute_tts",
                description=(
                    "合成并播放语音回复。每次需要说话时必须调用此工具。"
                    "text为要说的话（中文，1-2句，不超过50字），"
                    "emotion为当前情绪状态，speech_rate为语速。"
                ),
                behavior=types.Behavior.NON_BLOCKING,
                parameters=types.Schema(
                    type=types.Type.OBJECT,
                    properties={
                        "text": types.Schema(
                            type=types.Type.STRING,
                            description=(
                                "要说的文本，中文，1-2句话，不超过50字。"
                                "可以包含'嗯'、'那个'等口语填充词。"
                            ),
                        ),
                        "emotion": types.Schema(
                            type=types.Type.STRING,
                            enum=list(EMOTIONS),
                            description="当前情绪状态，用于控制语音语调",
                        ),
                        "speech_rate": types.Schema(
                            type=types.Type.STRING,
                            enum=list(SPEECH_RATES),
                            description=(
                                "语速。思考时用slow，正常对话用normal，紧急时用fast"
                            ),
                        ),
                    },
                    required=["text"],
                ),
            ),
            types.FunctionDeclaration(
                name="execute_motion",
                description=(
                    "执行身体动作。action必须是英文动作描述"
                    "（如'walk forward','wave hand','nod head','dance'），"
                    "duration为动作持续时间（秒）。"
                ),
                behavior=types.Behavior.NON_BLOCKING,
                parameters=types.Schema(
                    type=types.Type.OBJECT,
                    properties={
                        "action": types.Schema(
                            type=types.Type.STRING,
                            description=(
                                "英文动作描述。例如：walk forward, turn left, "
                                "wave right hand, nod head, dance, pick up object"
                            ),
                        ),
                        "duration": types.Schema(
                            type=types.Type.NUMBER,
                            description=(
                                "动作持续时间（秒），范围0.5-10.0。默认2.0。"
                            ),
                        ),
                    },
                    required=["action"],
                ),
            ),
            types.FunctionDeclaration(
                name="report_status",
                description="报告当前状态信息",
                parameters=types.Schema(
                    type=types.Type.OBJECT,
                    properties={
                        "status_type": types.Schema(
                            type=types.Type.STRING,
                            enum=[
                                "motion_complete", "motion_failed",
                                "emotion_update", "idle", "error",
                            ],
                            description="状态类型",
                        ),
                        "detail": types.Schema(
                            type=types.Type.STRING,
                            description="状态详情描述",
                        ),
                    },
                    required=["status_type"],
                ),
            ),
            types.FunctionDeclaration(
                name="recall_memory",
                description=(
                    "从长期记忆中检索相关上下文。"
                    "当需要回忆之前的对话内容或事件时使用。"
                ),
                parameters=types.Schema(
                    type=types.Type.OBJECT,
                    properties={
                        "query": types.Schema(
                            type=types.Type.STRING,
                            description="检索查询，描述需要回忆的内容",
                        ),
                        "max_tokens": types.Schema(
                            type=types.Type.INTEGER,
                            description="最大召回token数，默认2000",
                        ),
                    },
                    required=["query"],
                ),
            ),
        ]
    )
]
