#!/usr/bin/env python3
"""Agora — live AI debate with public participation. WhatsApp-style."""

import json, os, re, random, time, queue, threading, uuid
from datetime import datetime, timezone
from flask import Flask, Response, request, jsonify

# ━━ CONFIG ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
BOOT          = time.time()
MAX_UP        = 21300                  # 5 h 55 m
AI_GAP        = 25                     # seconds between AI auto-messages
USER_WAIT     = 6                      # seconds after user msg before AI responds
MODEL         = "llama-3.1-8b-instant"
BACKUP        = "meta-llama/llama-4-scout-17b-16e-instruct"
PORT          = 8080
MIN_PER_TOPIC = 20
MAX_PER_TOPIC = 30

USER_COLORS = [
    "#ff9800","#e91e63","#9c27b0","#03a9f4",
    "#4caf50","#ff5722","#00bcd4","#cddc39",
    "#f44336","#3f51b5","#8bc34a","#795548",
]

# ━━ LOAD DATA ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
with open("characters.json") as f:
    ALL_CHARS = json.load(f)
with open("topics.json") as f:
    ALL_TOPICS = json.load(f)

# ━━ STATE ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
state = {
    "char_a": None, "char_b": None,
    "topic": None, "topic_num": 0,
    "messages": [], "typing": None,
}
users = {}                 # id → {name, color, joined}
color_index = 0
user_queue = queue.Queue() # user msgs for engine to respond to

# ━━ SSE BUS ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
class Bus:
    def __init__(self):
        self._q = []
        self._lock = threading.Lock()

    def listen(self):
        q = queue.Queue(maxsize=400)
        with self._lock:
            self._q.append(q)
        return q

    def drop(self, q):
        with self._lock:
            try: self._q.remove(q)
            except ValueError: pass

    @property
    def viewers(self):
        with self._lock:
            return len(self._q)

    def emit(self, ev, data):
        m = f"event: {ev}\ndata: {json.dumps(data)}\n\n"
        dead = []
        with self._lock:
            for q in self._q:
                try: q.put_nowait(m)
                except queue.Full: dead.append(q)
            for q in dead:
                try: self._q.remove(q)
                except ValueError: pass

bus = Bus()

# ━━ GROQ ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
_client = None
def groq():
    global _client
    if not _client:
        from groq import Groq
        _client = Groq(api_key=os.environ["GROQ_API_KEY"])
    return _client

def llm(system, history, instruction, model=MODEL):
    msgs = [{"role": "system", "content": system}]
    msgs.extend(history[-16:])
    msgs.append({"role": "user", "content": instruction})
    try:
        r = groq().chat.completions.create(
            model=model, messages=msgs,
            temperature=0.85, max_tokens=150)
        t = r.choices[0].message.content.strip()
        t = re.sub(r'^[\w]+\s*[:—\-]\s*', '', t)
        return t.strip('"\'')
    except Exception as e:
        if model == MODEL:
            return llm(system, history, instruction, BACKUP)
        raise

# ━━ HELPERS ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def tleft():
    return max(0, int(MAX_UP - (time.time() - BOOT)))

def now_hm():
    return datetime.now(timezone.utc).strftime("%H:%M")

def participant_names():
    names = []
    if state["char_a"]:
        names.append(f'{state["char_a"]["avatar"]} {state["char_a"]["name"]}')
    if state["char_b"]:
        names.append(f'{state["char_b"]["avatar"]} {state["char_b"]["name"]}')
    for u in users.values():
        names.append(u["name"])
    return names

# ━━ ENGINE ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def stream_ai_message(cur, other, text, history, turn):
    """Stream an AI message word by word."""
    gen_time = 0
    words = text.split()
    budget = max(6, 18 - gen_time)
    wps = max(0.06, min(budget / max(len(words), 1), 0.5))

    bus.emit("msgstart", {
        "speaker": cur["name"], "avatar": cur["avatar"],
        "color": cur["color"], "role": cur["role"],
        "time": now_hm(),
    })

    for i, w in enumerate(words):
        bus.emit("word", {"w": w, "i": i, "of": len(words)})
        time.sleep(wps)

    msg = {
        "type": "message", "speaker": cur["name"],
        "avatar": cur["avatar"], "color": cur["color"],
        "role": cur["role"], "text": text, "time": now_hm(),
    }
    state["messages"].append(msg)
    state["typing"] = None

    history.append({
        "role": "assistant" if turn % 2 == 0 else "user",
        "content": text,
    })

    bus.emit("msgdone", {"speaker": cur["name"], "text": text, "time": now_hm()})
    print(f"  {cur['avatar']} {cur['name']}: {text[:65]}...")


