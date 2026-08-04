"""MIXLINE 回环验证

向「扬声器 (MIXLINE)」播放 440Hz 正弦波，同时从
「麦克风 (MIXLINE Record)」与「麦克风 (MIXLINE Stream)」录音，
检测能量与主频，确认虚拟声卡回环真实工作。
"""
import json
import numpy as np
import sounddevice as sd

SR = 48000
DURATION = 1.5
FREQ = 440.0

def find(name_part: str, want_output: bool, prefer_hostapi=(2, 1, 0)):
    """按名称片段找设备，hostapi 优先 WASAPI > DirectSound > MME，排除 WDM-KS"""
    candidates = []
    for i, d in enumerate(sd.query_devices()):
        ch = d["max_output_channels"] if want_output else d["max_input_channels"]
        if ch > 0 and name_part.lower() in d["name"].lower() and d["hostapi"] != 3:
            candidates.append((prefer_hostapi.index(d["hostapi"])
                               if d["hostapi"] in prefer_hostapi else 99, i, d["name"]))
    candidates.sort()
    return (candidates[0][1], candidates[0][2]) if candidates else (None, None)

def main():
    out_idx, out_name = find("MIXLINE", want_output=True)
    results = {"output_device": f"#{out_idx} {out_name}", "loopback": {}}

    t = np.linspace(0, DURATION, int(SR * DURATION), endpoint=False)
    tone = (0.5 * np.sin(2 * np.pi * FREQ * t)).astype(np.float32)

    for tap in ("MIXLINE Record", "MIXLINE Stream"):
        in_idx, in_name = find(tap, want_output=False)
        if in_idx is None:
            results["loopback"][tap] = "device not found"
            continue
        try:
            rec = sd.playrec(tone, samplerate=SR, channels=1,
                             input_mapping=[1],
                             device=(in_idx, out_idx), blocking=True)
            sd.wait()
            signal = rec[SR // 4:, 0]           # 跳过起始瞬态
            rms = float(np.sqrt(np.mean(signal ** 2)))
            spectrum = np.abs(np.fft.rfft(signal))
            peak_freq = float(np.fft.rfftfreq(len(signal), 1 / SR)[int(np.argmax(spectrum))])
            results["loopback"][tap] = {
                "input_device": f"#{in_idx} {in_name}",
                "rms": round(rms, 4),
                "peak_hz": round(peak_freq, 1),
                "pass": bool(rms > 0.01 and abs(peak_freq - FREQ) < 5),
            }
        except Exception as exc:
            results["loopback"][tap] = f"error: {exc}"

    with open("logs/mixline_loopback.json", "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=1)
    print(json.dumps(results, ensure_ascii=False, indent=1))

if __name__ == "__main__":
    main()
