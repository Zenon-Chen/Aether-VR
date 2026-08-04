"""列出音频设备，定位 MIXLINE 虚拟声卡"""
import json
import sounddevice as sd

out = {"outputs": [], "inputs": [], "hostapis": [], "default": list(sd.default.device)}
for i, h in enumerate(sd.query_hostapis()):
    out["hostapis"].append({"index": i, "name": h["name"]})
for i, d in enumerate(sd.query_devices()):
    entry = {"index": i, "hostapi": d["hostapi"], "name": d["name"],
             "sr": d["default_samplerate"]}
    if d["max_output_channels"] > 0:
        out["outputs"].append({**entry, "ch": d["max_output_channels"]})
    if d["max_input_channels"] > 0:
        out["inputs"].append({**entry, "ch": d["max_input_channels"]})

with open("logs/audio_devices.json", "w", encoding="utf-8") as f:
    json.dump(out, f, ensure_ascii=False, indent=1)
print("written", len(out["outputs"]), "outputs,", len(out["inputs"]), "inputs")