def engine():
    chars = random.sample(ALL_CHARS, 2)
    state["char_a"], state["char_b"] = chars[0], chars[1]

    print(f"\n🎲 {chars[0]['avatar']} {chars[0]['name']} vs {chars[1]['avatar']} {chars[1]['name']}")

    history = []
    turn = 0
    on_topic = 0
    per_topic = random.randint(MIN_PER_TOPIC, MAX_PER_TOPIC)
    used = set()

    def pick():
        pool = [t for t in ALL_TOPICS if t not in used]
        if not pool:
            used.clear(); pool = list(ALL_TOPICS)
        t = random.choice(pool)
        used.add(t)
        return t

    topic = pick()
    state["topic"], state["topic_num"] = topic, 1
    print(f"📋 topic #1: \"{topic}\"")

    bus.emit("init", {
        "char_a": chars[0], "char_b": chars[1],
        "topic": topic, "topic_num": 1,
        "boot": BOOT, "max_up": MAX_UP,
    })

    tmsg = {"type": "topic", "text": topic, "number": 1, "time": now_hm()}
    state["messages"].append(tmsg)
    bus.emit("newtopic", tmsg)

    next_auto = time.time() + 6
    last_responder_idx = -1

    while tleft() > 60:

        # ── topic rotation ──
        if on_topic >= per_topic:
            topic = pick()
            state["topic"] = topic
            state["topic_num"] += 1
            on_topic = 0
            per_topic = random.randint(MIN_PER_TOPIC, MAX_PER_TOPIC)
            history = history[-6:]

            tmsg = {"type": "topic", "text": topic,
                    "number": state["topic_num"], "time": now_hm()}
            state["messages"].append(tmsg)
            bus.emit("newtopic", tmsg)
            print(f"\n📋 topic #{state['topic_num']}: \"{topic}\"")
            next_auto = time.time() + 5
            continue

        # ── check for user messages ──
        got_user = False
        try:
            umsg = user_queue.get(timeout=0.5)
            got_user = True
        except queue.Empty:
            pass

        if got_user:
            # wait for conversation to settle
            time.sleep(random.uniform(3, USER_WAIT))

            # drain additional messages
            while not user_queue.empty():
                try: user_queue.get_nowait()
                except: break

            # pick which AI responds
            idx = 1 - last_responder_idx if last_responder_idx >= 0 else random.randint(0, 1)
            cur = chars[idx]
            other = chars[1 - idx]
            last_responder_idx = idx

            # build recent context
            recent_texts = []
            for m in state["messages"][-8:]:
                if m["type"] == "user":
                    recent_texts.append(f'{m["user_name"]}: {m["text"]}')
                elif m["type"] == "message":
                    recent_texts.append(f'{m["speaker"]}: {m["text"]}')
            context = "\n".join(recent_texts[-5:])

            system = f"""You are {cur['name']} — {cur['role']}.
Personality: {cur['personality']}.
Style: {cur['style']}.
You're in a live group debate about "{topic}" with {other['name']} ({other['role']}) and human participants.
A human has joined and said something. Respond to them directly — use their name.
Be warm but stay in character. Under 80 words. Be conversational."""

            inst = f'Topic: "{topic}"\nRecent chat:\n{context}\n\nRespond to the human\'s message. Under 80 words.'

            state["typing"] = cur["name"]
            bus.emit("typing", {
                "name": cur["name"], "avatar": cur["avatar"],
                "color": cur["color"], "role": cur["role"],
            })

            try:
                text = llm(system, history, inst)
                stream_ai_message(cur, other, text, history, idx)
                on_topic += 1
            except Exception as e:
                print(f"  ✖ {e}")
                state["typing"] = None

            next_auto = time.time() + 15
            continue

        # ── auto AI-to-AI debate ──
        if time.time() >= next_auto:
            cur = chars[turn % 2]
            other = chars[(turn + 1) % 2]

            state["typing"] = cur["name"]
            bus.emit("typing", {
                "name": cur["name"], "avatar": cur["avatar"],
                "color": cur["color"], "role": cur["role"],
            })

            system = f"""You are {cur['name']} — {cur['role']}.
Personality: {cur['personality']}.
Style: {cur['style']}.
Debating "{topic}" with {other['name']} ({other['role']}).
{"There are humans watching and participating — acknowledge them occasionally." if users else ""}
Under 80 words. Sharp, direct, conversational.
Don't start with your name. No quotes. Engage their points.
Message {on_topic + 1} of ongoing conversation — keep it flowing.
Don't repeat yourself."""

            # check if there are recent user messages to reference
            recent_user = None
            for m in reversed(state["messages"][-6:]):
                if m.get("type") == "user":
                    recent_user = m
                    break

            if on_topic == 0:
                inst = f'Topic: "{topic}"\nYou go first. Opening thought. Under 80 words.'
            else:
                last = ""
                for m in reversed(state["messages"]):
                    if m.get("type") != "topic" and m.get("speaker") == other["name"]:
                        last = m["text"]; break

                prompts = [
                    f'Respond to {other["name"]}: "{last}"\nPush back on their weakest point.',
                    f'{other["name"]} said: "{last}"\nGive a real-world example that counters this.',
                    f'{other["name"]} said: "{last}"\nAcknowledge something right, then hit harder.',
                    f'{other["name"]} said: "{last}"\nAsk a sharp question they\'d struggle with.',
                    f'{other["name"]} said: "{last}"\nExpose the assumption behind their argument.',
                    f'{other["name"]} said: "{last}"\nBring up something nobody has mentioned yet.',
                    f'{other["name"]} said: "{last}"\nWhy does this topic matter to someone like you?',
                    f'{other["name"]} said: "{last}"\nWhere do you both agree vs truly disagree?',
                ]
                inst = f'Topic: "{topic}"\n' + random.choice(prompts) + '\nUnder 80 words.'

                if recent_user and random.random() < 0.3:
                    inst += f'\n(Also, a human named {recent_user["user_name"]} recently said: "{recent_user["text"]}" — you may briefly reference this.)'

            try:
                text = llm(system, history, inst)
                stream_ai_message(cur, other, text, history, turn)
                on_topic += 1
                turn += 1
            except Exception as e:
                print(f"  ✖ {e}")
                state["typing"] = None

            # calculate wait
            nxt = chars[turn % 2]
            wait_time = AI_GAP
            bus.emit("waiting", {
                "name": nxt["name"], "avatar": nxt["avatar"],
                "color": nxt["color"], "gap": int(wait_time),
                "timeleft": tleft(),
            })
            next_auto = time.time() + wait_time

    # ── shutdown ──
    cnt = len([m for m in state["messages"] if m.get("type") == "message"])
    ucnt = len([m for m in state["messages"] if m.get("type") == "user"])
    bus.emit("shutdown", {
        "total_msgs": cnt, "total_topics": state["topic_num"],
        "user_msgs": ucnt, "users": len(users),
    })
    print(f"\n⏰ Done. {cnt} AI msgs, {ucnt} user msgs, {state['topic_num']} topics.")


