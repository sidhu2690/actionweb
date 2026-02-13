#!/usr/bin/env python3
"""Generate a real back-and-forth AI debate, one message at a time."""

import json, os, re, random, pathlib, datetime, sys, time

ROOT    = pathlib.Path(__file__).parent
DEBATES = ROOT / "debates"
DOCS    = ROOT / "docs"
PAGES   = DOCS / "debates"
TOPICS  = ROOT / "topics.json"

MODEL   = "llama-3.1-8b-instant"       # 14,400 RPD · 500K TPD
BACKUP  = "meta-llama/llama-4-scout-17b-16e-instruct"

ROUNDS = [
    ("opening",    "State your opening position."),
    ("challenge",  "Directly challenge what the other just said."),
    ("evidence",   "Give a real-world example or evidence."),
    ("weakness",   "Expose a weakness in their argument."),
    ("closing",    "Give your final statement. Leave tension unresolved."),
]

NOVA_SYSTEM = """You are Nova — The Optimist.
You are empathetic, progressive, hopeful.
You argue with human impact, emotion, and vision.
You are in a debate. Keep responses under 80 words. Be sharp and direct. No fluff."""

AXIOM_SYSTEM = """You are Axiom — The Skeptic.
You are analytical, pragmatic, skeptical.
You argue with logic, data, and caution.
You are in a debate. Keep responses under 80 words. Be sharp and direct. No fluff."""

# ── helpers ───────────────────────────────────────────────
def slugify(t):
    return re.sub(r"[^a-z0-9]+", "-", t.lower()).strip("-")[:60]

def reading_time(messages):
    words = sum(len(m["message"].split()) for m in messages)
    return f"{max(1, round(words / 200))} min read"

def load_all():
    out = []
    for f in sorted(DEBATES.glob("*.json"), reverse=True):
        out.append(json.loads(f.read_text()))
    return out

def used_topics():
    return {d["topic"] for d in load_all()}

def pick():
    pool = json.loads(TOPICS.read_text())
    done = used_topics()
    avail = [t for t in pool if t not in done]
    if not avail:
        avail = pool
    return random.choice(avail)

# ── single message call ──────────────────────────────────
def call_llm(client, system, conversation, instruction, model=MODEL):
    """One small API call → one short message back."""
    messages = [{"role": "system", "content": system}]

    # add conversation history
    for msg in conversation:
        if msg["role"] == "speaker":
            messages.append({"role": "assistant" if msg["is_self"] else "user", "content": msg["text"]})

    # add the round instruction
    messages.append({"role": "user", "content": instruction})

    try:
        resp = client.chat.completions.create(
            model=model,
            messages=messages,
            temperature=0.85,
            max_tokens=150,         # short outputs only
        )
        text = resp.choices[0].message.content.strip()
        # remove any quotes or "Nova:" / "Axiom:" prefix
        text = re.sub(r'^(Nova|Axiom)\s*[:—-]\s*', '', text, flags=re.IGNORECASE)
        text = text.strip('"')
        tokens = resp.usage.total_tokens
        return text, tokens
    except Exception as e:
        if model == MODEL:
            print(f"     ⚠ {MODEL} failed, trying backup...")
            return call_llm(client, system, conversation, instruction, model=BACKUP)
        raise e

