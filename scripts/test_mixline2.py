"""MIXLINE 回环第二轮：独立播放流 + 独立录音流，遍历 hostapi 组合"""
import json
import threading
import numpy as np
import sounddevice as sd

SR = 48000
DURATION = 1.5
FREQ = 440.0

def devices(name_part, want_output):
    out = []
    for i, d in enumerate(sd.query_devices()):
        ch = d["max_output_channels"] if want_output else d["max_input_channels"]
        if ch > 0 and name_part.lower() in d["name"].lower():
            out.append((i, d["name"], d["hostapi"]))
    return out

def probe(out_dev, in_dev):
    t = np.linspace(0, DURATION, int(SR * DURATION), endpoint=False)
    tone = (0.6 * np.sin(2 * np.pi * FREQ * t)).astype(np.float32)
    recorded = {}

    def record():
        try:
            recorded["data"] = sd.rec(int(SR * DURATION), samplerate=SR,
                                      channels=1, device=in_dev, blocking=True)
        except Exception as exc:
            recorded["error"] = str(exc)

    thread = threading.Thread(target=record)
    thread.start()
    try:
        sd.play(tone, samplerate=SR, device=out_dev, blocking=True)
    except Exception as exc:
        thread.join()
        return {"error": f"play: {exc}"}
    thread.join()

    if "error" in recorded:
        return {"error": f"rec: {recorded['error']}"}
    signal = recorded["data"][SR // 4:, 0]
    rms = float(np.sqrt(np.mean(signal ** 2)))
    spectrum = np.abs(np.fft.rfft(signal))
    peak = float(np.fft.rfftfreq(len(signal), 1 / SR)[int(np.argmax(spectrum))])
    return {"rms": round(rms, 4), "peak_hz": round(peak, 1),
            "pass": bool(rms > 0.01 and abs(peak - FREQ) < 5)}

def main():
    hostapi_names = {i: h["name"] for i, h in enumerate(sd.query_hostapis())}
    outs = devices("MIXLINE", True)
    ins = devices("MIXLINE", False)
    results = []
    for oi, oname, oha in outs:
        if oha == 3:      # WDM-KS 独占，跳过
            continue
        for ii, iname, iha in ins:
            if iha != oha:  # 同 hostapi 配对，减少矩阵
                continue
            r = probe(oi, ii)
            results.append({
                "hostapi": hostapi_names[oha],
                "out": f"#{oi} {oname}", "in": f"#{ii} {iname}", **r,
            })
            print(json.dumps(results[-1], ensure_ascii=False))
    with open("logs/mixline_loopback2.json", "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=1)

if __name__ == "__main__":
    main()