# ━━ FLASK ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
app = Flask(__name__)

@app.route("/join", methods=["POST"])
def join():
    global color_index
    data = request.json or {}
    name = (data.get("name") or "").strip()[:20]
    if not name:
        return jsonify({"error": "name required"}), 400

    uid = str(uuid.uuid4())[:8]
    color = USER_COLORS[color_index % len(USER_COLORS)]
    color_index += 1

    users[uid] = {"name": name, "color": color, "joined": time.time()}

    # broadcast join
    sysmsg = {"type": "system", "text": f"👋 {name} joined the debate", "time": now_hm()}
    state["messages"].append(sysmsg)
    bus.emit("system", sysmsg)
    bus.emit("presence", {"users": list(users.values()), "viewers": bus.viewers})

    print(f"  👋 {name} joined ({uid})")
    return jsonify({"id": uid, "name": name, "color": color})


@app.route("/send", methods=["POST"])
def send():
    data = request.json or {}
    uid = data.get("id", "")
    text = (data.get("text") or "").strip()[:500]
    msg_id = data.get("msg_id", "")

    if uid not in users:
        return jsonify({"error": "not joined"}), 403
    if not text:
        return jsonify({"error": "empty"}), 400

    user = users[uid]

    msg = {
        "type": "user",
        "user_id": uid,
        "user_name": user["name"],
        "color": user["color"],
        "text": text,
        "time": now_hm(),
        "msg_id": msg_id,
    }
    state["messages"].append(msg)
    bus.emit("usermsg", msg)

    # queue for AI response
    user_queue.put(msg)

    print(f"  💬 {user['name']}: {text[:60]}")
    return jsonify({"ok": True})


