"""scribe-service — 书记官：长期记忆与上下文压缩

方案（用户提出 + 评审补充的两条规则）：
    大脑持续运行，产出推理块 → 积攒 N 块批量标记重要性
    → 三档处理：重要=原样保留 / 一般=压成一行摘要 / 无价值=整块删除
    → 超过 context_limit → 清除全部「不重要」
    → 仍超限 → 驱逐智能体在「重要」块中二次裁决，**且必须落盘**

两条补充规则：
    1. 删减必须留摘要指针，不能真删（否则引用断裂造成人设事故）
    2. 驱逐 = 从上下文移出 + 写入长期记忆库
    另：驱逐有滞回，一次清到 evict_target 再停，避免频繁触发。

阶段 2 接入；当前为可用的本地骨架（SQLite 落盘 + 三档标记），
批量重要性标记的模型调用留待接入大脑时补齐。
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Optional

from ...infra.config import ScribeConfig

logger = logging.getLogger(__name__)


class Importance(str, Enum):
    IMPORTANT = "important"    # 原样保留
    NORMAL = "normal"          # 压成一行摘要
    WORTHLESS = "worthless"    # 整块删除


@dataclass
class Block:
    """一个推理块（think / text / tool）"""
    kind: str
    content: str
    created_at: float = field(default_factory=time.time)
    importance: Optional[Importance] = None
    summary: str = ""
    references: int = 0

    @property
    def active_text(self) -> str:
        return self.summary if self.importance is Importance.NORMAL and self.summary else self.content

    @property
    def tokens(self) -> int:
        """活动上下文的粗略 token 数；normal 只计算摘要。"""
        text = self.active_text
        chinese = sum(1 for c in text if "\u4e00" <= c <= "\u9fff")
        return max(1, chinese + (len(text) - chinese) // 4)


class ScribeService:
    """上下文压缩与长期记忆"""

    name = "scribe"

    def __init__(self, config: ScribeConfig) -> None:
        self._config = config
        self._pending: list[Block] = []
        self._blocks: list[Block] = []
        self._db: Optional[sqlite3.Connection] = None
        self._lock = asyncio.Lock()
        self._persisted_count = 0
        self._evaluator: Optional[
            Callable[[list[dict[str, str]]], Awaitable[list[dict[str, str]]]]
        ] = None

    def set_evaluator(
        self,
        evaluator: Callable[
            [list[dict[str, str]]], Awaitable[list[dict[str, str]]]
        ],
    ) -> None:
        self._evaluator = evaluator

    # ── 生命周期 ───────────────────────────────────────────

    async def start(self) -> None:
        if not self._config.enabled:
            logger.info("[Scribe] 未启用（阶段 2 接入）")
            return
        path = Path(self._config.db_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(path), check_same_thread=False)
        self._db.execute("""
            CREATE TABLE IF NOT EXISTS memory (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at REAL NOT NULL,
                kind TEXT NOT NULL,
                content TEXT NOT NULL,
                summary TEXT DEFAULT ''
            )
        """)
        self._db.commit()
        logger.info("[Scribe] 记忆库就绪：%s", path)

    async def stop(self) -> None:
        await self.flush()
        if self._db is not None:
            self._db.close()
            self._db = None

    async def health(self) -> str:
        if not self._config.enabled:
            return "disabled"
        return "ok" if self._db is not None else "degraded"

    # ── 记录 ───────────────────────────────────────────────

    async def note(self, kind: str, content: str) -> None:
        """登记一个推理块；积攒到 batch_size 后批量标记"""
        if not self._config.enabled or not content.strip():
            return
        async with self._lock:
            self._pending.append(Block(kind=kind, content=content))
            if len(self._pending) >= self._config.batch_size:
                await self._mark_batch()

    async def _mark_batch(self) -> None:
        """批量标记重要性，模型不可用时回退本地启发式。"""
        batch, self._pending = self._pending, []
        decisions: list[dict[str, str]] = []
        if self._evaluator is not None:
            decisions = await self._evaluator([
                {"kind": block.kind, "content": block.content}
                for block in batch
            ])

        for index, block in enumerate(batch):
            decision = decisions[index] if index < len(decisions) else {}
            try:
                block.importance = Importance(decision.get("importance", ""))
            except ValueError:
                block.importance = _heuristic_importance(block)
            if block.importance is Importance.NORMAL:
                block.summary = (
                    str(decision.get("summary", "")).strip()
                    or _one_line(block.content)
                )
        self._blocks.extend(b for b in batch if b.importance is not Importance.WORTHLESS)
        await self._enforce_limit()
        logger.debug("[Scribe] 批量标记 %d 块，保留 %d", len(batch), len(self._blocks))

    async def _enforce_limit(self) -> None:
        """超过上限时先驱逐 normal，再按确定性价值分驱逐 important。"""
        if self.context_tokens <= self._config.context_limit_tokens:
            return
        normals = [b for b in self._blocks if b.importance is Importance.NORMAL]
        importants = sorted(
            (b for b in self._blocks if b.importance is Importance.IMPORTANT),
            key=self._retention_score,
        )
        for block in normals + importants:
            if self.context_tokens <= self._config.evict_target_tokens:
                return
            pointer = block.summary or _one_line(block.content)
            await self.remember(block.content, f"evicted_{block.kind}", summary=pointer)
            self._blocks.remove(block)

    def _retention_score(self, block: Block) -> float:
        """高价值关键字、引用次数和近期性共同决定保留价值。"""
        durable = sum(
            1 for key in ("记住", "答应", "承诺", "名字", "喜欢", "讨厌")
            if key in block.content
        )
        age_hours = max(0.0, time.time() - block.created_at) / 3600
        recency = 1 / (1 + age_hours)
        return durable * 10 + block.references * 3 + recency

    # ── 长期记忆 ───────────────────────────────────────────

    async def remember(
        self,
        content: str,
        kind: str = "fact",
        summary: str = "",
    ) -> None:
        """写入长期记忆库 —— 驱逐时必须调用，保证信息不消失"""
        if self._db is None:
            return
        now = time.time()
        duplicate = self._db.execute(
            "SELECT 1 FROM memory "
            "WHERE kind = ? AND content = ? AND created_at >= ? LIMIT 1",
            (kind, content, now - self._config.dedupe_window_s),
        ).fetchone()
        if duplicate is not None:
            return
        self._db.execute(
            "INSERT INTO memory (created_at, kind, content, summary) VALUES (?, ?, ?, ?)",
            (now, kind, content, summary),
        )
        self._db.commit()
        self._persisted_count += 1

    async def recall(self, query: str, max_tokens: int = 2000) -> str:
        """检索长期记忆（当前为关键词匹配，接入嵌入后替换）"""
        if self._db is None:
            return ""
        cursor = self._db.execute(
            "SELECT content FROM memory WHERE content LIKE ? "
            "ORDER BY created_at DESC LIMIT 20",
            (f"%{query}%",),
        )
        return _fit_recent(cursor.fetchall(), max_tokens)

    async def recall_recent(self, max_tokens: int = 2000) -> str:
        """读取 SQLite 中最近的长期记忆，供大脑观察循环回流。"""
        if self._db is None:
            return ""
        cursor = self._db.execute(
            "SELECT content FROM memory ORDER BY created_at DESC LIMIT 20"
        )
        return _fit_recent(cursor.fetchall(), max_tokens)

    async def flush(self) -> None:
        """会话结束时把待处理块落盘"""
        async with self._lock:
            if self._pending:
                await self._mark_batch()
            for block in self._blocks:
                if block.importance is Importance.IMPORTANT:
                    await self.remember(block.content, block.kind)

    def render_active_context(self, max_tokens: int = 4000) -> str:
        """渲染 pending 与已裁决活动块；normal 绝不暴露原文。"""
        lines: list[str] = []
        used = 0
        for block in [*self._blocks, *self._pending]:
            text = block.active_text
            cost = block.tokens
            if used + cost > max_tokens:
                break
            lines.append(f"[{block.kind}] {text}")
            used += cost
        return "\n".join(lines)

    @property
    def context_tokens(self) -> int:
        return sum(b.tokens for b in [*self._blocks, *self._pending])

    @property
    def metrics(self) -> dict[str, int]:
        return {
            "pending_blocks": len(self._pending),
            "active_blocks": len(self._blocks),
            "active_tokens": self.context_tokens,
            "persisted_blocks": self._persisted_count,
        }


def _fit_recent(rows: list[tuple[str]], max_tokens: int) -> str:
    out: list[str] = []
    used = 0
    for (content,) in rows:
        cost = max(1, len(content) // 2)
        if used + cost > max_tokens:
            break
        out.append(content)
        used += cost
    return "\n".join(out)


def _heuristic_importance(block: Block) -> Importance:
    """占位启发式：无事件心跳确认为零价值，含事实陈述为重要"""
    text = block.content.strip()
    if not text or text.startswith("[HEARTBEAT") and "ack" in text.lower():
        return Importance.WORTHLESS
    if any(k in text for k in ("我叫", "我的名字", "记住", "喜欢", "讨厌", "明天", "答应")):
        return Importance.IMPORTANT
    return Importance.NORMAL


def _one_line(text: str, limit: int = 40) -> str:
    flat = " ".join(text.split())
    return flat if len(flat) <= limit else flat[:limit] + "…"
