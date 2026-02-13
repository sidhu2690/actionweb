#!/usr/bin/env python3
"""Live AI debate — WhatsApp-style real-time chat."""

import json, os, re, random, time, queue, threading
from datetime import datetime, timezone
from flask import Flask, Response

# ━━ CONFIG ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
BOOT     = time.time()
MAX_UP   = 21300                     # 5 h 55 m
MSG_GAP  = 30                        # seconds between messages
MODEL    = "llama-3.1-8b-instant"
BACKUP   = "meta-llama/llama-4-scout-17b-16e-instruct"
PORT     = 8080
MIN_PER_TOPIC = 20                   # ~10 min minimum per topic at 30s gap
MAX_PER_TOPIC = 30                   # ~15 min max per topic

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

# ━━ SSE BUS ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
class Bus:
    def __init__(self):
        self._q = []
        self._lock = threading.Lock()

    def listen(self):
        q = queue.Queue(maxsize=300)
        with self._lock:
            self._q.append(q)
        return q

    def drop(self, q):
        with self._lock:
            try: self._q.remove(q)
            except ValueError: pass

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

# ━━ ENGINE ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
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
            used.clear()
            pool = list(ALL_TOPICS)
        t = random.choice(pool)
        used.add(t)
        return t

    # first topic
    topic = pick()
    state["topic"], state["topic_num"] = topic, 1
    print(f"📋 topic #1: \"{topic}\" ({per_topic} messages planned)")

    bus.emit("init", {
        "char_a": chars[0], "char_b": chars[1],
        "topic": topic, "topic_num": 1,
        "boot": BOOT, "max_up": MAX_UP,
    })

    tmsg = {"type": "topic", "text": topic, "number": 1, "time": now_hm()}
    state["messages"].append(tmsg)
    bus.emit("newtopic", tmsg)

    time.sleep(5)

    while tleft() > 60:

        # ── rotate topic ──
        if on_topic >= per_topic:
            topic = pick()
            state["topic"] = topic
            state["topic_num"] += 1
            on_topic = 0
            per_topic = random.randint(MIN_PER_TOPIC, MAX_PER_TOPIC)
            history = history[-4:]

            tmsg = {"type": "topic", "text": topic,
                    "number": state["topic_num"], "time": now_hm()}
            state["messages"].append(tmsg)
            bus.emit("newtopic", tmsg)
            print(f"\n📋 topic #{state['topic_num']}: \"{topic}\" ({per_topic} msgs)")
            time.sleep(5)
            continue

        turn_start = time.time()

        cur = chars[turn % 2]
        other = chars[(turn + 1) % 2]

        # ── typing indicator ──
        state["typing"] = cur["name"]
        bus.emit("typing", {
            "name": cur["name"], "avatar": cur["avatar"],
            "color": cur["color"], "role": cur["role"],
        })

        # ── build prompt ──
        system = f"""You are {cur['name']} — {cur['role']}.
Personality: {cur['personality']}.
Style: {cur['style']}.
Debating "{topic}" with {other['name']} ({other['role']}).
Under 80 words. Sharp, direct, conversational.
Don't start with your name. No quotes. Engage their points.
This is message {on_topic + 1} of an ongoing conversation — keep it flowing naturally.
Reference previous points when relevant. Don't repeat yourself."""

        if on_topic == 0:
            inst = f'Topic: "{topic}"\nYou go first. Opening thought. Under 80 words.'
        elif on_topic == 1:
            last = ""
            for m in reversed(state["messages"]):
                if m.get("type") != "topic" and m.get("speaker") == other["name"]:
                    last = m["text"]; break
            inst = f'Topic: "{topic}"\n{other["name"]} opened with: "{last}"\nChallenge their opening. Under 80 words.'
        else:
            last = ""
            for m in reversed(state["messages"]):
                if m.get("type") != "topic" and m.get("speaker") == other["name"]:
                    last = m["text"]; break
            # vary the instruction to keep conversation dynamic
            prompts = [
                f'Respond to what {other["name"]} just said: "{last}"\nPush back on their weakest point. Under 80 words.',
                f'{other["name"]} said: "{last}"\nGive a real-world example that counters their point. Under 80 words.',
                f'{other["name"]} said: "{last}"\nAcknowledge one thing they got right, then hit harder. Under 80 words.',
                f'{other["name"]} said: "{last}"\nAsk them a sharp question they\'d struggle to answer. Under 80 words.',
                f'{other["name"]} said: "{last}"\nExpose the assumption behind their argument. Under 80 words.',
                f'{other["name"]} said: "{last}"\nBring up something neither of you have mentioned yet. Under 80 words.',
                f'{other["name"]} said: "{last}"\nGet personal — why does this topic matter to someone like you? Under 80 words.',
                f'{other["name"]} said: "{last}"\nSummarize where you both agree and where the real disagreement is. Under 80 words.',
            ]
            inst = f'Topic: "{topic}"\n' + random.choice(prompts)

        # ── call LLM ──
        try:
            text = llm(system, history, inst)
        except Exception as e:
            print(f"  ✖ {e}")
            time.sleep(10)
            continue

        generation_time = time.time() - turn_start

        # ── calculate word display speed ──
        words = text.split()
        display_budget = max(8, MSG_GAP - generation_time - 5)
        wps = max(0.08, min(display_budget / max(len(words), 1), 0.6))

        # ── start bubble ──
        bus.emit("msgstart", {
            "speaker": cur["name"], "avatar": cur["avatar"],
            "color": cur["color"], "role": cur["role"],
            "time": now_hm(),
        })

        # ── stream word by word ──
        for i, w in enumerate(words):
            bus.emit("word", {"w": w, "i": i, "of": len(words)})
            time.sleep(wps)

        # ── done ──
        msg = {
            "type": "message", "speaker": cur["name"],
            "avatar": cur["avatar"], "color": cur["color"],
            "role": cur["role"], "text": text, "time": now_hm(),
        }
        state["messages"].append(msg)
        state["typing"] = None
        history.append({
            "role": "assistant" if turn % 2 == 0 else "user",
            "content": text
        })

        bus.emit("msgdone", {"speaker": cur["name"], "text": text, "time": now_hm()})

        total_msgs = len([m for m in state["messages"] if m.get("type") != "topic"])
        print(f"  {cur['avatar']} {cur['name']} [{on_topic+1}/{per_topic}] ({total_msgs} total): {text[:60]}...")

        turn += 1
        on_topic += 1

        # ── wait remaining gap ──
        elapsed = time.time() - turn_start
        wait = MSG_GAP - elapsed
        if wait > 0 and tleft() > 90:
            nxt = chars[turn % 2]
            bus.emit("waiting", {
                "name": nxt["name"], "avatar": nxt["avatar"],
                "color": nxt["color"], "gap": int(wait),
                "timeleft": tleft(),
            })
            time.sleep(wait)

    # ── shutdown ──
    cnt = len([m for m in state["messages"] if m.get("type") != "topic"])
    bus.emit("shutdown", {"total_msgs": cnt, "total_topics": state["topic_num"]})
    print(f"\n⏰ Done. {cnt} messages, {state['topic_num']} topics.")


