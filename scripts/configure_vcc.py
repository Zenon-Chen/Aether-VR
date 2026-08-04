"""把 Unity 编辑器路径写入 VCC settings.json"""
import json
import os
from pathlib import Path

SETTINGS = Path(os.environ["LOCALAPPDATA"]) / "VRChatCreatorCompanion" / "settings.json"
EDITOR = r"C:\Program Files\Unity\Hub\Editor\2022.3.22f1\Editor\Unity.exe"
HUB = r"C:\Program Files\Unity Hub\Unity Hub.exe"

data = json.loads(SETTINGS.read_text(encoding="utf-8"))
data["pathToUnityExe"] = EDITOR
data["pathToUnityHub"] = HUB
editors = set(data.get("unityEditors") or [])
editors.add(EDITOR)
data["unityEditors"] = sorted(editors)
data["skipUnityAutoFind"] = False

SETTINGS.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
print("pathToUnityExe:", data["pathToUnityExe"])
print("unityEditors:", data["unityEditors"])
