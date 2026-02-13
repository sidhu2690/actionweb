#!/usr/bin/env python3
"""Live AI debate server — two random characters argue in real-time, forever."""

import json, os, re, random, time, queue, threading
from datetime import datetime, timezone
from flask import Flask, Response

# ━━ CONFIG ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
BOOT      = time.time()
MAX_UP    = 21300                    # 5 h 55 m
MSG_GAP   = 30                       # seconds between messages
MODEL     = "llama-3.1-8b-instant"
BACKUP    = "meta-llama/llama-4-scout-17b-16e-instruct"
PORT      = 8080

# ━━ LOAD DATA ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
with open("characters.json") as f:
    ALL_CHARS = json.load(f)
with open("topics.json") as f:
    ALL_TOPICS = json.load(f)

# ━━ SHARED STATE ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
state = {
    "char_a":    None,
    "char_b":    None,
    "topic":     None,
    "topic_num": 0,
    "messages":  [],
    "typing":    None,
    "boot":      BOOT,
    "max_up":    MAX_UP,
}

# ━━ EVENT BUS (SSE) ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
class EventBus:
    def __init__(self):
        self._listeners = []
        self._lock = threading.Lock()

    def listen(self):
        q = queue.Queue(maxsize=200)
        with self._lock:
            self._listeners.append(q)
        return q

    def unlisten(self, q):
        with self._lock:
            try:
                self._listeners.remove(q)
            except ValueError:
                pass

    def emit(self, event, data):
        msg = f"event: {event}\ndata: {json.dumps(data)}\n\n"
        dead = []
        with self._lock:
            for q in self._listeners:
                try:
                    q.put_nowait(msg)
                except queue.Full:
                    dead.append(q)
            for q in dead:
                try:
                    self._listeners.remove(q)
                except ValueError:
                    pass

bus = EventBus()

# ━━ GROQ CALLER ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
_groq_client = None

def get_groq():
    global _groq_client
    if _groq_client is None:
        from groq import Groq
        _groq_client = Groq(api_key=os.environ["GROQ_API_KEY"])
    return _groq_client

def call_llm(system, history, instruction, model=MODEL):
    client = get_groq()
    msgs = [{"role": "system", "content": system}]
    msgs.extend(history[-14:])
    msgs.append({"role": "user", "content": instruction})

    try:
        resp = client.chat.completions.create(
            model=model,
            messages=msgs,
            temperature=0.85,
            max_tokens=150,
        )
        text = resp.choices[0].message.content.strip()
        text = re.sub(r'^[\w]+\s*[:—\-]\s*', '', text)
        text = text.strip('"\'')
        return text
    except Exception as e:
        if model == MODEL:
            print(f"  ⚠ {MODEL} failed, trying backup: {str(e)[:80]}")
            return call_llm(system, history, instruction, model=BACKUP)
        raise


