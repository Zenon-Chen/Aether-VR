"""从 MIXLINE Stream 虚拟麦录音，检测 440Hz 测试音是否到达"""
import json
import numpy as np
import sounddevice as sd

SR = 48000
SECONDS = 2.0

target = None
for i, d in enumerate(sd.query_devices()):
    if d["max_input_channels"] > 0 and "MIXLINE Stream" in d["name"] and d["hostapi"] == 2:
        target = (i, d["name"])
        break

rec = sd.rec(int(SR * SECONDS), samplerate=SR, channels=1, device=target[0], blocking=True)
signal = rec[SR // 4:, 0]
rms = float(np.sqrt(np.mean(signal ** 2)))
spectrum = np.abs(np.fft.rfft(signal))
peak = float(np.fft.rfftfreq(len(signal), 1 / SR)[int(np.argmax(spectrum))])
result = {
    "input": f"#{target[0]} {target[1]}",
    "rms": round(rms, 4),
    "peak_hz": round(peak, 1),
    "pass": bool(rms > 0.005 and abs(peak - 440.0) < 5),
}
with open("logs/mixline_verify.json", "w", encoding="utf-8") as f:
    json.dump(result, f, ensure_ascii=False, indent=1)
print(json.dumps(result, ensure_ascii=False))
