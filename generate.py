#!/usr/bin/env python3
"""Pick a topic, generate an AI debate, build the static site."""

import json, os, re, random, pathlib, datetime, sys, time

ROOT      = pathlib.Path(__file__).parent
DEBATES   = ROOT / "debates"
DOCS      = ROOT / "docs"
PAGES     = DOCS / "debates"
TOPICS    = ROOT / "topics.json"

# ── personas ──────────────────────────────────────────────
NOVA  = {
    "name": "Nova", "role": "The Optimist", "color": "#4a9eff",
    "desc": "empathetic, progressive, hopeful — argues with human impact and vision"
}
AXIOM = {
    "name": "Axiom", "role": "The Skeptic", "color": "#ff6b4a",
    "desc": "analytical, pragmatic, skeptical — argues with logic, data, and caution"
}

# ── helpers ───────────────────────────────────────────────
def slugify(t):
    return re.sub(r"[^a-z0-9]+", "-", t.lower()).strip("-")[:60]

def reading_time(messages):
    words = sum(len(m["message"].split()) for m in messages)
    mins  = max(1, round(words / 200))
    return f"{mins} min read"

def load_all():
    out = []
    for f in sorted(DEBATES.glob("*.json"), reverse=True):
        out.append(json.loads(f.read_text()))
    return out

def used_topics():
    return {d["topic"] for d in load_all()}

# ── topic picker ──────────────────────────────────────────
def pick():
    pool = json.loads(TOPICS.read_text())
    done = used_topics()
    avail = [t for t in pool if t not in done]
    if not avail:
        avail = pool                       # recycle
    return random.choice(avail)

# ── gemini call ───────────────────────────────────────────
def generate(topic):
    import google.generativeai as genai

    key = os.environ.get("GEMINI_API_KEY")
    if not key:
        print("✖  Set GEMINI_API_KEY secret in repo settings")
        sys.exit(1)

    genai.configure(api_key=key)
    model = genai.GenerativeModel("gemini-2.0-flash")

    prompt = f"""You are a debate script writer.

Topic: "{topic}"

Speakers
  Nova  (The Optimist) — empathetic, progressive, hopeful.
  Axiom (The Skeptic)  — analytical, pragmatic, skeptical.

Rules
  • Exactly 5 rounds. Nova speaks first each round. 10 messages total.
  • Each message 60-100 words. Sharp, direct, no fluff.
  • They must engage each other's points, not just monologue.
  • Round 1 → Opening positions
  • Round 2 → Direct challenge
  • Round 3 → Real-world evidence / examples
  • Round 4 → Expose weakness in opponent's logic
  • Round 5 → Closing (leave tension unresolved)

Output ONLY a raw JSON array — no markdown fences, no commentary:
[{{"speaker":"Nova","message":"..."}},{{"speaker":"Axiom","message":"..."}}, ...]"""

    for attempt in range(3):
        try:
            resp = model.generate_content(prompt)
            text = resp.text.strip()
            # strip markdown fences if present
            text = re.sub(r"^```\w*\n?", "", text)
            text = re.sub(r"\n?```$", "", text)
            msgs = json.loads(text)
            if isinstance(msgs, list) and len(msgs) >= 8:
                return msgs[:10]
        except Exception as e:
            print(f"  attempt {attempt+1} failed: {e}")
            time.sleep(3)

    print("✖  Could not generate debate after 3 attempts")
    sys.exit(1)

# ── save debate json ──────────────────────────────────────
def save(topic, messages):
    DEBATES.mkdir(exist_ok=True)
    now  = datetime.datetime.now(datetime.timezone.utc)
    slug = slugify(topic)
    data = {
        "topic":    topic,
        "date":     now.isoformat(),
        "display":  now.strftime("%B %d, %Y · %H:%M UTC"),
        "slug":     slug,
        "filename": f"{now.strftime('%Y-%m-%d')}-{slug}",
        "reading":  reading_time(messages),
        "messages": messages,
    }
    path = DEBATES / f"{data['filename']}.json"
    path.write_text(json.dumps(data, indent=2))
    print(f"  saved → {path.name}")
    return data

