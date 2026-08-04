import json
import sqlite3

conn = sqlite3.connect(r"C:\Users\Zenon\AppData\Local\Logi\MIXLINE\MIXLINE\settings.db")
cur = conn.cursor()
tables = [r[0] for r in cur.execute("SELECT name FROM sqlite_master WHERE type='table'")]
out = {"tables": tables, "data": {}}
for t in tables:
    try:
        cols = [c[1] for c in cur.execute(f"PRAGMA table_info({t})")]
        rows = cur.execute(f"SELECT * FROM {t} LIMIT 50").fetchall()
        out["data"][t] = {"cols": cols, "rows": [[str(x)[:200] for x in r] for r in rows]}
    except Exception as e:
        out["data"][t] = {"error": str(e)}
with open("logs/mixline_settings.json", "w", encoding="utf-8") as f:
    json.dump(out, f, ensure_ascii=False, indent=1)
print("tables:", tables)