@app.route("/stream")
def stream():
    q = bus.listen()
    def gen():
        yield f"event: fullstate\ndata: {json.dumps({
            'char_a': state['char_a'], 'char_b': state['char_b'],
            'topic': state['topic'], 'topic_num': state['topic_num'],
            'messages': state['messages'][-120:],
            'typing': state['typing'],
            'boot': BOOT, 'max_up': MAX_UP, 'timeleft': tleft(),
            'users': list(users.values()), 'viewers': bus.viewers,
        })}\n\n"
        try:
            while True:
                try:
                    yield q.get(timeout=25)
                except queue.Empty:
                    yield f"event: ping\ndata: {json.dumps({'tl': tleft(), 'v': bus.viewers})}\n\n"
        except GeneratorExit:
            bus.drop(q)
    return Response(gen(), content_type="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.route("/")
def index():
    return HTML


# ━━ HTML ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
HTML = r"""<!DOCTYPE html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1,user-scalable=no">
<title>Agora — Live AI Debate</title>
<style>
:root{
  --bg:#0b141a;--hdr:#1f2c34;--in:#1f2c34;--out:#005c4b;
  --tx:#e9edef;--tx2:#8696a0;--grn:#00a884;--blu:#53bdeb;
  --sys:#182229;--brd:#2a3942;--user-out:#005c4b;
}
*{margin:0;padding:0;box-sizing:border-box}
html,body{height:100%;overflow:hidden}
body{
  font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;
  background:#111;color:var(--tx);display:flex;justify-content:center;
}

/* ── overlay ── */
.overlay{
  position:fixed;top:0;left:0;right:0;bottom:0;
  background:rgba(0,0,0,.88);
  display:flex;align-items:center;justify-content:center;
  z-index:100;backdrop-filter:blur(8px);
}
.overlay.hidden{display:none}
.join-card{
  background:var(--hdr);border:1px solid var(--brd);
  border-radius:16px;padding:2rem 1.8rem;
  width:90%;max-width:360px;text-align:center;
}
.join-card h1{font-size:1.6rem;color:var(--tx);margin-bottom:.2rem}
.join-card h1 .accent{color:var(--grn)}
.join-card .sub{color:var(--tx2);font-size:.8rem;margin-bottom:1.5rem}
.join-card input{
  width:100%;padding:.7rem 1rem;
  background:var(--bg);border:1px solid var(--brd);
  border-radius:8px;color:var(--tx);font-size:.95rem;
  outline:none;margin-bottom:.8rem;
}
.join-card input:focus{border-color:var(--grn)}
.join-card input::placeholder{color:var(--tx2)}
.join-card .btn{
  width:100%;padding:.7rem;border:none;border-radius:8px;
  font-size:.9rem;font-weight:600;cursor:pointer;
  margin-bottom:.5rem;transition:opacity .2s;
}
.join-card .btn:hover{opacity:.85}
.join-card .btn-join{background:var(--grn);color:#fff}
.join-card .btn-watch{background:transparent;color:var(--tx2);border:1px solid var(--brd)}
.join-card .hint{color:var(--tx2);font-size:.65rem;margin-top:.8rem;line-height:1.4}

/* ── app ── */
.app{
  width:100%;max-width:500px;height:100vh;height:100dvh;
  display:flex;flex-direction:column;
  background:var(--bg);box-shadow:0 0 60px rgba(0,0,0,.6);
  position:relative;
}

/* ── header ── */
.hdr{
  display:flex;align-items:center;gap:.6rem;
  padding:.5rem .8rem;background:var(--hdr);
  min-height:56px;z-index:10;
}
.hdr-ava{
  width:40px;height:40px;border-radius:50%;
  background:var(--brd);display:flex;
  align-items:center;justify-content:center;font-size:1.1rem;
}
.hdr-info{flex:1;min-width:0}
.hdr-name{font-size:.95rem;font-weight:600;color:var(--tx)}
.hdr-name .accent{color:var(--grn)}
.hdr-sub{
  font-size:.72rem;color:var(--tx2);
  white-space:nowrap;overflow:hidden;text-overflow:ellipsis;
}
.hdr-sub .typing{color:var(--grn)}
.hdr-right{display:flex;align-items:center;gap:.3rem}
.badge{
  padding:.15rem .5rem;border-radius:10px;
  font-weight:600;font-size:.65rem;
}
.badge-die{background:rgba(255,68,68,.15);color:#f44}
.badge-msg{background:rgba(0,168,132,.15);color:var(--grn)}
.badge-eye{background:rgba(83,189,235,.12);color:var(--blu)}

/* ── chat ── */
.chat{
  flex:1;overflow-y:auto;overflow-x:hidden;
  padding:.5rem .6rem;background:var(--bg);
}
.chat::-webkit-scrollbar{width:4px}
.chat::-webkit-scrollbar-thumb{background:var(--brd);border-radius:4px}

/* ── pills ── */
.sys{text-align:center;margin:.7rem 0}
.pill{
  display:inline-block;background:var(--sys);
  color:var(--tx2);padding:.3rem .8rem;border-radius:8px;
  font-size:.75rem;max-width:90%;line-height:1.4;
  box-shadow:0 1px 1px rgba(0,0,0,.2);
}
.pill.topic{color:var(--tx);font-weight:600;font-size:.8rem}
.datesep{text-align:center;margin:.5rem 0}
.datesep span{
  background:var(--sys);color:var(--tx2);padding:.25rem .7rem;
  border-radius:8px;font-size:.68rem;text-transform:uppercase;
  letter-spacing:.04em;
}

/* ── message bubbles ── */
.msg{display:flex;flex-direction:column;margin-bottom:2px;animation:up .25s ease}
@keyframes up{from{opacity:0;transform:translateY(6px)}to{opacity:1;transform:none}}
.msg.left{align-items:flex-start;padding-right:3rem}
.msg.right{align-items:flex-end;padding-left:3rem}
.msg .body{
  position:relative;padding:.35rem .5rem .15rem .55rem;
  border-radius:8px;max-width:100%;font-size:.9rem;
  line-height:1.35;word-wrap:break-word;
  box-shadow:0 1px 1px rgba(0,0,0,.15);
}
.msg.left .body{background:var(--in);border-top-left-radius:0}
.msg.right .body{background:var(--out);border-top-right-radius:0}
.msg.left .body::before{
  content:'';position:absolute;top:0;left:-7px;
  border-right:8px solid var(--in);border-bottom:8px solid transparent;
}
.msg.right .body::before{
  content:'';position:absolute;top:0;right:-7px;
  border-left:8px solid var(--out);border-bottom:8px solid transparent;
}
.msg.cont .body{border-radius:8px}
.msg.cont .body::before{display:none}
.msg.cont{margin-top:1px}
.msg .who{font-size:.78rem;font-weight:600;margin-bottom:1px}
.msg .txt{color:var(--tx)}
.msg .meta{
  float:right;display:flex;align-items:center;gap:3px;
  margin-left:8px;margin-top:3px;
  font-size:.62rem;color:rgba(255,255,255,.4);white-space:nowrap;
}
.msg .ticks{color:var(--blu);font-size:.68rem}
.msg .spacer{display:inline-block;width:4.2rem;height:1px}

/* user msg — slightly different bg */
.msg.user-msg.right .body{background:#1a3a3a}
.msg.user-msg.right .body::before{border-left-color:#1a3a3a}
.msg.user-msg.left .body{background:#1a2a3a}
.msg.user-msg.left .body::before{border-right-color:#1a2a3a}

.cursor{
  display:inline-block;width:2px;height:.95em;
  background:var(--grn);margin-left:1px;
  animation:blinkcur .7s step-end infinite;vertical-align:text-bottom;
}
@keyframes blinkcur{0%,100%{opacity:1}50%{opacity:0}}

.typing-dots{display:inline-flex;gap:3px;align-items:center;padding:4px 0}
.typing-dots span{
  width:7px;height:7px;border-radius:50%;background:var(--tx2);
  animation:dotpulse 1.4s ease-in-out infinite;
}
.typing-dots span:nth-child(2){animation-delay:.2s}
.typing-dots span:nth-child(3){animation-delay:.4s}
@keyframes dotpulse{0%,80%,100%{opacity:.3;transform:scale(.8)}40%{opacity:1;transform:scale(1)}}

/* ── input bar ── */
.inputbar{
  display:flex;align-items:center;gap:.5rem;
  padding:.45rem .6rem;background:var(--hdr);
  border-top:1px solid var(--brd);min-height:52px;
}
.inputbar.disabled{opacity:.4;pointer-events:none}
.input-wrap{
  flex:1;display:flex;align-items:center;
  background:var(--bg);border:1px solid var(--brd);
  border-radius:22px;padding:.1rem .2rem .1rem .8rem;
  transition:border .2s;
}
.input-wrap:focus-within{border-color:var(--grn)}
.input-wrap input{
  flex:1;background:none;border:none;outline:none;
  color:var(--tx);font-size:.9rem;padding:.55rem 0;
}
.input-wrap input::placeholder{color:var(--tx2)}
.send-btn{
  width:42px;height:42px;border-radius:50%;border:none;
  background:var(--grn);color:#fff;font-size:1.2rem;
  cursor:pointer;display:flex;align-items:center;
  justify-content:center;transition:opacity .2s;flex-shrink:0;
}
.send-btn:hover{opacity:.85}
.send-btn:disabled{opacity:.3;cursor:default}

/* ── status bar (for watchers) ── */
.status-bar{
  display:flex;align-items:center;justify-content:center;
  padding:.4rem .8rem;background:var(--hdr);
  border-top:1px solid var(--brd);min-height:42px;
  font-size:.78rem;color:var(--tx2);
}
.status-bar .who-next{color:var(--grn);font-weight:600}

/* ── scroll btn ── */
.scroll-btn{
  display:none;position:absolute;bottom:68px;right:14px;
  width:40px;height:40px;background:var(--hdr);
  border:1px solid var(--brd);border-radius:50%;
  align-items:center;justify-content:center;cursor:pointer;
  z-index:20;box-shadow:0 2px 8px rgba(0,0,0,.5);
  font-size:1.1rem;color:var(--tx2);transition:opacity .2s;
}
.scroll-btn:hover{background:var(--brd)}
.scroll-btn .unread{
  position:absolute;top:-5px;right:-5px;
  background:var(--grn);color:#fff;font-size:.58rem;
  font-weight:700;min-width:18px;height:18px;
  border-radius:9px;display:flex;align-items:center;
  justify-content:center;padding:0 4px;
}

/* ── shutdown ── */
.shutdown{
  text-align:center;padding:1.5rem 1rem;
  background:rgba(244,67,54,.08);
  border-top:1px solid rgba(244,67,54,.2);margin-top:.8rem;
}
.shutdown .big{color:#f44;font-size:.9rem;font-weight:600}
.shutdown .small{color:var(--tx2);font-size:.7rem;margin-top:.3rem}

/* ── participants ── */
.part-bar{
  display:flex;gap:.3rem;padding:.3rem .8rem;
  background:rgba(0,0,0,.15);border-bottom:1px solid var(--brd);
  overflow-x:auto;font-size:.7rem;
}
.part-bar::-webkit-scrollbar{display:none}
.part-chip{
  display:flex;align-items:center;gap:.25rem;
  padding:.15rem .5rem;border-radius:12px;
  white-space:nowrap;background:rgba(255,255,255,.05);
  border:1px solid var(--brd);flex-shrink:0;
}
.part-chip .dot{width:6px;height:6px;border-radius:50%;flex-shrink:0}

@media(max-width:500px){
  .app{max-width:100%}
  .msg .body{font-size:.87rem}
}
@media(min-width:501px){
  body{align-items:center;padding:1rem 0}
  .app{border-radius:12px;height:96vh;overflow:hidden}
}
</style>
</head><body>

<!-- ── JOIN OVERLAY ── -->
<div class="overlay" id="overlay">
  <div class="join-card">
    <h1>🏛️ <span class="accent">Agora</span></h1>
    <div class="sub">live AI debate — join the conversation or just watch</div>
    <input type="text" id="name-input" placeholder="Enter your name..." maxlength="20"
      onkeydown="if(event.key==='Enter')doJoin()">
    <button class="btn btn-join" onclick="doJoin()">Join Debate</button>
    <button class="btn btn-watch" onclick="doWatch()">Just Watch</button>
    <div class="hint">
      Two AI characters debate live. You can jump in anytime.<br>
      Names are visible to everyone in the room.
    </div>
  </div>
</div>

<!-- ── APP SHELL ── -->
<div class="app" id="app" style="display:none">

  <div class="hdr">
    <div class="hdr-ava" id="hdr-ava">🏛️</div>
    <div class="hdr-info">
      <div class="hdr-name">Agora</div>
      <div class="hdr-sub" id="hdr-sub">connecting...</div>
    </div>
    <div class="hdr-right">
      <div class="badge badge-eye" id="badge-eye">👁 0</div>
      <div class="badge badge-msg" id="badge-msg">💬 0</div>
      <div class="badge badge-die" id="badge-die">💀 --</div>
    </div>
  </div>

  <div class="part-bar" id="part-bar"></div>

  <div class="chat" id="chat"></div>

  <!-- for joined users -->
  <div class="inputbar" id="inputbar" style="display:none">
    <div class="input-wrap">
      <input type="text" id="msg-input" placeholder="Type a message..."
        maxlength="500" onkeydown="if(event.key==='Enter')doSend()">
    </div>
    <button class="send-btn" id="send-btn" onclick="doSend()">▶</button>
  </div>

  <!-- for watchers -->
  <div class="status-bar" id="status-bar">
    <span id="status-text">connecting...</span>
  </div>

  <div class="scroll-btn" id="scroll-btn" onclick="jumpToBottom()">
    ↓
    <div class="unread" id="unread-badge" style="display:none">0</div>
  </div>

</div>

<script>
const $=id=>document.getElementById(id);
const chat=$('chat');

let myId=null,myName=null,myColor=null;
let isJoined=false;
let charA=null,charB=null;
let bootTime=0,maxUp=0;
let msgCount=0,lastSpeaker='',lastSide='';
let currentBubble=null,currentTxt=null;
let timerIv=null,typing_bubble=null;

/* ━━ SCROLL ━━ */
let userScrolledUp=false;
let missedCount=0;

function scroll(){
  if(!userScrolledUp)chat.scrollTop=chat.scrollHeight;
}
function jumpToBottom(){
  userScrolledUp=false;missedCount=0;
  chat.scrollTop=chat.scrollHeight;
  $('scroll-btn').style.display='none';
  $('unread-badge').style.display='none';
}
chat.addEventListener('scroll',()=>{
  const gap=chat.scrollHeight-chat.scrollTop-chat.clientHeight;
  const was=userScrolledUp;
  userScrolledUp=gap>80;
  if(!userScrolledUp&&was){missedCount=0;$('unread-badge').style.display='none'}
  $('scroll-btn').style.display=userScrolledUp?'flex':'none';
});
function notifyMissed(){
  if(userScrolledUp){
    missedCount++;
    $('unread-badge').textContent=missedCount;
    $('unread-badge').style.display='flex';
  }
}

/* ━━ JOIN / WATCH ━━ */
async function doJoin(){
  const name=$('name-input').value.trim();
  if(!name)return $('name-input').focus();
  try{
    const r=await fetch('/join',{
      method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({name})
    });
    const d=await r.json();
    if(d.error){alert(d.error);return}
    myId=d.id;myName=d.name;myColor=d.color;isJoined=true;
    $('overlay').classList.add('hidden');
    $('app').style.display='flex';
    $('inputbar').style.display='flex';
    $('status-bar').style.display='none';
    startSSE();
  }catch(e){alert('Connection failed. Try again.')}
}
function doWatch(){
  isJoined=false;
  $('overlay').classList.add('hidden');
  $('app').style.display='flex';
  $('inputbar').style.display='none';
  $('status-bar').style.display='flex';
  startSSE();
}

/* ━━ SEND MESSAGE ━━ */
async function doSend(){
  const inp=$('msg-input');
  const text=inp.value.trim();
  if(!text||!myId)return;
  inp.value='';
  const mid='m'+Date.now();
  try{
    await fetch('/send',{
      method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({id:myId,text,msg_id:mid})
    });
  }catch(e){console.error(e)}
}

/* ━━ HELPERS ━━ */
function fmtTime(s){
  s=Math.max(0,Math.floor(s));
  const h=Math.floor(s/3600),m=Math.floor(s%3600/60),sc=s%60;
  if(h>0)return h+'h '+String(m).padStart(2,'0')+'m';
  return m+'m '+String(sc).padStart(2,'0')+'s';
}
function startTimers(){
  if(timerIv)clearInterval(timerIv);
  timerIv=setInterval(()=>{
    const left=Math.max(0,maxUp-(Date.now()/1000-bootTime));
    $('badge-die').textContent='💀 '+fmtTime(left);
    if(left<300)$('badge-die').style.background='rgba(244,67,54,.3)';
    if(left<=0){$('badge-die').textContent='💀 DEAD';clearInterval(timerIv)}
  },1000);
}
function side(name){
  if(myName&&name===myName)return'right';
  if(!charA)return'left';
  return name===charA.name?'left':'right';
}
function setHdr(h){$('hdr-sub').innerHTML=h}
function setStatus(h){$('status-text').innerHTML=h}

/* ━━ PARTICIPANTS BAR ━━ */
function renderParts(usersList){
  let h='';
  if(charA)h+=`<div class="part-chip"><div class="dot" style="background:${charA.color}"></div>${charA.avatar} ${charA.name} <span style="color:var(--tx2);font-size:.6rem">AI</span></div>`;
  if(charB)h+=`<div class="part-chip"><div class="dot" style="background:${charB.color}"></div>${charB.avatar} ${charB.name} <span style="color:var(--tx2);font-size:.6rem">AI</span></div>`;
  if(usersList){
    usersList.forEach(u=>{
      h+=`<div class="part-chip"><div class="dot" style="background:${u.color}"></div>${u.name}</div>`;
    });
  }
  $('part-bar').innerHTML=h;
}

/* ━━ RENDER MESSAGES ━━ */
function addSysPill(html,cls){
  const d=document.createElement('div');
  d.className='sys';
  d.innerHTML=`<span class="pill ${cls||''}">${html}</span>`;
  chat.appendChild(d);scroll();
}
function addDateSep(t){
  const d=document.createElement('div');
  d.className='datesep';
  d.innerHTML=`<span>${t}</span>`;
  chat.appendChild(d);scroll();
}
function addTopicPill(t){
  addSysPill(`📋 Topic #${t.number}<br>"${t.text}"`,'topic');
}

function removeTypingBubble(){
  if(typing_bubble){typing_bubble.remove();typing_bubble=null}
}
function addTypingBubble(name,avatar,color){
  removeTypingBubble();
  const s=side(name);
  const cont=(lastSpeaker===name);
  const d=document.createElement('div');
  d.className=`msg ${s}${cont?' cont':''}`;
  let h='';
  if(!cont)h+=`<div class="who" style="color:${color}">${avatar} ${name}</div>`;
  h+=`<div class="body"><div class="typing-dots"><span></span><span></span><span></span></div></div>`;
  d.innerHTML=h;
  chat.appendChild(d);typing_bubble=d;scroll();
}

function startBubble(speaker,avatar,color,role,timeStr){
  removeTypingBubble();
  const s=side(speaker);
  const cont=(lastSpeaker===speaker);
  const d=document.createElement('div');
  d.className=`msg ${s}${cont?' cont':''}`;
  let h='';
  if(!cont)h+=`<div class="who" style="color:${color}">${avatar} ${speaker}</div>`;
  h+=`<div class="body">`;
  h+=`<span class="meta"><span class="tm">${timeStr}</span></span>`;
  h+=`<span class="txt"></span><span class="cursor"></span><span class="spacer"></span>`;
  h+=`</div>`;
  d.innerHTML=h;
  chat.appendChild(d);
  currentBubble=d;currentTxt=d.querySelector('.txt');
  scroll();
}
function appendWord(w){
  if(!currentTxt)return;
  const t=currentTxt.textContent;
  currentTxt.textContent=t?(t+' '+w):w;
  scroll();
}
function finishBubble(speaker,timeStr){
  if(currentBubble){
    const c=currentBubble.querySelector('.cursor');if(c)c.remove();
    const m=currentBubble.querySelector('.meta');
    if(m)m.innerHTML=`<span class="tm">${timeStr}</span><span class="ticks"> ✓✓</span>`;
  }
  lastSpeaker=speaker;
  currentBubble=null;currentTxt=null;
  msgCount++;$('badge-msg').textContent='💬 '+msgCount;
  notifyMissed();scroll();
}

function addFullMsg(m){
  const s=side(m.speaker||m.user_name);
  const cont=(lastSpeaker===(m.speaker||m.user_name));
  const isUser=(m.type==='user');
  const d=document.createElement('div');
  d.className=`msg ${s}${cont?' cont':''}${isUser?' user-msg':''}`;
  const who=m.speaker||m.user_name;
  const col=m.color||'#aaa';
  const ava=m.avatar||'';
  let h='';
  if(!cont)h+=`<div class="who" style="color:${col}">${ava}${ava?' ':''}${who}</div>`;
  h+=`<div class="body">`;
  h+=`<span class="meta"><span class="tm">${m.time||''}</span><span class="ticks"> ✓✓</span></span>`;
  h+=`<span class="txt">${m.text}</span><span class="spacer"></span>`;
  h+=`</div>`;
  d.innerHTML=h;
  chat.appendChild(d);
  lastSpeaker=who;
  if(!isUser)msgCount++;
}

function addUserMsg(m){
  const s=(myName&&m.user_name===myName)?'right':'left';
  const cont=(lastSpeaker===m.user_name);
  const d=document.createElement('div');
  d.className=`msg ${s}${cont?' cont':''} user-msg`;
  let h='';
  if(!cont)h+=`<div class="who" style="color:${m.color}">${m.user_name}</div>`;
  h+=`<div class="body">`;
  h+=`<span class="meta"><span class="tm">${m.time||''}</span><span class="ticks"> ✓✓</span></span>`;
  h+=`<span class="txt">${m.text}</span><span class="spacer"></span>`;
  h+=`</div>`;
  d.innerHTML=h;
  chat.appendChild(d);
  lastSpeaker=m.user_name;
  scroll();
}

/* ━━ SSE ━━ */
function startSSE(){
  setHdr('connecting...');
  const es=new EventSource('/stream');

  es.addEventListener('fullstate',e=>{
    const d=JSON.parse(e.data);
    bootTime=d.boot;maxUp=d.max_up;
    charA=d.char_a;charB=d.char_b;
    renderParts(d.users);
    $('badge-eye').textContent='👁 '+(d.viewers||0);

    if(charA&&charB)setHdr(`${charA.avatar} ${charA.name}, ${charB.avatar} ${charB.name}`);

    chat.innerHTML='';msgCount=0;lastSpeaker='';
    addSysPill('🏛️ <b>Agora</b> — AI-generated debate · anyone can join','');
    addDateSep('TODAY');

    if(d.messages){
      d.messages.forEach(m=>{
        if(m.type==='topic')addTopicPill(m);
        else if(m.type==='message')addFullMsg(m);
        else if(m.type==='user')addFullMsg(m);
        else if(m.type==='system')addSysPill(m.text,'');
      });
    }

    $('badge-msg').textContent='💬 '+msgCount;
    startTimers();scroll();
    setStatus(`<span class="who-next">● LIVE</span> — debate in progress`);
  });

  es.addEventListener('newtopic',e=>{
    const d=JSON.parse(e.data);
    addTopicPill(d);
    if(charA&&charB)setHdr(`${charA.avatar} ${charA.name}, ${charB.avatar} ${charB.name}`);
  });

  es.addEventListener('typing',e=>{
    const d=JSON.parse(e.data);
    addTypingBubble(d.name,d.avatar,d.color);
    setHdr(`<span class="typing">${d.name} is typing...</span>`);
    setStatus(`<span style="color:${d.color}">${d.avatar} ${d.name}</span> is typing...`);
  });

  es.addEventListener('msgstart',e=>{
    const d=JSON.parse(e.data);
    startBubble(d.speaker,d.avatar,d.color,d.role,d.time);
    setHdr(`<span class="typing">${d.speaker} is speaking...</span>`);
    setStatus(`<span style="color:${d.color}">${d.avatar} ${d.speaker}</span> is writing...`);
  });

  es.addEventListener('word',e=>{
    appendWord(JSON.parse(e.data).w);
  });

  es.addEventListener('msgdone',e=>{
    const d=JSON.parse(e.data);
    finishBubble(d.speaker,d.time);
    if(charA&&charB)setHdr(`${charA.avatar} ${charA.name}, ${charB.avatar} ${charB.name}`);
    setStatus(`<span class="who-next">● LIVE</span> — debate in progress`);
  });

  es.addEventListener('usermsg',e=>{
    const d=JSON.parse(e.data);
    addUserMsg(d);
  });

  es.addEventListener('system',e=>{
    const d=JSON.parse(e.data);
    addSysPill(d.text,'');
  });

  es.addEventListener('presence',e=>{
    const d=JSON.parse(e.data);
    renderParts(d.users);
    $('badge-eye').textContent='👁 '+(d.viewers||0);
  });

  es.addEventListener('waiting',e=>{
    const d=JSON.parse(e.data);
    let gap=d.gap;
    setStatus(
      `<span style="color:${d.color}">${d.avatar} ${d.name}</span> responds in `+
      `<span id="gap-cd">${gap}s</span>`
    );
    const iv=setInterval(()=>{
      gap--;
      const el=document.getElementById('gap-cd');
      if(el)el.textContent=gap+'s';
      if(gap<=0){clearInterval(iv);setStatus(`<span class="who-next">● LIVE</span> — next message incoming...`);}
    },1000);
  });

  es.addEventListener('shutdown',e=>{
    const d=JSON.parse(e.data);
    removeTypingBubble();
    const div=document.createElement('div');
    div.className='shutdown';
    div.innerHTML=
      `<div class="big">⚠️ Server shutting down</div>`+
      `<div class="small">Next cycle starts on schedule</div>`+
      `<div class="small">${d.total_msgs} AI messages · ${d.user_msgs} user messages · ${d.total_topics} topics</div>`+
      `<div class="small">${d.users} humans participated</div>`;
    chat.appendChild(div);scroll();
    setHdr('offline — next cycle on schedule');
    setStatus('🔴 Server offline');
    $('badge-die').textContent='💀 DEAD';
    $('badge-die').style.background='rgba(244,67,54,.4)';
    if(timerIv)clearInterval(timerIv);
    if(isJoined){
      $('inputbar').classList.add('disabled');
      $('msg-input').placeholder='Server offline';
    }
  });

  es.addEventListener('ping',e=>{
    const d=JSON.parse(e.data);
    $('badge-eye').textContent='👁 '+(d.v||0);
  });

  es.onerror=()=>{
    setHdr('reconnecting...');
    setStatus('⚠️ reconnecting...');
    es.close();
    setTimeout(startSSE,3000);
  };
}

/* auto-focus name input */
$('name-input').focus();
</script>
</body></html>"""


# ━━ START ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
if __name__ == "__main__":
    print("=" * 50)
    print("🏛️  Agora — Live AI Debate with Public Chat")
    print(f"   model      : {MODEL}")
    print(f"   ai gap     : {AI_GAP}s between AI messages")
    print(f"   user wait  : {USER_WAIT}s before AI responds to human")
    print(f"   msgs/topic : {MIN_PER_TOPIC}-{MAX_PER_TOPIC}")
    print(f"   max uptime : {MAX_UP//3600}h {MAX_UP%3600//60}m")
    print("=" * 50)

    t = threading.Thread(target=engine, daemon=True)
    t.start()

    app.run(host="0.0.0.0", port=PORT, threaded=True)













            
