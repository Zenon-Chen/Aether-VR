"""大脑输出契约 — 人格层的观察、台词与记忆意图

关键分工（架构评审结论）：
    台词只出自大脑；ER2 仅负责「何时说、配什么动作」。
    这样人格、记忆一致性与语言风格完整地留在大脑一侧。
"""

from __future__ import annotations

import itertools
import json
from dataclasses import dataclass, field
from typing import Any, Optional

_brain_output_counter = itertools.count(1)


@dataclass(frozen=True, slots=True)
class Observation:
    """大脑从画面/音频中识别出的一个事件"""
    event: str
    target: str = ""
    confidence: float = 0.0

    def render(self) -> str:
        parts = [self.event]
        if self.target:
            parts.append(f"→ {self.target}")
        if self.confidence:
            parts.append(f"({self.confidence:.0%})")
        return " ".join(parts)


@dataclass(frozen=True, slots=True)
class BrainOutput:
    """大脑每轮的结构化输出

    Lite 档模型偶尔会漂出格式，因此 parse() 永不抛出：解析失败时返回
    None，由调用方沿用上一轮建议或只发事实心跳。
    """
    observations: tuple[Observation, ...] = ()
    suggested_reply: str = ""
    suggested_intent: str = ""
    wake_er2: bool = False
    memory_write: tuple[str, ...] = ()
    raw: Optional[dict[str, Any]] = field(default=None, repr=False)
    output_id: int = field(
        default_factory=lambda: next(_brain_output_counter), compare=False
    )

    @classmethod
    def parse(cls, text: str) -> Optional["BrainOutput"]:
        """宽容解析模型输出；失败返回 None 而非抛出"""
        if not text:
            return None
        payload = _extract_json_object(text)
        if payload is None:
            return None
        try:
            observations = tuple(
                Observation(
                    event=str(item.get("event", "")).strip(),
                    target=str(item.get("target", "")).strip(),
                    confidence=_as_float(item.get("confidence")),
                )
                for item in payload.get("observations", [])
                if isinstance(item, dict) and item.get("event")
            )
            memory = tuple(
                str(m).strip()
                for m in payload.get("memory_write", [])
                if str(m).strip()
            )
            return cls(
                observations=observations,
                suggested_reply=str(payload.get("suggested_reply", "")).strip(),
                suggested_intent=str(payload.get("suggested_intent", "")).strip(),
                wake_er2=bool(payload.get("wake_er2", False)),
                memory_write=memory,
                raw=payload,
            )
        except Exception:
            return None

    def is_empty(self) -> bool:
        return not (self.observations or self.suggested_reply or self.suggested_intent)


def _as_float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _extract_json_object(text: str) -> Optional[dict[str, Any]]:
    """从可能包含 markdown 围栏或前后缀文字的输出中抠出 JSON 对象"""
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = stripped.split("```")[1] if "```" in stripped[3:] else stripped[3:]
        if stripped.lstrip().startswith("json"):
            stripped = stripped.lstrip()[4:]
    start = stripped.find("{")
    end = stripped.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        parsed = json.loads(stripped[start:end + 1])
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None
