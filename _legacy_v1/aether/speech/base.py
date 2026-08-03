"""TTS 引擎基类

定义语音合成引擎的统一接口。
"""

import asyncio
from abc import ABC, abstractmethod
from typing import Optional


class BaseTTSEngine(ABC):
    """TTS 引擎抽象基类"""

    @abstractmethod
    async def synthesize(self, text: str, emotion: str = "neutral",
                         speech_rate: str = "normal") -> str:
        """
        合成语音并加入播放队列。

        Args:
            text: 要说的文本
            emotion: 情绪状态
            speech_rate: 语速

        Returns:
            状态字符串
        """
        ...

    @abstractmethod
    def clear_queue(self):
        """清空播放队列（用于打断场景）"""
        ...

    @abstractmethod
    async def get_next_audio(self) -> Optional[bytes]:
        """从播放队列获取下一段音频"""
        ...
