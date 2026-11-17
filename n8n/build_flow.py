#!/usr/bin/env python3
"""Builds n8n/stella-voice.json, the workflow the Copilot key talks to.

    POST /webhook/stella/voice  {text, pending}  ->  {say, pending, action}

Voice in -> Settings -> Understand (rules) -> matched? -- no --> LLM router (Ollama) -> Parse
                                                 \\ yes                                   /
                                                  +------------> Intent <----------------+
Intent -> weather | clock | desktop | chat | help -> Reply

Edit the Settings node in n8n after importing (Ollama URL, model, your town), or change SETTINGS below and rebuild.
"""
import hashlib
import json
import os
import uuid

HERE = os.path.dirname(os.path.abspath(__file__))
FID = "stellaVoicePub"
SETTINGS = {
    "ollama": "http://host.docker.internal:11434",
    "model": "qwen3:8b",
    "place": "London",
    "lat": "51.5072",
    "lon": "-0.1276",
    "timezone": "Europe/London",
}
INTENTS = ["weather", "clock", "desktop", "chat", "help"]

nodes, conn = [], {}


def nid(name):
    return str(uuid.UUID(hashlib.md5(f"{FID}/{name}".encode()).hexdigest()))


def add(name, typ, ver, params, x, y, **extra):
    nodes.append({"parameters": params, "id": nid(name), "name": name, "type": typ, "typeVersion": ver,
                  "position": [x, y], **extra})
    return name


def link(a, b, out=0):
    m = conn.setdefault(a, {"main": []})["main"]
    while len(m) <= out:
        m.append([])
    m[out].append({"node": b, "type": "main", "index": 0})


def code(name, js, x, y):
    return add(name, "n8n-nodes-base.code", 2, {"jsCode": js.strip()}, x, y)


def http(name, method, url, x, y, body=None, query=None, timeout=60000):
    p = {"method": method, "url": url, "options": {"timeout": timeout}}
    if body is not None:
        p.update(sendBody=True, specifyBody="json", jsonBody=body)
    if query:
        p.update(sendQuery=True, specifyQuery="keypair",
                 queryParameters={"parameters": [{"name": k, "value": v} for k, v in query.items()]})
    return add(name, "n8n-nodes-base.httpRequest", 4.2, p, x, y, onError="continueRegularOutput")


def cond(left, op, typ="string"):
    return {"options": {"caseSensitive": True, "leftValue": "", "typeValidation": "loose", "version": 2},
            "conditions": [{"id": nid(f"c/{left}/{op}"), "leftValue": left, "rightValue": "",
                            "operator": {"type": typ, "operation": op, "singleValue": True}}],
            "combinator": "and"}


def note(name, text, x, y, w, h, color=7):
    add(name, "n8n-nodes-base.stickyNote", 1, {"content": text, "width": w, "height": h, "color": color}, x, y)


S = "$('Settings').first().json"

# ------------------------------------------------------------------ understand
add("Voice in", "n8n-nodes-base.webhook", 2,
    {"httpMethod": "POST", "path": "stella/voice", "responseMode": "responseNode", "options": {}},
    0, 300, webhookId=nid("Voice in/hook"))
add("Settings", "n8n-nodes-base.set", 3.4, {"assignments": {"assignments": [
    {"id": nid("set/" + k), "name": k, "value": v, "type": "string"} for k, v in SETTINGS.items()]}, "options": {}},
    220, 300)

code("Understand", r"""
// Clean the words, then try cheap rules first. Anything a rule can't place goes to the LLM.
const b = $('Voice in').first().json.body || {};
const raw = String(b.text || '').trim();
const prev = b.pending || null;          // what Stella asked last turn, if anything
let t = raw.toLowerCase().replace(/[‘’]/g, "'").replace(/[^a-z0-9\s'.:%/-]/g, ' ').replace(/\s+/g, ' ').replace(/[.\s]+$/, '').trim();
t = t.replace(/^(hey |ok |okay |so |right |um |uh )+/, '').replace(/^stella[,]?\s+/, '')
     .replace(/^(please|can you|could you|would you)\s+/, '').replace(/\s+(please|thanks|thank you)$/, '').trim();
const out = (intent, slots = {}, rule = '') => [{ json: { raw, t, prev, intent, slots, rule } }];
let m;
if (!t) return out('help', { empty: true }, 'empty');
if (prev && prev.intent === 'desktop' && prev.ask === 'target') return out('desktop', { action: prev.action, target: t }, 'followup');
if (/^(help|what can you do|what can i (say|ask))\b/.test(t)) return out('help', {}, 'help');
if (/^(never ?mind|cancel|forget it|nothing)$/.test(t)) return out('help', { cancel: true }, 'cancel');
if (/^(what'?s the time|what time is it|time( now)?|tell me the time)$/.test(t)) return out('clock', { what: 'time' }, 'clock.time');
if (/^(what'?s the date|what day is (it|today)|today'?s date|what date is it)$/.test(t)) return out('clock', { what: 'date' }, 'clock.date');
if (/\b(weather|forecast|raining|umbrella|temperature)\b|\bis it (going to be )?(cold|warm|hot|raining)\b|\bwill it rain\b|\bneed (a|my) (jacket|coat)\b/.test(t) && !/\bin (january|july|summer|winter)\b/.test(t))
  return out('weather', { when: /tomorrow/.test(t) ? 'tomorrow' : 'today' }, 'weather');
if (/^(lock|lock (the |my )?(screen|laptop|computer))$/.test(t)) return out('desktop', { action: 'lock' }, 'desktop.lock');
if ((m = t.match(/^(?:close|quit|exit)\s*(?:the |my )?(.*?)(?: app| window)?$/))) return out('desktop', { action: 'close', target: m[1] }, 'desktop.close');
if ((m = t.match(/^(?:open|launch|start|bring up|pull up)\s*(?:the |my |up )?(.*?)(?: app)?$/))) return out('desktop', { action: 'open', target: m[1] }, 'desktop.open');
return out('', {}, '');
""", 440, 300)
add("Matched by a rule?", "n8n-nodes-base.if", 2.2, {"conditions": cond("={{ $json.intent }}", "notEmpty"), "options": {}},
    660, 300)