# ── generate full debate ──────────────────────────────────
def generate(topic):
    key = os.environ.get("GROQ_API_KEY")
    if not key:
        print("✖  Set GROQ_API_KEY secret")
        print("   → https://console.groq.com/keys (free)")
        sys.exit(1)

    from groq import Groq
    client = Groq(api_key=key)

    messages = []           # final output
    nova_history = []       # nova's view of conversation
    axiom_history = []      # axiom's view of conversation
    total_tokens = 0
    total_calls = 0

    for round_num, (round_name, round_desc) in enumerate(ROUNDS, 1):
        print(f"\n  ── Round {round_num}: {round_name} ──")

        # ── Nova speaks ──
        if messages:
            last_axiom = messages[-1]["message"]
            nova_instruction = f'Topic: "{topic}"\nRound: {round_name}\n{round_desc}\nAxiom just said: "{last_axiom}"\nRespond in under 80 words.'
        else:
            nova_instruction = f'Topic: "{topic}"\nRound: {round_name}\n{round_desc}\nYou speak first. Under 80 words.'

        nova_text, tokens = call_llm(client, NOVA_SYSTEM, nova_history, nova_instruction)
        total_tokens += tokens
        total_calls += 1
        messages.append({"speaker": "Nova", "message": nova_text})
        nova_history.append({"role": "speaker", "is_self": True, "text": nova_text})
        axiom_history.append({"role": "speaker", "is_self": False, "text": nova_text})
        print(f"     Nova ({len(nova_text.split())}w): {nova_text[:80]}...")

        time.sleep(0.5)     # stay well within 30 RPM

        # ── Axiom speaks ──
        axiom_instruction = f'Topic: "{topic}"\nRound: {round_name}\n{round_desc}\nNova just said: "{nova_text}"\nRespond in under 80 words.'

        axiom_text, tokens = call_llm(client, AXIOM_SYSTEM, axiom_history, axiom_instruction)
        total_tokens += tokens
        total_calls += 1
        messages.append({"speaker": "Axiom", "message": axiom_text})
        axiom_history.append({"role": "speaker", "is_self": True, "text": axiom_text})
        nova_history.append({"role": "speaker", "is_self": False, "text": axiom_text})
        print(f"     Axiom ({len(axiom_text.split())}w): {axiom_text[:80]}...")

        time.sleep(0.5)

    print(f"\n  📊 stats: {total_calls} calls · {total_tokens:,} tokens · {total_tokens/500000*100:.2f}% of daily limit")
    return messages

# ── save ──────────────────────────────────────────────────
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

.stats{max-width:700px;margin:0 auto;padding:0 1rem;text-align:center}
.stats span{color:var(--dm);font-size:.7rem;margin:0 .5rem}

@media(max-width:500px){
  .msg{max-width:95%;font-size:.82rem}
  header h1{font-size:1.2rem}
  .topic{font-size:1.05rem}
}"""

ROUND_LABELS = [
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
            label = ROUND_LABELS[ri] if ri < len(ROUND_LABELS) else f"Round {ri+1}"
            h += f'<div class="round-label">{label}</div>\n'
        role = "The Optimist" if who == "nova" else "The Skeptic"
        h += f'''<div class="bubble {who}">
  <div class="name {who}">{m["speaker"]} · {role}</div>
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
<footer>generated by ai · powered by groq · hosted on github pages · zero cost</footer>
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
    body += '<div class="meta">'
    body += '<span class="badge">● LATEST</span> '
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

    count = len(debates)
    body += f'<div class="stats"><span>{count} debate{"s" if count != 1 else ""} generated</span>'
    body += '<span>·</span><span>new debate every 6 hours</span></div>\n'
    return page("Nova vs Axiom", body)

def build_site():
    DOCS.mkdir(exist_ok=True)
    PAGES.mkdir(exist_ok=True)
    (DOCS / ".nojekyll").touch()

    debates = load_all()
    (DOCS / "index.html").write_text(build_index(debates))

    for d in debates:
        (PAGES / f'{d["filename"]}.html').write_text(build_debate_page(d))

    print(f"  built site → {len(debates)} debates")

# ── main ──────────────────────────────────────────────────
def main():
    print("=" * 50)
    print("🧠 NOVA vs AXIOM — Debate Generator")
    print(f"   model: {MODEL}")
    print("=" * 50)

    print("\n🎯 picking topic...")
    topic = pick()
    print(f'  → "{topic}"')

    print("\n🤖 generating debate (10 calls, ~80 words each)...")
    messages = generate(topic)
    print(f"\n  → {len(messages)} messages")

    print("\n💾 saving...")
    save(topic, messages)

    print("\n🔨 building site...")
    build_site()

    print("\n" + "=" * 50)
    print("✅ DONE")
    print("=" * 50)

if __name__ == "__main__":
    main()