# ── html builder ──────────────────────────────────────────
CSS = """\
:root{--bg:#0a0a0a;--sf:#111;--bd:#1a1a1a;--tx:#b0b0b0;
--dm:#555;--nova:#4a9eff;--axiom:#ff6b4a;--gn:#0f0}
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:'SF Mono','Fira Code',Consolas,monospace;
background:var(--bg);color:var(--tx);min-height:100vh}
a{color:var(--nova);text-decoration:none}a:hover{text-decoration:underline}

header{text-align:center;padding:3rem 1rem 1rem}
header h1{font-size:1.6rem;color:#fff;letter-spacing:.04em}
header h1 span.n{color:var(--nova)}
header h1 span.a{color:var(--axiom)}
header p{color:var(--dm);font-size:.8rem;margin-top:.4rem}

.meta{text-align:center;padding:1rem;color:var(--dm);font-size:.75rem}
.meta span{margin:0 .6rem}

.topic{text-align:center;padding:1.5rem 1rem .5rem;
font-size:1.25rem;color:#fff;font-weight:bold}

.chat{max-width:700px;margin:1rem auto 2rem;padding:0 1rem}
.bubble{margin-bottom:1.2rem;display:flex;flex-direction:column}
.bubble.nova{align-items:flex-start}
.bubble.axiom{align-items:flex-end}
.name{font-size:.7rem;font-weight:bold;margin-bottom:.3rem;padding:0 .4rem}
.name.nova{color:var(--nova)}.name.axiom{color:var(--axiom)}
.msg{max-width:85%;padding:.9rem 1.1rem;border-radius:14px;
font-size:.88rem;line-height:1.55;background:var(--sf);border:1px solid var(--bd)}
.bubble.nova .msg{border-left:3px solid var(--nova);border-radius:4px 14px 14px 14px}
.bubble.axiom .msg{border-right:3px solid var(--axiom);border-radius:14px 4px 14px 14px}

.round-label{text-align:center;color:var(--dm);font-size:.65rem;
letter-spacing:.12em;text-transform:uppercase;margin:1.8rem 0 .8rem}

.archive{max-width:700px;margin:0 auto 3rem;padding:0 1rem}
.archive h2{color:#fff;font-size:1.1rem;margin-bottom:1rem;
border-bottom:1px solid var(--bd);padding-bottom:.5rem}
.card{display:block;background:var(--sf);border:1px solid var(--bd);
border-radius:10px;padding:1rem 1.2rem;margin-bottom:.7rem;transition:border .2s}
.card:hover{border-color:var(--nova);text-decoration:none}
.card .t{color:#ddd;font-size:.9rem;font-weight:bold}
.card .d{color:var(--dm);font-size:.72rem;margin-top:.3rem}

footer{text-align:center;padding:2rem;color:#1a1a1a;font-size:.65rem}

.badge{display:inline-block;background:#0f01;color:var(--gn);
border:1px solid #0f03;border-radius:20px;padding:.15rem .7rem;
font-size:.7rem;margin-top:.5rem}

.nav{text-align:center;padding:1rem;font-size:.8rem}

@media(max-width:500px){
  .msg{max-width:95%;font-size:.82rem}
  header h1{font-size:1.2rem}
  .topic{font-size:1.05rem}
}"""

ROUNDS = [
    "Round 1 · Opening Positions",
    "Round 2 · Direct Challenge",
    "Round 3 · Evidence & Examples",
    "Round 4 · Expose the Weakness",
    "Round 5 · Final Statements",
]

def bubble_html(messages):
    h = ""
    for i, m in enumerate(messages):
        who = m["speaker"].lower()
        if who not in ("nova", "axiom"):
            who = "nova" if i % 2 == 0 else "axiom"
        if i % 2 == 0:
            ri = i // 2
            label = ROUNDS[ri] if ri < len(ROUNDS) else f"Round {ri+1}"
            h += f'<div class="round-label">{label}</div>\n'
        h += f'''<div class="bubble {who}">
  <div class="name {who}">{m["speaker"]} · {NOVA["role"] if who=="nova" else AXIOM["role"]}</div>
  <div class="msg">{m["message"]}</div>
</div>\n'''
    return h

def page(title, body, nav_home=False):
    nav = '<div class="nav"><a href="../index.html">← back to latest</a></div>' if nav_home else ''
    return f"""<!DOCTYPE html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title}</title>
<style>{CSS}</style>
</head><body>
<header>
  <h1><span class="n">Nova</span> vs <span class="a">Axiom</span></h1>
  <p>two AIs debate — new topic every 6 hours</p>
</header>
{nav}
{body}
<footer>generated by ai · hosted on github pages · zero cost</footer>
</body></html>"""

def build_debate_page(d):
    body  = f'<div class="topic">"{d["topic"]}"</div>\n'
    body += f'<div class="meta"><span>{d["display"]}</span><span>{d["reading"]}</span></div>\n'
    body += f'<div class="chat">\n{bubble_html(d["messages"])}</div>\n'
    return page(f'{d["topic"]} — Nova vs Axiom', body, nav_home=True)

def build_index(debates):
    if not debates:
        return page("Nova vs Axiom", "<p style='text-align:center;padding:3rem'>No debates yet.</p>")

    latest = debates[0]
    body  = f'<div class="topic">"{latest["topic"]}"</div>\n'
    body += f'<div class="meta">'
    body += f'<span class="badge">● LATEST</span> '
    body += f'<span>{latest["display"]}</span>'
    body += f'<span>{latest["reading"]}</span></div>\n'
    body += f'<div class="chat">\n{bubble_html(latest["messages"])}</div>\n'

    if len(debates) > 1:
        body += '<div class="archive"><h2>Previous Debates</h2>\n'
        for d in debates[1:]:
            body += f'''<a class="card" href="debates/{d["filename"]}.html">
  <div class="t">{d["topic"]}</div>
  <div class="d">{d["display"]} · {d["reading"]}</div>
</a>\n'''
        body += '</div>\n'

    return page("Nova vs Axiom", body)

def build_archive(debates):
    body = '<div class="archive" style="margin-top:1rem"><h2>All Debates</h2>\n'
    for d in debates:
        body += f'''<a class="card" href="debates/{d["filename"]}.html">
  <div class="t">{d["topic"]}</div>
  <div class="d">{d["display"]} · {d["reading"]}</div>
</a>\n'''
    body += '</div>\n'
    return page("Archive — Nova vs Axiom", body)

def build_site():
    DOCS.mkdir(exist_ok=True)
    PAGES.mkdir(exist_ok=True)
    (DOCS / ".nojekyll").touch()

    debates = load_all()

    (DOCS / "index.html").write_text(build_index(debates))
    (DOCS / "archive.html").write_text(build_archive(debates))

    for d in debates:
        (PAGES / f'{d["filename"]}.html').write_text(build_debate_page(d))

    print(f"  built site → {len(debates)} debates")

# ── main ──────────────────────────────────────────────────
def main():
    print("🎯 picking topic...")
    topic = pick()
    print(f"  → {topic}")

    print("🤖 generating debate...")
    messages = generate(topic)
    print(f"  → {len(messages)} messages")

    print("💾 saving...")
    save(topic, messages)

    print("🔨 building site...")
    build_site()

    print("✅ done")

if __name__ == "__main__":
    main()