# ━━ DEBATE ENGINE (background thread) ━━━━━━━━━━━━━━━━━━━
def engine():
    # pick two random characters
    chars = random.sample(ALL_CHARS, 2)
    state["char_a"] = chars[0]
    state["char_b"] = chars[1]

    print(f"\n🎲 characters:")
    print(f"   {chars[0]['avatar']} {chars[0]['name']} ({chars[0]['role']})")
    print(f"   {chars[1]['avatar']} {chars[1]['name']} ({chars[1]['role']})")

    history = []             # shared openai-style history
    turn = 0
    msgs_on_topic = 0
    msgs_per_topic = random.randint(10, 16)
    used_topics = set()

    def pick_topic():
        avail = [t for t in ALL_TOPICS if t not in used_topics]
        if not avail:
            used_topics.clear()
            avail = list(ALL_TOPICS)
        t = random.choice(avail)
        used_topics.add(t)
        return t

    def now_str():
        return datetime.now(timezone.utc).strftime("%H:%M:%S UTC")

    def time_left():
        return max(0, int(MAX_UP - (time.time() - BOOT)))

    # ── first topic ──
    topic = pick_topic()
    state["topic"] = topic
    state["topic_num"] = 1

    print(f"\n📋 topic #1: \"{topic}\"")

    bus.emit("init", {
        "char_a": state["char_a"],
        "char_b": state["char_b"],
        "topic": topic,
        "topic_num": 1,
        "boot": BOOT,
        "max_up": MAX_UP,
    })

    time.sleep(5)

    # ── main loop ──
    while time_left() > 60:

        # ── topic rotation ──
        if msgs_on_topic >= msgs_per_topic:
            topic = pick_topic()
            state["topic"] = topic
            state["topic_num"] += 1
            msgs_on_topic = 0
            msgs_per_topic = random.randint(10, 16)
            history = history[-4:]

            topic_msg = {
                "type": "topic",
                "text": topic,
                "number": state["topic_num"],
                "timestamp": now_str(),
            }
            state["messages"].append(topic_msg)
            bus.emit("newtopic", topic_msg)
            print(f"\n📋 topic #{state['topic_num']}: \"{topic}\"")
            time.sleep(5)
            continue

        # ── determine speaker ──
        current = chars[turn % 2]
        other = chars[(turn + 1) % 2]

        # ── broadcast typing ──
        state["typing"] = current["name"]
        bus.emit("typing", {
            "speaker": current["name"],
            "avatar": current["avatar"],
            "color": current["color"],
            "role": current["role"],
            "time_left": time_left(),
        })

        # ── wait before generating (typing illusion) ──
        typing_wait = random.uniform(3, 6)
        time.sleep(typing_wait)

        # ── build prompt ──
        system = f"""You are {current['name']} — {current['role']}.
Personality: {current['personality']}.
Style: {current['style']}.
You are in a live debate about "{topic}" with {other['name']} ({other['role']}).
Keep responses under 80 words. Be sharp, direct, conversational.
Don't start with your name. Don't wrap in quotes.
Engage directly with what they said. Be natural."""

        if msgs_on_topic == 0:
            instruction = f'Topic: "{topic}"\nYou speak first. Share your opening thought. Under 80 words.'
        else:
            last_text = ""
            for m in reversed(state["messages"]):
                if m.get("type") != "topic" and m.get("speaker") == other["name"]:
                    last_text = m["text"]
                    break
            instruction = f'Topic: "{topic}"\n{other["name"]} just said: "{last_text}"\nRespond directly. Under 80 words.'

        # ── call LLM ──
        try:
            text = call_llm(system, history, instruction)
        except Exception as e:
            print(f"  ✖ LLM error: {str(e)[:100]}")
            time.sleep(10)
            continue

        # ── build message ──
        msg = {
            "type": "message",
            "speaker": current["name"],
            "avatar": current["avatar"],
            "color": current["color"],
            "role": current["role"],
            "text": text,
            "timestamp": now_str(),
            "msg_num": len([m for m in state["messages"] if m.get("type") != "topic"]) + 1,
            "time_left": time_left(),
            "topic": topic,
            "topic_num": state["topic_num"],
        }

        state["messages"].append(msg)
        state["typing"] = None

        # ── update openai-style history ──
        role_tag = "assistant" if turn % 2 == 0 else "user"
        history.append({"role": role_tag, "content": text})

        # ── emit words one by one ──
        words = text.split()
        chunks = []
        chunk = []
        for w in words:
            chunk.append(w)
            if len(chunk) >= 3:
                chunks.append(" ".join(chunk))
                chunk = []
        if chunk:
            chunks.append(" ".join(chunk))

        # first emit starts the bubble
        bus.emit("msgstart", {
            "speaker": current["name"],
            "avatar": current["avatar"],
            "color": current["color"],
            "role": current["role"],
            "timestamp": now_str(),
            "msg_num": msg["msg_num"],
            "time_left": time_left(),
        })

        # stream chunks
        for i, c in enumerate(chunks):
            bus.emit("msgchunk", {
                "speaker": current["name"],
                "chunk": c,
                "index": i,
                "total": len(chunks),
            })
            time.sleep(random.uniform(0.15, 0.4))

        # signal message complete
        bus.emit("msgdone", {
            "speaker": current["name"],
            "full_text": text,
            "msg_num": msg["msg_num"],
        })

        print(f"  {current['avatar']} {current['name']}: {text[:70]}...")

        turn += 1
        msgs_on_topic += 1

        # ── wait between messages ──
        remaining_gap = MSG_GAP - typing_wait
        if remaining_gap > 0 and time_left() > 90:
            bus.emit("waiting", {
                "next_speaker": chars[turn % 2]["name"],
                "next_avatar": chars[turn % 2]["avatar"],
                "next_color": chars[turn % 2]["color"],
                "gap": int(remaining_gap),
                "time_left": time_left(),
            })
            time.sleep(remaining_gap)

    # ── server dying ──
    bus.emit("shutdown", {
        "message": "Server shutting down. Next cycle starts on schedule.",
        "total_messages": len([m for m in state["messages"] if m.get("type") != "topic"]),
        "total_topics": state["topic_num"],
    })
    print("\n⏰ Time's up. Shutting down.")


