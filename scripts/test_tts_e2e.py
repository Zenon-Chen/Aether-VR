"""TTS 端到端验证：SpeechService 合成 → MIXLINE → Stream 虚拟麦收到"""
import asyncio
import json
import sys
import threading

import numpy as np
import sounddevice as sd

sys.path.insert(0, ".")
from aether.infra.config import load_config
from aether.services.speech.service import SpeechService

SR = 48000
RECORD_SECONDS = 8.0

def find_stream_mic():
    # 优先 MME（索引稳定、兼容性最好），其次 WASAPI
    for api in (0, 2, 1):
        for i, d in enumerate(sd.query_devices()):
            if (d["max_input_channels"] > 0 and "MIXLINE Stream" in d["name"]
                    and d["hostapi"] == api):
                return i
    return None

async def main():
    config = load_config("config.yaml")
    if not config.speech.api_key:
        print(json.dumps({"pass": False, "error": "GEMINI_API_KEY 未设置"}))
        return

    mic = find_stream_mic()
    chunks: list[np.ndarray] = []

    def on_audio(indata, frames, t, status):
        chunks.append(indata.copy())

    # 独立 InputStream：避免与 SpeechService 内部 sd.play 的全局流冲突
    stream = sd.InputStream(samplerate=SR, channels=1, device=mic, callback=on_audio)
    stream.start()

    service = SpeechService(config.speech)
    await service.start()
    duration = await service.speak("你好呀，我是 Aether，很高兴见到你！")
    await service.stop()
    await asyncio.sleep(0.5)
    stream.stop()
    stream.close()

    if not chunks:
        print(json.dumps({"pass": False, "tts_duration_s": round(duration, 2),
                          "error": "未录到任何音频"}, ensure_ascii=False))
        return
    recorded = {"data": np.concatenate(chunks)}

    signal = recorded["data"][:, 0]
    rms = float(np.sqrt(np.mean(signal ** 2)))
    peak_amp = float(np.max(np.abs(signal)))
    # 语音不是单频，判据：能量显著高于底噪
    result = {
        "tts_duration_s": round(duration, 2),
        "mic_rms": round(rms, 4),
        "mic_peak": round(peak_amp, 4),
        "pass": bool(duration > 0.5 and rms > 0.003),
    }
    with open("logs/tts_e2e.json", "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=1)
    print(json.dumps(result, ensure_ascii=False))

if __name__ == "__main__":
    asyncio.run(main())
