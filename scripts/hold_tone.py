"""持续播放低音量测试音，让 Python 出现在 MIXLINE 的应用列表中"""
import sys
import numpy as np
import sounddevice as sd

SR = 48000
minutes = float(sys.argv[1]) if len(sys.argv) > 1 else 5.0

# 播到 MIXLINE 扬声器端点：不吵人，且算“正在发声”
target = None
for i, d in enumerate(sd.query_devices()):
    if d["max_output_channels"] > 0 and "MIXLINE" in d["name"] and d["hostapi"] == 2:
        target = i
        break

t = np.linspace(0, 1.0, SR, endpoint=False)
tone = (0.3 * np.sin(2 * np.pi * 440 * t)).astype(np.float32)
loops = int(minutes * 60)
print(f"playing 440Hz to device #{target} for {minutes} min ...", flush=True)
for n in range(loops):
    sd.play(tone, samplerate=SR, device=target, blocking=True)
print("done")