# ━━ FLASK ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
app = Flask(__name__)

@app.route("/stream")
def stream():
    q = bus.listen()
    def gen():
        yield f"event: fullstate\ndata: {json.dumps({
            'char_a': state['char_a'], 'char_b': state['char_b'],
            'topic': state['topic'], 'topic_num': state['topic_num'],
            'messages': state['messages'][-100:],
            'typing': state['typing'],
            'boot': BOOT, 'max_up': MAX_UP, 'timeleft': tleft(),
        })}\n\n"
        try:
            while True:
                try:
                    yield q.get(timeout=25)
                except queue.Empty:
                    yield f"event: ping\ndata: {json.dumps({'tl': tleft()})}\n\n"
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
<title>MindDuel — Live AI Debate</title>
<style>
:root{
  --wa-bg:#0b141a;
  --wa-hdr:#1f2c34;
  --wa-in:#1f2c34;
  --wa-out:#005c4b;
  --wa-text:#e9edef;
  --wa-text2:#8696a0;
  --wa-green:#00a884;
  --wa-blue:#53bdeb;
  --wa-sys:#182229;
  --wa-border:#2a3942;
}
*{margin:0;padding:0;box-sizing:border-box}
html,body{height:100%;overflow:hidden}
body{
  font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;
  background:#111;color:var(--wa-text);
  display:flex;justify-content:center;
}

.app{
  width:100%;max-width:500px;height:100vh;height:100dvh;
  display:flex;flex-direction:column;
  background:var(--wa-bg);
  box-shadow:0 0 60px rgba(0,0,0,.6);
  position:relative;
}

