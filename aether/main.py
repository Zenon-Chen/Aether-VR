"""Aether VR Agent 主程序

启动流程：
1. 加载配置
2. 初始化日志
3. 检查 DART 服务可用性
4. 初始化所有模块
5. 连接 ER2 Live API
6. 启动并行协程（音频输入、ER2 接收、心跳、上下文管理）
7. CLI 命令交互
"""

import asyncio
import logging
import sys

from .config import load_config, AetherConfig
from .logging_config import setup_logging
from .er2 import ER2Client
from .tool_dispatcher import ToolDispatcher
from .motion import DARTClient, OSCSender
from .motion.use_case import MotionUseCase
from .io import AudioPipeline
from .io.audio_stream import AudioStream
from .speech import TTSEngine
from .context import (
    ContextBlock,
    ContextBlockStore,
    ImportanceScorer,
    ContextEvictor,
    RAGRetriever,
    HeartbeatGenerator,
    AgentState,
)
from .tool_dispatcher import MotionState

logger = logging.getLogger(__name__)


class AetherAgent:
    """VR Agent 主控制器

    负责初始化并串联所有子系统：
    ER2 ↔ ToolDispatcher ↔ (TTS / DART / RAG)
    AudioPipeline ↔ ER2（音频流）
    OSCSender ← DART（骨架数据）
    ContextBlockStore ← ER2（上下文块）→ Scorer / Evictor / RAG
    HeartbeatGenerator → ER2（定期状态注入）
    """

    def __init__(self, config: AetherConfig):
        self.config = config

        # ── 音频流：TTS 生产端 ↔ AudioPipeline 消费端 共享同一实例 ──
        self.audio_stream = AudioStream()

        # ── 核心模块 ──────────────────────────────
        self.er2 = ER2Client(config.er2)
        self.dispatcher = ToolDispatcher()
        self.dart = DARTClient(config.dart)
        self.osc = OSCSender(config.osc)
        self.audio = AudioPipeline(config.audio, self.audio_stream)
        self.tts = TTSEngine(config.tts, self.audio_stream)

        # ── 应用用例 ──────────────────────────────
        self.motion_use_case = MotionUseCase(self.dart, self.osc, config.dart)

        # ── 上下文管理 ────────────────────────────
        self.block_store = ContextBlockStore(config.context.embedding_dim)
        self.scorer = ImportanceScorer(config.context, self.block_store, config.er2.api_key)
        self.evictor = ContextEvictor(config.context, self.block_store, config.er2.api_key)
        self.rag = RAGRetriever(config.context, self.block_store, config.er2.api_key)
        self.heartbeat = HeartbeatGenerator(config.context.heartbeat_interval)

        self._running = False
        self._dart_available = False

    # ══════════════════════════════════════════════
    # 启动
    # ══════════════════════════════════════════════

    async def start(self):
        """启动 Agent — 按顺序初始化所有子系统"""
        logger.info("=== Aether VR Agent Starting ===")

        # 1. 连接 DART 服务（可选，不可用时跳过动作生成）
        await self.dart.connect()
        self._dart_available = await self.dart.is_available()
        if self._dart_available:
            health = await self.dart.health_check()
            logger.info("DART service ready: %s", health)
        else:
            logger.warning("DART service not available, motion generation disabled")

        # 2. 连接 OSC（发送端，UDP 无握手）
        self.osc.connect()
        logger.info("OSC connected to %s:%d", self.config.osc.host, self.config.osc.port)

        # 3. 注入处理器到 ToolDispatcher
        self._wire_handlers()

        # 4. 注入处理器到 ER2
        self.er2.set_tool_call_handler(self.dispatcher.handle_tool_call)
        self.er2.set_tool_call_cancelled_handler(self.dispatcher.cancel_pending)
        self.er2.set_block_interceptor(self._block_interceptor)

        # 5. 注入结果回调：后台任务完成后异步回传 FunctionResponse
        self.dispatcher.set_response_callback(self.er2.send_tool_response)

        # 6. 设置音频回调
        #    注意：on_speech_start / on_speech_end 是同步回调
        #         （AudioPipeline._process_vad_result 中直接调用，不 await）
        #    on_audio_chunk 是异步回调（input_loop 中 await）
        self.audio.set_callbacks(
            on_speech_start=self._on_speech_start,
            on_speech_end=self._on_speech_end,
            on_audio_chunk=self._on_audio_chunk,
        )

        # 7. 连接 ER2 Live API
        await self.er2.connect()
        logger.info("ER2 Live API connected")

        # 8. 启动音频管道（内部会创建 input_loop + output_loop 任务）
        await self.audio.start()

        # 9. 加载垫音文件
        self.audio.load_filler_sounds()

        self._running = True
        logger.info("=== Aether VR Agent Ready ===")

    # ── 处理器注入 ─────────────────────────────────

    def _wire_handlers(self):
        """连接各模块的处理器到 ToolDispatcher"""

        # TTS handler: dispatcher → tts_engine（音频入 tts.audio_stream）
        async def tts_handler(text: str, emotion: str = "neutral",
                              speech_rate: str = "normal"):
            result = await self.tts.synthesize(text, emotion, speech_rate)
            return result

        # Motion handler: dispatcher → motion_use_case（DART 生成 → OSC 播放）
        async def motion_handler(action: str, duration: float = 2.0):
            return await self.motion_use_case.execute(action, duration)

        # Memory handler: dispatcher → rag_retriever
        async def memory_handler(query: str, max_tokens: int = 2000):
            return await self.rag.retrieve(query, max_tokens)

        # Status handler: dispatcher → 日志（状态已由 dispatcher 统一维护）
        async def status_handler(status_type: str, detail: str):
            logger.info("Status: %s - %s", status_type, detail)

        self.dispatcher.set_tts_handler(tts_handler)
        self.dispatcher.set_motion_handler(motion_handler)
        self.dispatcher.set_memory_handler(memory_handler)
        self.dispatcher.set_status_handler(status_handler)

    # ══════════════════════════════════════════════
    # 拦截器与回调
    # ══════════════════════════════════════════════

    async def _block_interceptor(self, chunk):
        """上下文块拦截器 — 由 ER2 receive_loop 调用

        解析 chunk.server_content 中的文本和工具调用，
        创建 ContextBlock 加入 block_store 和 scorer 队列。
        """
        sc = chunk.server_content
        if sc is None:
            return

        # 提取文本内容（ER2 chunk.text 已在外层日志，这里记录到上下文存储）
        text = chunk.text if hasattr(chunk, "text") and chunk.text else ""
        if text:
            block = ContextBlock(
                block_type="text",
                content=text,
                token_estimate=self.block_store.estimate_token_count(text),
            )
            self.block_store.add_block(block)
            self.scorer.queue_for_scoring(block)

        # 提取工具调用（tool_call 在 LiveServerMessage 顶层，不在 server_content 里）
        if hasattr(chunk, "tool_call") and chunk.tool_call:
            for fc in chunk.tool_call.function_calls:
                fc_text = f"{fc.name}({fc.args})"
                block = ContextBlock(
                    block_type="tool_call",
                    content=fc_text,
                    token_estimate=self.block_store.estimate_token_count(fc_text),
                    metadata={"function": fc.name, "args": dict(fc.args) if fc.args else {}},
                )
                self.block_store.add_block(block)
                self.scorer.queue_for_scoring(block)

    def _on_speech_start(self):
        """用户开始说话 — 打断处理（同步回调）

        AudioPipeline._process_vad_result 直接调用（不 await），
        因此必须是同步方法。
        """
        logger.info("[Agent] User started speaking (interrupt)")
        self.audio.stop_playback()
        self.tts.clear_queue()
        # 停止动作播放并回到空闲姿态
        self.osc.stop()
        try:
            self.osc.send_idle_pose()
        except Exception:
            pass
        # play_filler_sound 是 async，需要在事件循环中调度
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(self.audio.play_filler_sound())
        except RuntimeError:
            pass  # 无运行中的事件循环，跳过垫音

    def _on_speech_end(self):
        """用户停止说话（同步回调）"""
        logger.debug("[Agent] User stopped speaking")

    async def _on_audio_chunk(self, pcm_data: bytes):
        """音频块 → 发送到 ER2（异步回调）"""
        await self.er2.send_audio(pcm_data)

    # ══════════════════════════════════════════════
    # 运行循环
    # ══════════════════════════════════════════════

    async def run(self):
        """主运行循环 — 启动后台任务 + CLI 交互"""
        await self.start()

        # 后台任务列表
        # 注：AudioPipeline.start() 已创建 input_loop / output_loop 任务，
        #     TTS→播放 由共享 AudioStream 直连，无需桥接任务
        tasks = [
            asyncio.create_task(self.er2.receive_loop(), name="er2_receive"),
            asyncio.create_task(self._heartbeat_loop(), name="heartbeat"),
            asyncio.create_task(self._context_management_loop(), name="context_mgmt"),
        ]

        try:
            await self._cli_loop()
        except KeyboardInterrupt:
            logger.info("Shutting down...")
        finally:
            self._running = False
            for task in tasks:
                task.cancel()
            # 等待任务取消完成
            await asyncio.gather(*tasks, return_exceptions=True)
            await self.stop()

    async def _heartbeat_loop(self):
        """心跳循环 — 定期向 ER2 注入世界状态

        AgentState 由 dispatcher.state 投影而来（单一状态源），
        HeartbeatGenerator 只做纯函数投影。
        """
        while self._running:
            await asyncio.sleep(1)
            if self.heartbeat.should_send():
                # 从 dispatcher 状态投影 AgentState（单一事实来源）
                recent = self.dispatcher.state.recent_actions[-3:]
                recent_actions = tuple(
                    a.get("action", "unknown") if isinstance(a, dict) else str(a)
                    for a in recent
                )
                state = AgentState(
                    current_task=(
                        f"executing: {recent_actions[-1]}"
                        if self.dispatcher.state.motion_state
                        in (MotionState.GENERATING, MotionState.PLAYING)
                        and recent_actions
                        else "idle"
                    ),
                    recent_actions=recent_actions,
                    emotion_state=self.dispatcher.state.emotion_state,
                )

                msg = self.heartbeat.generate(state)
                if msg:
                    await self.er2.send_heartbeat(msg)
                    logger.debug("[Heartbeat] %s...", msg[:80])

    async def _context_management_loop(self):
        """上下文管理循环 — 评分 + 驱逐"""
        while self._running:
            await asyncio.sleep(5)

            # 重要性评分
            if self.scorer.should_score():
                await self.scorer.score_pending()

            # 驱逐检查
            if self.evictor.should_evict():
                await self.evictor.evict()

    # ── CLI 交互 ──────────────────────────────────

    async def _cli_loop(self):
        """CLI 命令交互循环

        支持的命令：
          /status           - 系统状态
          /tokens           - Token 用量
          /memory           - 上下文存储摘要
          /idle             - 发送空闲姿态
          /motion <text>    - 测试动作生成
          /tts <text>       - 测试 TTS 合成
          /quit             - 退出
        """
        print("\n=== Aether VR Agent ===")
        print("Commands:")
        print("  /status              - Show system status")
        print("  /tokens              - Show token usage")
        print("  /memory              - Show context memory stats")
        print("  /idle                - Send idle pose")
        print("  /motion <text>       - Generate motion (e.g., /motion walk forward*5)")
        print("  /tts <text>          - Test TTS (e.g., /tts hello)")
        print("  /quit                - Exit")
        print("========================\n")

        loop = asyncio.get_running_loop()

        while self._running:
            try:
                # input() 是阻塞的，用 executor 包装
                line = await loop.run_in_executor(
                    None, lambda: input("> ").strip()
                )

                if not line:
                    continue

                if line == "/quit":
                    break
                elif line == "/status":
                    self._print_status()
                elif line == "/tokens":
                    print(f"Active tokens: {self.block_store.total_active_tokens}")
                    print(f"Total blocks: {len(self.block_store.blocks)}")
                elif line == "/memory":
                    summary = self.block_store.get_summary()
                    for key, val in summary.items():
                        print(f"  {key}: {val}")
                elif line == "/idle":
                    self.osc.send_idle_pose()
                    print("Idle pose sent")
                elif line.startswith("/motion "):
                    text = line[8:]
                    print(f"Generating motion: {text}")
                    try:
                        result = await self.dart.generate(text)
                        if result and "poses" in result:
                            await self.osc.play_motion(result)
                            print(f"Motion playing: {result.get('num_frames', 0)} frames")
                        else:
                            print("Motion generation returned no data")
                    except Exception as exc:
                        print(f"Motion failed: {exc}")
                elif line.startswith("/tts "):
                    text = line[5:]
                    print(f"Synthesizing: {text}")
                    try:
                        await self.tts.synthesize(text, "neutral", "normal")
                        print("TTS queued")
                    except Exception as exc:
                        print(f"TTS failed: {exc}")
                else:
                    # 作为文本发送给 ER2（自由对话）
                    await self.er2.send_text(line)

            except EOFError:
                break

    def _print_status(self):
        """打印系统状态"""
        print(f"\n--- System Status ---")
        print(f"ER2:     {'Connected' if self.er2.session else 'Disconnected'}")
        print(f"DART:    {'Available' if self._dart_available else 'Not connected'}")
        print(f"OSC:     {self.config.osc.host}:{self.config.osc.port}")
        print(f"Motion:  {self.dispatcher.state.motion_state.value}")
        print(f"Emotion: {self.dispatcher.state.emotion_state}")
        print(f"Audio:   {'no-audio' if self.audio.no_audio else 'active'}")
        print(f"Context: {len(self.block_store.blocks)} blocks, "
              f"{self.block_store.total_active_tokens} tokens")
        print(f"Dispatcher: {self.dispatcher.get_state_summary()}")
        print(f"--------------------\n")

    # ══════════════════════════════════════════════
    # 停止
    # ══════════════════════════════════════════════

    async def stop(self):
        """停止所有模块"""
        logger.info("Stopping Aether Agent...")
        await self.audio.stop()
        self.osc.stop()
        try:
            self.osc.send_idle_pose()
        except Exception:
            pass
        await self.er2.close()
        await self.dart.close()
        logger.info("Aether Agent stopped")


# ══════════════════════════════════════════════════
# 入口函数
# ══════════════════════════════════════════════════

async def main():
    """入口函数"""
    config = load_config()
    setup_logging(config)

    agent = AetherAgent(config)
    await agent.run()


if __name__ == "__main__":
    asyncio.run(main())