# ------------------------------------------------------------------ LLM route
code("Build router prompt", r"""
// One call does both jobs: pick the intent and, for chat, write the answer.
const j = $input.first().json, cfg = """ + S + r""";
const system = `You are Stella, a voice assistant on a Linux laptop. Reply ONLY with JSON:
{"intent": one of ${JSON.stringify(""" + json.dumps(INTENTS) + r""")}, "slots": {...}}
weather: {"when": "today|tomorrow"}
clock: {"what": "time|date"}
desktop: {"action": "open|close|lock", "target": "app or website"}
help: {}
chat: {"answer": "your spoken reply: calm, dry and quick, one short sentence, two at most, British English, no emojis, no lists"}
Questions you can answer yourself are chat.`;
const shots = [
  ['fire up firefox for me', { intent: 'desktop', slots: { action: 'open', target: 'firefox' } }],
  ['is it going to be nippy later', { intent: 'weather', slots: { when: 'today' } }],
  ['how many grams in a pound', { intent: 'chat', slots: { answer: 'About 454 grams.' } }],
];
const messages = [{ role: 'system', content: system }];
for (const [u, a] of shots) messages.push({ role: 'user', content: u }, { role: 'assistant', content: JSON.stringify(a) });
messages.push({ role: 'user', content: j.raw });
return [{ json: { ...j, llm_body: { model: cfg.model, stream: false, think: false, format: 'json', keep_alive: '30m',
  options: { temperature: 0.1, num_predict: 200 }, messages } } }];
""", 880, 460)
http("LLM router (Ollama)", "POST", "={{ " + S + ".ollama }}/api/chat", 1100, 460,
     body="={{ JSON.stringify($json.llm_body) }}", timeout=150000)
code("Parse LLM reply", r"""
const INTENTS = """ + json.dumps(INTENTS) + r""";
const j = $('Build router prompt').first().json, res = $input.first().json;
let r = {};
try { r = JSON.parse(String(res.message?.content || '{}').replace(/^[^{]*/, '').replace(/[^}]*$/, '')); } catch (e) {}
if (!INTENTS.includes(r.intent))
  r = res.error ? { intent: 'chat', slots: { answer: "I can't reach the language model right now." } }
                : { intent: 'chat', slots: { answer: "Sorry, I didn't get that." } };
const { llm_body, ...rest } = j;
return [{ json: { ...rest, intent: r.intent, slots: r.slots || {}, rule: 'llm' } }];
""", 1320, 460)

# ------------------------------------------------------------------ act
rules = []
for v in INTENTS:
    c = cond("={{ $json.intent }}", "equals")
    c["conditions"][0].update(rightValue=v, id=nid(f"sw/{v}"))
    del c["conditions"][0]["operator"]["singleValue"]
    rules.append({"conditions": c, "renameOutput": True, "outputKey": v})
add("Intent", "n8n-nodes-base.switch", 3.2, {"rules": {"values": rules}, "options": {"fallbackOutput": "extra"}},
    1540, 300)

http("Open-Meteo", "GET", "https://api.open-meteo.com/v1/forecast", 1780, 60, query={
    "latitude": "={{ " + S + ".lat }}", "longitude": "={{ " + S + ".lon }}", "timezone": "={{ " + S + ".timezone }}",
    "forecast_days": "2", "current": "temperature_2m,apparent_temperature,weather_code",
    "daily": "temperature_2m_max,temperature_2m_min,precipitation_probability_max,weather_code"}, timeout=15000)