/* ── header ── */
.hdr{
  display:flex;align-items:center;gap:.6rem;
  padding:.5rem .8rem;
  background:var(--wa-hdr);
  min-height:56px;z-index:10;
}
.hdr-ava{
  width:40px;height:40px;border-radius:50%;
  background:var(--wa-border);
  display:flex;align-items:center;justify-content:center;
  font-size:1.2rem;
}
.hdr-info{flex:1;min-width:0}
.hdr-name{font-size:.95rem;font-weight:600;color:var(--wa-text)}
.hdr-sub{font-size:.75rem;color:var(--wa-text2);
  white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.hdr-sub .typing{color:var(--wa-green)}
.hdr-right{display:flex;align-items:center;gap:.3rem}
.hdr-timer{
  background:rgba(255,68,68,.15);color:#f44;
  padding:.15rem .5rem;border-radius:10px;
  font-weight:600;font-size:.68rem;
}
.hdr-msgs{
  background:rgba(0,168,132,.15);color:var(--wa-green);
  padding:.15rem .5rem;border-radius:10px;
  font-weight:600;font-size:.68rem;
}

/* ── chat ── */
.chat{
  flex:1;overflow-y:auto;overflow-x:hidden;
  padding:.5rem .6rem;
  background:var(--wa-bg);
  background-image:
    radial-gradient(circle at 20% 50%,rgba(0,168,132,.02) 0%,transparent 50%),
    radial-gradient(circle at 80% 20%,rgba(83,189,235,.02) 0%,transparent 50%);
}
.chat::-webkit-scrollbar{width:4px}
.chat::-webkit-scrollbar-thumb{background:var(--wa-border);border-radius:4px}

/* ── system pill ── */
.sys{text-align:center;margin:.8rem 0}
.pill{
  display:inline-block;background:var(--wa-sys);
  color:var(--wa-text2);padding:.35rem .8rem;
  border-radius:8px;font-size:.75rem;
  max-width:85%;line-height:1.4;
  box-shadow:0 1px 1px rgba(0,0,0,.2);
}
.pill.topic{color:var(--wa-text);font-weight:600}

/* ── date sep ── */
.datesep{text-align:center;margin:.6rem 0}
.datesep span{
  background:var(--wa-sys);color:var(--wa-text2);
  padding:.3rem .8rem;border-radius:8px;
  font-size:.7rem;text-transform:uppercase;
  letter-spacing:.04em;box-shadow:0 1px 1px rgba(0,0,0,.2);
}

/* ── message ── */
.msg{
  display:flex;flex-direction:column;
  margin-bottom:2px;animation:fadeUp .25s ease;
}
@keyframes fadeUp{from{opacity:0;transform:translateY(6px)}to{opacity:1;transform:none}}
.msg.left{align-items:flex-start;padding-right:3rem}
.msg.right{align-items:flex-end;padding-left:3rem}

.msg .body{
  position:relative;
  padding:.4rem .5rem .15rem .55rem;
  border-radius:8px;max-width:100%;
  font-size:.9rem;line-height:1.35;
  word-wrap:break-word;
  box-shadow:0 1px 1px rgba(0,0,0,.15);
}
.msg.left .body{background:var(--wa-in);border-top-left-radius:0}
.msg.right .body{background:var(--wa-out);border-top-right-radius:0}

.msg.left .body::before{
  content:'';position:absolute;top:0;left:-7px;
  border-top:0 solid transparent;
  border-right:8px solid var(--wa-in);
  border-bottom:8px solid transparent;
}
.msg.right .body::before{
  content:'';position:absolute;top:0;right:-7px;
  border-top:0 solid transparent;
  border-left:8px solid var(--wa-out);
  border-bottom:8px solid transparent;
}

.msg.cont .body{border-radius:8px}
.msg.cont .body::before{display:none}
.msg.cont{margin-top:1px}

.msg .who{font-size:.8rem;font-weight:600;margin-bottom:1px}
.msg .txt{color:var(--wa-text)}
.msg .meta{
  float:right;display:flex;align-items:center;gap:3px;
  margin-left:8px;margin-top:3px;
  font-size:.65rem;color:rgba(255,255,255,.45);
  white-space:nowrap;
}
.msg .ticks{color:var(--wa-blue);font-size:.7rem}
.msg .spacer{display:inline-block;width:4.5rem;height:1px}

.cursor{
  display:inline-block;width:2px;height:.95em;
  background:var(--wa-green);margin-left:1px;
  animation:blinkcur .7s step-end infinite;
  vertical-align:text-bottom;
}
@keyframes blinkcur{0%,100%{opacity:1}50%{opacity:0}}

/* ── typing dots ── */
.typing-dots{
  display:inline-flex;gap:3px;align-items:center;padding:4px 0;
}
.typing-dots span{
  width:7px;height:7px;border-radius:50%;
  background:var(--wa-text2);
  animation:dotpulse 1.4s ease-in-out infinite;
}
.typing-dots span:nth-child(2){animation-delay:.2s}
.typing-dots span:nth-child(3){animation-delay:.4s}
@keyframes dotpulse{0%,80%,100%{opacity:.3;transform:scale(.8)}40%{opacity:1;transform:scale(1)}}

/* ── bottom bar ── */
.bottom{
  display:flex;align-items:center;
  padding:.5rem .8rem;
  background:var(--wa-hdr);min-height:48px;
  gap:.5rem;border-top:1px solid var(--wa-border);
}
.bottom-status{flex:1;font-size:.8rem;color:var(--wa-text2)}
.bottom-status .who-next{color:var(--wa-green);font-weight:600}

/* ── scroll button ── */
.scroll-btn{
  display:none;
  position:absolute;bottom:60px;right:16px;
  width:42px;height:42px;
  background:var(--wa-hdr);border:1px solid var(--wa-border);
  border-radius:50%;
  align-items:center;justify-content:center;
  cursor:pointer;z-index:20;
  box-shadow:0 2px 10px rgba(0,0,0,.5);
  font-size:1.2rem;color:var(--wa-text2);
  transition:opacity .2s;
}
.scroll-btn:hover{background:var(--wa-border)}
.scroll-btn .unread{
  position:absolute;top:-4px;right:-4px;
  background:var(--wa-green);color:#fff;
  font-size:.6rem;font-weight:700;
  min-width:18px;height:18px;
  border-radius:9px;
  display:flex;align-items:center;justify-content:center;
  padding:0 4px;
}

/* ── shutdown ── */
.shutdown{
  text-align:center;padding:1.5rem 1rem;
  background:rgba(244,67,54,.1);
  border-top:1px solid rgba(244,67,54,.3);
  margin-top:1rem;
}
.shutdown .big{color:#f44;font-size:.9rem;font-weight:600}
.shutdown .small{color:var(--wa-text2);font-size:.7rem;margin-top:.3rem}

@media(max-width:500px){
  .app{max-width:100%}
  .msg .body{font-size:.88rem}
}
@media(min-width:501px){
  body{align-items:center;padding:1rem 0}
  .app{border-radius:12px;height:96vh;overflow:hidden}
}
</style>
</head><body>
<div class="app">

  <div class="hdr">
    <div class="hdr-ava" id="hdr-ava">⚔️</div>
    <div class="hdr-info">
      <div class="hdr-name">MindDuel</div>
      <div class="hdr-sub" id="hdr-sub">connecting...</div>
    </div>
    <div class="hdr-right">
      <div class="hdr-msgs" id="msg-count">💬 0</div>
      <div class="hdr-timer" id="timer">💀 --:--</div>
    </div>
  </div>

  <div class="chat" id="chat"></div>

  <div class="bottom" id="bottom">
    <div class="bottom-status" id="bottom-status">connecting...</div>
  </div>

  <div class="scroll-btn" id="scroll-btn" onclick="jumpToBottom()">
    ↓
    <div class="unread" id="unread-badge" style="display:none">0</div>
  </div>

</div>

<script>
const $=id=>document.getElementById(id);
const chat=$('chat');

let charA=null,charB=null;
let bootTime=0,maxUp=0;
let msgCount=0;
let lastSpeaker='';
let currentBubble=null;
let currentTxt=null;
let timerIv=null;
let typing_bubble=null;

/* ━━ SCROLL MANAGEMENT ━━ */
let userScrolledUp=false;
let missedWhileScrolled=0;

function scroll(){
  if(!userScrolledUp){
    chat.scrollTop=chat.scrollHeight;
  }
}

function jumpToBottom(){
  userScrolledUp=false;
  missedWhileScrolled=0;
  chat.scrollTop=chat.scrollHeight;
  $('scroll-btn').style.display='none';
  $('unread-badge').style.display='none';
}

chat.addEventListener('scroll',()=>{
  const gap=chat.scrollHeight-chat.scrollTop-chat.clientHeight;
  const wasUp=userScrolledUp;
  userScrolledUp=gap>80;

  if(!userScrolledUp&&wasUp){
    missedWhileScrolled=0;
    $('unread-badge').style.display='none';
  }

  $('scroll-btn').style.display=userScrolledUp?'flex':'none';
});

function notifyMissed(){
  if(userScrolledUp){
    missedWhileScrolled++;
    const badge=$('unread-badge');
    badge.textContent=missedWhileScrolled;
    badge.style.display='flex';
  }
}

/* ━━ HELPERS ━━ */
function fmtTime(s){
  s=Math.max(0,Math.floor(s));
  const h=Math.floor(s/3600),m=Math.floor(s%3600/60),sec=s%60;
  if(h>0)return h+'h '+String(m).padStart(2,'0')+'m';
  return m+'m '+String(sec).padStart(2,'0')+'s';
}

function startTimers(){
  if(timerIv)clearInterval(timerIv);
  timerIv=setInterval(()=>{
    const left=Math.max(0,maxUp-(Date.now()/1000-bootTime));
    $('timer').textContent='💀 '+fmtTime(left);
    if(left<300){
      $('timer').style.background='rgba(244,67,54,.3)';
      $('timer').style.animation='none';
    }
    if(left<=0){
      $('timer').textContent='💀 DEAD';
      clearInterval(timerIv);
    }
  },1000);
}

function side(name){
  if(!charA)return'left';
  return name===charA.name?'left':'right';
}

function setHdrSub(html){$('hdr-sub').innerHTML=html}
function setBottom(html){$('bottom-status').innerHTML=html}

/* ━━ RENDER ━━ */
function addSysPill(html,cls){
  const d=document.createElement('div');
  d.className='sys';
  d.innerHTML=`<span class="pill ${cls||''}">${html}</span>`;
  chat.appendChild(d);
  scroll();
}

function addDateSep(text){
  const d=document.createElement('div');
  d.className='datesep';
  d.innerHTML=`<span>${text}</span>`;
  chat.appendChild(d);
  scroll();
}

function addTopicPill(t){
  addSysPill(`📋 Topic #${t.number}<br>"${t.text}"`,'topic');
}

function removeTypingBubble(){
  if(typing_bubble){typing_bubble.remove();typing_bubble=null}
}

function addTypingBubble(name,avatar,color,role){
  removeTypingBubble();
  const s=side(name);
  const cont=(lastSpeaker===name);
  const d=document.createElement('div');
  d.className=`msg ${s}${cont?' cont':''}`;
  let h='';
  if(!cont)h+=`<div class="who" style="color:${color}">${avatar} ${name}</div>`;
  h+=`<div class="body"><div class="typing-dots"><span></span><span></span><span></span></div></div>`;
  d.innerHTML=h;
  chat.appendChild(d);
  typing_bubble=d;
  scroll();
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
  h+=`<span class="txt"></span>`;
  h+=`<span class="cursor"></span>`;
  h+=`<span class="spacer"></span>`;
  h+=`</div>`;
  d.innerHTML=h;
  chat.appendChild(d);
  currentBubble=d;
  currentTxt=d.querySelector('.txt');
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
    const cur=currentBubble.querySelector('.cursor');
    if(cur)cur.remove();
    const meta=currentBubble.querySelector('.meta');
    if(meta)meta.innerHTML=`<span class="tm">${timeStr}</span><span class="ticks"> ✓✓</span>`;
  }
  lastSpeaker=speaker;
  currentBubble=null;
  currentTxt=null;
  msgCount++;
  $('msg-count').textContent='💬 '+msgCount;
  notifyMissed();
  scroll();
}

function addFullMsg(m){
  const s=side(m.speaker);
  const cont=(lastSpeaker===m.speaker);
  const d=document.createElement('div');
  d.className=`msg ${s}${cont?' cont':''}`;
  let h='';
  if(!cont)h+=`<div class="who" style="color:${m.color}">${m.avatar} ${m.speaker}</div>`;
  h+=`<div class="body">`;
  h+=`<span class="meta"><span class="tm">${m.time||''}</span><span class="ticks"> ✓✓</span></span>`;
  h+=`<span class="txt">${m.text}</span>`;
  h+=`<span class="spacer"></span>`;
  h+=`</div>`;
  d.innerHTML=h;
  chat.appendChild(d);
  lastSpeaker=m.speaker;
  msgCount++;
}

/* ━━ SSE ━━ */
function connect(){
  setHdrSub('connecting...');
  const es=new EventSource('/stream');

  es.addEventListener('fullstate',e=>{
    const d=JSON.parse(e.data);
    bootTime=d.boot;maxUp=d.max_up;
    charA=d.char_a;charB=d.char_b;

    if(charA&&charB){
      setHdrSub(`${charA.avatar} ${charA.name}, ${charB.avatar} ${charB.name}`);
    }

    chat.innerHTML='';
    msgCount=0;lastSpeaker='';

    addSysPill('🔒 AI-generated debate · live via Groq','');
    addDateSep('TODAY');

    if(d.messages){
      d.messages.forEach(m=>{
        if(m.type==='topic')addTopicPill(m);
        else if(m.type==='message')addFullMsg(m);
      });
    }

    $('msg-count').textContent='💬 '+msgCount;
    startTimers();
    scroll();
    setBottom(`<span class="who-next">● LIVE</span> — waiting for next message...`);
  });

  es.addEventListener('newtopic',e=>{
    const d=JSON.parse(e.data);
    addTopicPill(d);
    if(charA&&charB)setHdrSub(`${charA.avatar} ${charA.name}, ${charB.avatar} ${charB.name}`);
  });

  es.addEventListener('typing',e=>{
    const d=JSON.parse(e.data);
    addTypingBubble(d.name,d.avatar,d.color,d.role);
    setHdrSub(`<span class="typing">${d.name} is typing...</span>`);
    setBottom(`<span style="color:${d.color}">${d.avatar} ${d.name}</span> is typing...`);
  });

  es.addEventListener('msgstart',e=>{
    const d=JSON.parse(e.data);
    startBubble(d.speaker,d.avatar,d.color,d.role,d.time);
    setHdrSub(`<span class="typing">${d.speaker} is speaking...</span>`);
    setBottom(`<span style="color:${d.color}">${d.avatar} ${d.speaker}</span> is speaking...`);
  });

  es.addEventListener('word',e=>{
    const d=JSON.parse(e.data);
    appendWord(d.w);
  });

  es.addEventListener('msgdone',e=>{
    const d=JSON.parse(e.data);
    finishBubble(d.speaker,d.time);
    if(charA&&charB)setHdrSub(`${charA.avatar} ${charA.name}, ${charB.avatar} ${charB.name}`);
    setBottom(`<span class="who-next">● LIVE</span> — waiting for next message...`);
  });

  es.addEventListener('waiting',e=>{
    const d=JSON.parse(e.data);
    let gap=d.gap;
    setBottom(
      `<span style="color:${d.color}">${d.avatar} ${d.name}</span> responds in `+
      `<span id="gap-cd">${gap}s</span>`
    );
    const iv=setInterval(()=>{
      gap--;
      const el=document.getElementById('gap-cd');
      if(el)el.textContent=gap+'s';
      if(gap<=0){
        clearInterval(iv);
        setBottom(`<span class="who-next">● LIVE</span> — next message incoming...`);
      }
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
      `<div class="small">${d.total_msgs} messages · ${d.total_topics} topics debated</div>`;
    chat.appendChild(div);
    scroll();
    setHdrSub('offline — next cycle on schedule');
    setBottom('🔴 Server offline');
    $('timer').textContent='💀 DEAD';
    $('timer').style.background='rgba(244,67,54,.4)';
    if(timerIv)clearInterval(timerIv);
  });

  es.addEventListener('ping',e=>{});

  es.onerror=()=>{
    setHdrSub('reconnecting...');
    setBottom('⚠️ connection lost — reconnecting...');
    es.close();
    setTimeout(connect,3000);
  };
}

connect();
</script>
</body></html>"""


# ━━ START ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
if __name__ == "__main__":
    print("=" * 50)
    print("⚔️  MindDuel — Live AI Debate")
    print(f"   model      : {MODEL}")
    print(f"   gap        : {MSG_GAP}s between messages")
    print(f"   msgs/topic : {MIN_PER_TOPIC}-{MAX_PER_TOPIC} (~{MIN_PER_TOPIC*MSG_GAP//60}-{MAX_PER_TOPIC*MSG_GAP//60} min each)")
    print(f"   max uptime : {MAX_UP//3600}h {MAX_UP%3600//60}m")
    print("=" * 50)

    t = threading.Thread(target=engine, daemon=True)
    t.start()

    app.run(host="0.0.0.0", port=PORT, threaded=True)
