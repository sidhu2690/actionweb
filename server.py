from flask import Flask
import time
from datetime import datetime, timezone

app = Flask(__name__)
BOOT = time.time()
MAX_UP = 21300          # 5 h 55 m
hits = 0

@app.route("/")
def index():
    global hits
    hits += 1

    up = int(time.time() - BOOT)
    h, m, s = up // 3600, up % 3600 // 60, up % 60

    left = max(0, MAX_UP - up)
    lh, lm = left // 3600, left % 3600 // 60

    now = datetime.now(timezone.utc).strftime("%H:%M:%S UTC")

    return f"""<!DOCTYPE html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="refresh" content="30">
<title>live server</title>
<style>
  *{{margin:0;padding:0;box-sizing:border-box}}
  body{{
    font-family:'SF Mono','Fira Code',Consolas,monospace;
    background:#0a0a0a;color:#aaa;
    display:grid;place-items:center;min-height:100vh;
  }}
  main{{
    background:#111;border:1px solid #222;border-radius:12px;
    padding:2rem 2.5rem;min-width:360px;
  }}
  .st{{display:flex;align-items:center;gap:.6rem;margin-bottom:1.5rem}}
  .dot{{
    width:9px;height:9px;background:#0f0;border-radius:50%;
    animation:p 1.5s ease-in-out infinite;
  }}
  @keyframes p{{50%{{opacity:.2}}}}
  .st span{{color:#0f0;font-weight:bold;font-size:1.1rem}}
  .r{{
    display:flex;justify-content:space-between;
    padding:.55rem 0;border-bottom:1px solid #1a1a1a;font-size:.9rem;
  }}
  .r:last-of-type{{border:none}}
  .l{{color:#555}}.v{{color:#0f0}}
  footer{{text-align:center;margin-top:1.3rem;color:#2a2a2a;font-size:.7rem}}
</style></head><body>
<main>
  <div class="st"><div class="dot"></div><span>LIVE</span></div>
  <div class="r"><span class="l">uptime</span><span class="v">{h}h {m}m {s}s</span></div>
  <div class="r"><span class="l">clock</span><span class="v">{now}</span></div>
  <div class="r"><span class="l">visitors</span><span class="v">{hits}</span></div>
  <div class="r"><span class="l">host</span><span class="v">github actions</span></div>
  <div class="r"><span class="l">next cycle</span><span class="v">~{lh}h {lm}m</span></div>
  <footer>auto-refresh 30s · github actions runner</footer>
</main></body></html>"""

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)
