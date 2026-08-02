"""DART 动作生成 HTTP 客户端

通过 HTTP 调用 WSL2 中的 DART FastAPI 服务，
获取文本描述的 SMPL-X 人体骨架数据。

DART 服务返回格式：
    poses:   (T, 165) — 前 66 维有效（22 关节 × 3 轴角），后 99 维全零
    trans:   (T, 3)   — 平移
    betas:   (10,)    — 体型参数
    joints:  (T, 22, 3) — 22 个关节的 3D 位置
    framerate: 30
    num_frames: T
"""

import asyncio
import logging
from typing import Optional

try:
    import httpx
except ImportError:
    httpx = None  # type: ignore[assignment]

from ..config import DARTConfig
from ..protocols import MotionPort

logger = logging.getLogger(__name__)


class DARTClient(MotionPort):
    """DART 动作生成客户端

    使用 httpx.AsyncClient 连接池复用 TCP 连接，
    支持超时、重试和结构化错误处理。
    """

    def __init__(self, config: DARTConfig):
        if httpx is None:
            raise ImportError(
                "httpx 未安装，请运行: pip install httpx"
            )
        self.config = config
        self.base_url = f"http://{config.host}:{config.port}"
        self._client: Optional[httpx.AsyncClient] = None

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    async def connect(self) -> None:
        """创建 HTTP 客户端（连接池复用）"""
        if self._client is not None:
            return
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            timeout=httpx.Timeout(self.config.timeout, connect=5.0),
        )
        logger.info("[DART] HTTP 客户端已创建 → %s", self.base_url)

    async def close(self) -> None:
        """关闭客户端、释放连接池"""
        if self._client is not None:
            await self._client.aclose()
            self._client = None
            logger.info("[DART] HTTP 客户端已关闭")

    def _ensure_client(self) -> "httpx.AsyncClient":
        """确保客户端已初始化，否则自动创建"""
        if self._client is None:
            raise RuntimeError(
                "DARTClient 尚未连接，请先调用 await connect()"
            )
        return self._client

    # ------------------------------------------------------------------
    # 健康检查
    # ------------------------------------------------------------------

    async def health_check(self) -> dict:
        """检查 DART 服务健康状态

        Returns:
            dict: 包含 GPU 状态和模型加载状态，例如
                  {"status": "ok", "gpu_available": true, "model_loaded": true}
        """
        client = self._ensure_client()
        try:
            resp = await client.get("/health")
            resp.raise_for_status()
            data = resp.json()
            logger.debug("[DART] health_check → %s", data)
            return data
        except httpx.ConnectError as exc:
            logger.error("[DART] 连接失败 (%s): %s", self.base_url, exc)
            return {"status": "error", "detail": f"连接失败: {exc}"}
        except httpx.TimeoutException as exc:
            logger.error("[DART] health_check 超时: %s", exc)
            return {"status": "error", "detail": f"超时: {exc}"}
        except Exception as exc:
            logger.error("[DART] health_check 异常: %s", exc)
            return {"status": "error", "detail": str(exc)}

    async def is_available(self) -> bool:
        """检查服务是否可用（GPU 就绪且模型已加载）"""
        result = await self.health_check()
        return result.get("status") == "ok"

    # ------------------------------------------------------------------
    # 动作生成
    # ------------------------------------------------------------------

    async def generate(
        self,
        text: str,
        guidance_param: float = 5.0,
        *,
        max_retries: int = 2,
    ) -> dict:
        """生成动作骨架数据

        Args:
            text: DART 格式文本，如 ``"walk forward*5"`` 或 ``"wave hand*3, turn left*2"``
            guidance_param: 引导强度，默认 5.0
            max_retries: 最大重试次数（仅针对网络/超时错误）

        Returns:
            dict::

                {
                    "poses":  list[list[float]],        # (T, 165)
                    "trans":  list[list[float]],        # (T, 3)
                    "betas":  list[float],              # (10,)
                    "joints": list[list[list[float]]],  # (T, 22, 3)
                    "framerate":  int,                  # 30
                    "num_frames": int,
                }

        Raises:
            httpx.HTTPStatusError: 服务端返回 4xx/5xx
            RuntimeError: 超过最大重试次数
        """
        client = self._ensure_client()
        payload = {"text": text, "guidance_param": guidance_param}

        last_exc: Optional[Exception] = None
        for attempt in range(1, max_retries + 1):
            try:
                logger.info(
                    "[DART] 请求生成 (attempt %d/%d): text=%r, guidance=%.1f",
                    attempt, max_retries, text, guidance_param,
                )
                resp = await client.post("/generate", json=payload)
                resp.raise_for_status()
                data: dict = resp.json()

                num_frames = data.get("num_frames", "?")
                logger.info(
                    "[DART] 生成完成: %s 帧, framerate=%s",
                    num_frames, data.get("framerate", "?"),
                )
                return data

            except httpx.TimeoutException as exc:
                last_exc = exc
                logger.warning(
                    "[DART] 生成超时 (attempt %d/%d): %s",
                    attempt, max_retries, exc,
                )
            except httpx.ConnectError as exc:
                last_exc = exc
                logger.warning(
                    "[DART] 连接失败 (attempt %d/%d): %s",
                    attempt, max_retries, exc,
                )
            except httpx.HTTPStatusError as exc:
                # 服务端错误不重试，直接抛出
                logger.error(
                    "[DART] 服务端错误 %d: %s", exc.response.status_code, exc
                )
                raise

            # 重试前短暂等待
            if attempt < max_retries:
                backoff = 1.0 * attempt
                logger.info("[DART] 等待 %.1fs 后重试…", backoff)
                await asyncio.sleep(backoff)

        raise RuntimeError(
            f"DART 生成失败，已重试 {max_retries} 次: {last_exc}"
        ) from last_exc

    # ------------------------------------------------------------------
    # 静态工具方法
    # ------------------------------------------------------------------

    @staticmethod
    def seconds_to_segments(
        seconds: float,
        framerate: int = 30,
        frames_per_segment: int = 8,
    ) -> int:
        """将持续秒数转换为 DART 的 segment 数

        Args:
            seconds: 动作持续时间（秒）
            framerate: 帧率，默认 30fps
            frames_per_segment: 每个 segment 包含的帧数

        Returns:
            segment 数量（至少为 1）

        Examples:
            >>> DARTClient.seconds_to_segments(2.0)
            7
            >>> DARTClient.seconds_to_segments(0.5)
            1
        """
        total_frames = int(seconds * framerate)
        return max(1, total_frames // frames_per_segment)

    @staticmethod
    def format_prompt(action: str, duration_seconds: float = 2.0) -> str:
        """将动作描述和持续时间格式化为 DART 输入

        Args:
            action: 英文动作描述，如 ``"walk forward"``
            duration_seconds: 持续时间（秒）

        Returns:
            DART 格式文本，如 ``"walk forward*7"``

        Examples:
            >>> DARTClient.format_prompt("walk forward", 2.0)
            'walk forward*7'
            >>> DARTClient.format_prompt("wave hand", 1.0)
            'wave hand*3'
        """
        segments = DARTClient.seconds_to_segments(duration_seconds)
        return f"{action}*{segments}"