# ━━ FLASK APP ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
app = Flask(__name__)

@app.route("/stream")
def stream():
    q = bus.listen()

    def gen():
        # send current state on connect
        init = {
            "char_a": state["char_a"],
            "char_b": state["char_b"],
            "topic": state["topic"],
            "topic_num": state["topic_num"],
            "messages": state["messages"][-50:],
            "typing": state["typing"],
            "boot": BOOT,
            "max_up": MAX_UP,
            "time_left": max(0, int(MAX_UP - (time.time() - BOOT))),
        }
        yield f"event: fullstate\ndata: {json.dumps(init)}\n\n"

        try:
            while True:
                try:
                    msg = q.get(timeout=30)
                    yield msg
                except queue.Empty:
                    yield f"event: ping\ndata: {json.dumps({'time_left': max(0, int(MAX_UP - (time.time() - BOOT)))})}\n\n"
        except GeneratorExit:
            bus.unlisten(q)

    return Response(gen(), content_type="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.route("/")
def index():
    return PAGE_HTML


# ━━ HTML PAGE ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
PAGE_HTML = r"""<!DOCTYPE html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>MindDuel — Live AI Debate</title>
<style>
:root{--bg:#0a0a0a;--sf:#111;--bd:#1a1a1a;--tx:#b0b0b0;--dm:#555;--gn:#0f0}
*{margin:0;padding:0;box-sizing:border-box}
body{
  font-family:'SF Mono','Fira Code',Consolas,monospace;
  background:var(--bg);color:var(--tx);min-height:100vh;
  display:flex;flex-direction:column;
}

/* header */
.hdr{
  text-align:center;padding:1.5rem 1rem .8rem;
  border-bottom:1px solid var(--bd);
}
.hdr h1{font-size:1.3rem;color:#fff;letter-spacing:.04em}
.hdr .sub{color:var(--dm);font-size:.7rem;margin-top:.3rem}

/* status bar */
.bar{
  display:flex;justify-content:center;gap:1.5rem;
  padding:.6rem 1rem;border-bottom:1px solid var(--bd);
  font-size:.7rem;color:var(--dm);flex-wrap:wrap;
}
.bar .item{display:flex;align-items:center;gap:.3rem}
.dot{width:7px;height:7px;border-radius:50%;animation:pulse 1.5s ease-in-out infinite}
@keyframes pulse{50%{opacity:.2}}
.alive .dot{background:var(--gn)}
.dead .dot{background:#f44}

/* topic banner */
.topic-bar{
  text-align:center;padding:.8rem 1rem;
  background:#0f01;border-bottom:1px solid #0f03;
}
.topic-bar .label{font-size:.6rem;color:var(--gn);letter-spacing:.1em;text-transform:uppercase}
.topic-bar .text{color:#fff;font-size:1rem;font-weight:bold;margin-top:.2rem}

/* versus */
.vs{
  display:flex;justify-content:center;align-items:center;gap:1.5rem;
  padding:1rem;border-bottom:1px solid var(--bd);
}
.fighter{text-align:center;min-width:100px}
.fighter .ava{font-size:2rem}
.fighter .nm{font-weight:bold;font-size:.85rem;margin-top:.2rem}
.fighter .rl{font-size:.65rem;margin-top:.1rem}
.vs-x{color:var(--dm);font-size:1.2rem;font-weight:bold}

/* chat */
.chat{
  flex:1;overflow-y:auto;padding:1rem;
  max-width:750px;width:100%;margin:0 auto;
}

/* system message */
.sys{
  text-align:center;margin:1.5rem 0;
  padding:.5rem;
}
.sys .pill{
  display:inline-block;
  background:#111;border:1px solid var(--bd);
  border-radius:20px;padding:.3rem 1rem;
  font-size:.7rem;color:var(--dm);
}
.sys .topic-change{
  display:block;color:#fff;font-weight:bold;
  font-size:.9rem;margin-top:.3rem;
}

/* bubble */
.bubble{margin-bottom:1rem;display:flex;flex-direction:column;animation:fadeIn .3s ease}
@keyframes fadeIn{from{opacity:0;transform:translateY(8px)}to{opacity:1;transform:none}}
.bubble.left{align-items:flex-start}
.bubble.right{align-items:flex-end}
.bubble .head{
  display:flex;align-items:center;gap:.4rem;
  font-size:.7rem;font-weight:bold;margin-bottom:.25rem;padding:0 .3rem;
}
.bubble .time{font-weight:normal;color:var(--dm);font-size:.6rem;margin-left:.3rem}
.bubble .body{
  max-width:80%;padding:.8rem 1rem;border-radius:14px;
  font-size:.85rem;line-height:1.55;
  background:var(--sf);border:1px solid var(--bd);
  min-height:1.4em;
}
.bubble.left .body{border-radius:4px 14px 14px 14px}
.bubble.right .body{border-radius:14px 4px 14px 14px}

/* typing indicator */
.typing-indicator{
  display:inline-flex;gap:4px;align-items:center;padding:4px 0;
}
.typing-indicator span{
  width:6px;height:6px;border-radius:50%;background:#555;
  animation:blink 1.4s ease-in-out infinite;
}
.typing-indicator span:nth-child(2){animation-delay:.2s}
.typing-indicator span:nth-child(3){animation-delay:.4s}
@keyframes blink{0%,80%,100%{opacity:.3}40%{opacity:1}}

/* cursor */
.cursor{
  display:inline-block;width:2px;height:1em;
  background:currentColor;margin-left:2px;
  animation:cursorBlink .8s step-end infinite;
  vertical-align:text-bottom;
}
@keyframes cursorBlink{0%,100%{opacity:1}50%{opacity:0}}

/* countdown bar */
.countdown{
  text-align:center;padding:.8rem;
  border-top:1px solid var(--bd);
  font-size:.7rem;color:var(--dm);
}
.countdown .next{color:var(--tx)}
.countdown .death{color:#f44;margin-left:1rem}

.shutdown-msg{
  text-align:center;padding:2rem;color:#f44;font-size:.9rem;
  border-top:1px solid #f44;margin-top:1rem;
}

@media(max-width:500px){
  .bubble .body{max-width:92%;font-size:.8rem}
  .hdr h1{font-size:1rem}
  .vs{gap:.8rem}
  .fighter .ava{font-size:1.5rem}
}
</style>
</head><body>

<div class="hdr">
  <h1>⚔️ MindDuel</h1>
  <div class="sub">live ai debate — continuous until shutdown</div>
</div>

<div class="bar" id="statusbar">
  <div class="item alive"><div class="dot"></div><span id="status-text">connecting...</span></div>
  <div class="item">⏱ server: <span id="uptime">--</span></div>
  <div class="item">💀 dies in: <span id="timeleft" style="color:#f44">--</span></div>
  <div class="item">💬 <span id="msgcount">0</span> messages</div>
</div>

<div class="topic-bar" id="topicbar">
  <div class="label">topic #<span id="topicnum">0</span></div>
  <div class="text" id="topictext">loading...</div>
</div>

<div class="vs" id="vsbar" style="display:none">
  <div class="fighter" id="fa">
    <div class="ava" id="fa-ava"></div>
    <div class="nm" id="fa-name"></div>
    <div class="rl" id="fa-role"></div>
  </div>
  <div class="vs-x">vs</div>
  <div class="fighter" id="fb">
    <div class="ava" id="fb-ava"></div>
    <div class="nm" id="fb-name"></div>
    <div class="rl" id="fb-role"></div>
  </div>
</div>

<div class="chat" id="chat"></div>

<div class="countdown" id="countdown">
  <span class="next" id="next-label">waiting...</span>
</div>

<script>
const $=s=>document.getElementById(s);
const chat=$('chat');
let msgCount=0;
let bootTime=0;
let maxUp=0;
let charA=null,charB=null;
let typingBubble=null;
let currentBubbleBody=null;
let timerInterval=null;

function scrollBottom(){
  chat.scrollTop=chat.scrollHeight;
}

function formatTime(s){
  s=Math.max(0,Math.floor(s));
  const h=Math.floor(s/3600),m=Math.floor(s%3600/60),sec=s%60;
  if(h>0)return h+'h '+m+'m '+sec+'s';
  if(m>0)return m+'m '+sec+'s';
  return sec+'s';
}

function startTimers(){
  if(timerInterval)clearInterval(timerInterval);
  timerInterval=setInterval(()=>{
    const now=Date.now()/1000;
    const up=now-bootTime;
    const left=Math.max(0,maxUp-up);
    $('uptime').textContent=formatTime(up);
    $('timeleft').textContent=formatTime(left);
    if(left<300){
      $('timeleft').style.color='#f44';
      $('timeleft').style.fontWeight='bold';
    }
    if(left<=0){
      $('status-text').textContent='OFFLINE';
      document.querySelector('.dot').style.background='#f44';
      clearInterval(timerInterval);
    }
  },1000);
}

function setChars(a,b){
  charA=a;charB=b;
  $('vsbar').style.display='flex';
  $('fa-ava').textContent=a.avatar;
  $('fa-name').textContent=a.name;
  $('fa-name').style.color=a.color;
  $('fa-role').textContent=a.role;
  $('fa-role').style.color=a.color;
  $('fb-ava').textContent=b.avatar;
  $('fb-name').textContent=b.name;
  $('fb-name').style.color=b.color;
  $('fb-role').textContent=b.role;
  $('fb-role').style.color=b.color;
}

function setTopic(text,num){
  $('topictext').textContent='"'+text+'"';
  $('topicnum').textContent=num;
}

function sideFor(name){
  if(!charA)return'left';
  return name===charA.name?'left':'right';
}

function colorFor(name){
  if(charA&&name===charA.name)return charA.color;
  if(charB&&name===charB.name)return charB.color;
  return'#aaa';
}

function addSystemMsg(html){
  const d=document.createElement('div');
  d.className='sys';
  d.innerHTML=html;
  chat.appendChild(d);
  scrollBottom();
}

function removeTyping(){
  if(typingBubble){
    typingBubble.remove();
    typingBubble=null;
    currentBubbleBody=null;
  }
}

function addTypingBubble(speaker,avatar,color,role){
  removeTyping();
  const side=sideFor(speaker);
  const d=document.createElement('div');
  d.className='bubble '+side;
  d.innerHTML=`
    <div class="head" style="color:${color}">
      ${avatar} ${speaker} · ${role}
    </div>
    <div class="body" style="border-${side==='left'?'left':'right'}:3px solid ${color}">
      <div class="typing-indicator"><span></span><span></span><span></span></div>
    </div>`;
  chat.appendChild(d);
  typingBubble=d;
  scrollBottom();
}

function startMessageBubble(speaker,avatar,color,role,timestamp){
  removeTyping();
  const side=sideFor(speaker);
  const d=document.createElement('div');
  d.className='bubble '+side;
  d.innerHTML=`
    <div class="head" style="color:${color}">
      ${avatar} ${speaker} · ${role}
      <span class="time">${timestamp}</span>
    </div>
    <div class="body" style="border-${side==='left'?'left':'right'}:3px solid ${color}">
      <span class="words"></span><span class="cursor"></span>
    </div>`;
  chat.appendChild(d);
  currentBubbleBody=d.querySelector('.words');
  scrollBottom();
  return d;
}

function appendChunk(text){
  if(!currentBubbleBody)return;
  const prev=currentBubbleBody.textContent;
  currentBubbleBody.textContent=prev?(prev+' '+text):text;
  scrollBottom();
}

function finishMessage(el){
  if(!el)return;
  const cursor=el.querySelector('.cursor');
  if(cursor)cursor.remove();
  currentBubbleBody=null;
  msgCount++;
  $('msgcount').textContent=msgCount;
}

function addFullMessage(m){
  const side=sideFor(m.speaker);
  const color=m.color||colorFor(m.speaker);
  const d=document.createElement('div');
  d.className='bubble '+side;
  d.innerHTML=`
    <div class="head" style="color:${color}">
      ${m.avatar||''} ${m.speaker} · ${m.role||''}
      <span class="time">${m.timestamp||''}</span>
    </div>
    <div class="body" style="border-${side==='left'?'left':'right'}:3px solid ${color}">
      ${m.text}
    </div>`;
  chat.appendChild(d);
  msgCount++;
  $('msgcount').textContent=msgCount;
}

function addTopicChange(t){
  addSystemMsg(`
    <div class="pill">topic #${t.number}</div>
    <div class="topic-change">"${t.text}"</div>
  `);
  setTopic(t.text,t.number);
}

// ── SSE ──────────────────────────────────────────────
let currentMsgEl=null;

function connect(){
  $('status-text').textContent='connecting...';
  const es=new EventSource('/stream');

  es.addEventListener('fullstate',e=>{
    const d=JSON.parse(e.data);
    bootTime=d.boot;
    maxUp=d.max_up;
    if(d.char_a&&d.char_b)setChars(d.char_a,d.char_b);
    if(d.topic)setTopic(d.topic,d.topic_num);
    // render history
    chat.innerHTML='';
    msgCount=0;
    if(d.messages){
      d.messages.forEach(m=>{
        if(m.type==='topic'){
          addTopicChange(m);
        } else if(m.type==='message'){
          addFullMessage(m);
        }
      });
    }
    $('status-text').textContent='LIVE';
    startTimers();
    scrollBottom();
  });

  es.addEventListener('newtopic',e=>{
    const d=JSON.parse(e.data);
    addTopicChange(d);
  });

  es.addEventListener('typing',e=>{
    const d=JSON.parse(e.data);
    addTypingBubble(d.speaker,d.avatar,d.color,d.role);
    $('next-label').textContent=d.speaker+' is thinking...';
  });

  es.addEventListener('msgstart',e=>{
    const d=JSON.parse(e.data);
    currentMsgEl=startMessageBubble(d.speaker,d.avatar,d.color,d.role,d.timestamp);
    $('next-label').textContent=d.speaker+' is speaking...';
  });

  es.addEventListener('msgchunk',e=>{
    const d=JSON.parse(e.data);
    appendChunk(d.chunk);
  });

  es.addEventListener('msgdone',e=>{
    finishMessage(currentMsgEl);
    currentMsgEl=null;
    $('next-label').textContent='waiting for next turn...';
  });

  es.addEventListener('waiting',e=>{
    const d=JSON.parse(e.data);
    let gap=d.gap;
    $('next-label').innerHTML=
      `${d.next_avatar} <span style="color:${d.next_color}">${d.next_speaker}</span> responds in <span id="gap-sec">${gap}s</span>`;
    const gi=setInterval(()=>{
      gap--;
      const el=document.getElementById('gap-sec');
      if(el)el.textContent=gap+'s';
      if(gap<=0)clearInterval(gi);
    },1000);
  });

  es.addEventListener('shutdown',e=>{
    const d=JSON.parse(e.data);
    removeTyping();
    const div=document.createElement('div');
    div.className='shutdown-msg';
    div.innerHTML=`⚠️ ${d.message}<br>
      <span style="font-size:.75rem;color:var(--dm)">
        ${d.total_messages} messages · ${d.total_topics} topics debated
      </span>`;
    chat.appendChild(div);
    scrollBottom();
    $('status-text').textContent='OFFLINE';
    document.querySelector('.dot').style.background='#f44';
    $('next-label').textContent='server offline — next cycle on schedule';
  });

  es.addEventListener('ping',e=>{
    const d=JSON.parse(e.data);
    // keep alive
  });

  es.onerror=()=>{
    $('status-text').textContent='reconnecting...';
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
    print("⚔️  MindDuel — Live AI Debate Server")
    print(f"   model   : {MODEL}")
    print(f"   gap     : {MSG_GAP}s between messages")
    print(f"   max up  : {MAX_UP//3600}h {MAX_UP%3600//60}m")
    print("=" * 50)

    t = threading.Thread(target=engine, daemon=True)
    t.start()

    app.run(host="0.0.0.0", port=PORT, threaded=True)
