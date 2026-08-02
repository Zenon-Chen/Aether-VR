"""共享常量 — 工具契约的单一事实来源

execute_tts / execute_motion 的枚举与约束统一定义在此，
由 tool_dispatcher.py（本地校验）与 er2/prompts.py（LLM 工具声明）共同引用，
避免两处维护导致漂移。
"""

# ── execute_tts 参数枚举 ──────────────────────────
EMOTIONS: tuple[str, ...] = (
    "neutral", "happy", "sad", "angry", "amused",
    "curious", "worried", "enthusiastic", "sarcastic",
    "thinking", "apologetic", "surprised",
)

SPEECH_RATES: tuple[str, ...] = ("slow", "normal", "fast")

# ── execute_tts 参数约束 ──────────────────────────
TEXT_MAX_LENGTH: int = 200

# ── execute_motion 参数约束 ───────────────────────
MOTION_DURATION_MIN: float = 0.5
MOTION_DURATION_MAX: float = 10.0