code("Weather reply", r"""
const j = $('Intent').first().json, w = $input.first().json, cfg = """ + S + r""";
if (w.error || !w.daily) return [{ json: { ...j, say: "I can't get the weather right now." } }];
const W = c => c === 0 ? 'clear' : c <= 2 ? 'partly cloudy' : c === 3 ? 'overcast' : c <= 48 ? 'foggy' : c <= 57 ? 'drizzly'
  : c <= 67 ? 'rainy' : c <= 77 ? 'snowy' : c <= 82 ? 'showery' : c <= 86 ? 'snow showers' : 'stormy';
const r = Math.round, d = w.daily, i = j.slots.when === 'tomorrow' ? 1 : 0;
const rain = d.precipitation_probability_max[i];
const say = i === 0
  ? `${cfg.place}: ${r(w.current.temperature_2m)} degrees and ${W(w.current.weather_code)}, high of ${r(d.temperature_2m_max[0])}.` + (rain >= 40 ? ` ${rain} percent chance of rain, take a jacket.` : '')
  : `Tomorrow in ${cfg.place}: ${W(d.weather_code[1])}, ${r(d.temperature_2m_min[1])} to ${r(d.temperature_2m_max[1])} degrees, ${rain} percent chance of rain.`;
return [{ json: { ...j, say } }];
""", 2000, 60)

code("Clock reply", r"""
const j = $input.first().json, tz = """ + S + r""".timezone;
const now = new Date();
const say = j.slots.what === 'date'
  ? 'It is ' + now.toLocaleDateString('en-GB', { weekday: 'long', day: 'numeric', month: 'long', timeZone: tz }) + '.'
  : 'It is ' + now.toLocaleTimeString('en-GB', { hour: '2-digit', minute: '2-digit', timeZone: tz }) + '.';
return [{ json: { ...j, say } }];
""", 1780, 220)

code("Desktop action", r"""
// The laptop does the work: the extension reads `action` and opens, closes or locks.
const j = $input.first().json, s = j.slots || {};
const action = ['open', 'close', 'lock'].includes(s.action) ? s.action : 'open';
const target = String(s.target || '').replace(/^(the|my)\s+/, '').trim();
if (action !== 'lock' && !target)
  return [{ json: { ...j, say: action === 'open' ? 'Open what?' : 'Close what?', pending: { intent: 'desktop', action, ask: 'target' } } }];
const say = action === 'lock' ? 'Locking.' : (action === 'open' ? 'Opening ' : 'Closing ') + target + '.';
return [{ json: { ...j, say, action: action === 'lock' ? { type: 'lock' } : { type: action, target } } }];
""", 1780, 380)

code("Chat reply", r"""
const j = $input.first().json;
const say = String((j.slots || {}).answer || '').replace(/\*+/g, '').replace(/\s+/g, ' ').trim() || "I'm not sure.";
return [{ json: { ...j, say } }];
""", 1780, 540)

code("Help reply", r"""
const j = $input.first().json, s = j.slots || {};
const say = s.cancel ? 'Okay.' : s.empty ? '' : 'Ask me anything, or say the weather, the time, open or close an app, or lock the screen.';
return [{ json: { ...j, say } }];
""", 1780, 700)

add("Reply", "n8n-nodes-base.respondToWebhook", 1.1, {"respondWith": "json", "responseBody":
    "={{ JSON.stringify({ say: $json.say || '', pending: $json.pending || null, action: $json.action || null, intent: $json.intent, rule: $json.rule }) }}",
    "options": {}}, 2240, 380)

# ------------------------------------------------------------------ wiring
for a, b in [("Voice in", "Settings"), ("Settings", "Understand"), ("Understand", "Matched by a rule?"),
             ("Build router prompt", "LLM router (Ollama)"), ("LLM router (Ollama)", "Parse LLM reply"),
             ("Parse LLM reply", "Intent"), ("Open-Meteo", "Weather reply")]:
    link(a, b)
link("Matched by a rule?", "Intent", 0)
link("Matched by a rule?", "Build router prompt", 1)
for i, n in enumerate(["Open-Meteo", "Clock reply", "Desktop action", "Chat reply", "Help reply", "Chat reply"]):
    link("Intent", n, i)
for n in ["Weather reply", "Clock reply", "Desktop action", "Chat reply", "Help reply"]:
    link(n, "Reply")

note("Note: understand", "## 1. Understand\nRules catch the common asks for free. Anything else goes to a local model "
     "through Ollama, which picks the intent and, for small talk, writes the answer.", -40, 120, 1520, 520, 7)
note("Note: act", "## 2. Act and reply\nThe laptop does desktop actions itself: the reply carries `action` and the "
     "GNOME extension opens, closes or locks.", 1740, -120, 680, 1000, 7)

wf = {"id": FID, "name": "Stella Voice", "nodes": nodes, "connections": conn, "active": False,
      "settings": {"executionOrder": "v1"}, "pinData": {}}
with open(os.path.join(HERE, "stella-voice.json"), "w") as fh:
    json.dump(wf, fh, indent=1)
print(f"wrote stella-voice.json ({len(nodes)} nodes)")
