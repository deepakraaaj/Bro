"""Offline assistant brain (bite-sized: Needle ~120 MB + optional gemma3:270m).

Routing, fastest first:
  1. Instant path: the command clearly matches one action (trigger words) -> run it directly, no model.
  2. Needle (26M): ambiguous commands, or open/search when the name can't be parsed.
  3. Tiny LLM (gemma3:270m via Ollama): questions and chat. Set NEEDLE_LLM="" to disable.

Every system change goes through sh() or launch(), so tests can fake them all.
Run typed mode:  needle assistant.py
"""
import ast
import builtins
import datetime
import difflib
import glob
import json
import operator
import os
import re
import shlex
import shutil
import signal
import subprocess
import tempfile
import threading
import time
import urllib.parse
import urllib.request

import cactus

import control

# Local brain for questions and chat. Benchmarked on this laptop (2026-11-11): 26/30 everyday questions right
# vs 11/30 for gemma3:270m; ~3 s per answer, ~3 GB RAM while loaded (unloads after 5 idle minutes).
LLM_MODEL = os.environ.get("NEEDLE_LLM", "qwen3:4b-instruct-2507-q4_K_M")  # set NEEDLE_LLM="" to disable
OLLAMA = "http://127.0.0.1:11434/api/chat"
VIDEO_LOG = os.path.expanduser("~/Documents/needle-youtube-log.md")
BACKLIGHT = "/sys/class/backlight/intel_backlight"
CONFIRM_SECONDS = 15        # a "yes" later than this won't trigger a pending shutdown
MIN_WIFI_GAIN = 10          # don't drop the connection to gain less than this much signal

# Env vars leaked by the VS Code snap terminal that crash GTK apps (e.g. gnome-calculator).
SNAP_LEAKS = ("GTK_PATH", "GTK_EXE_PREFIX", "GTK_IM_MODULE_FILE", "GIO_MODULE_DIR", "LOCPATH",
              "GDK_PIXBUF_MODULE_FILE", "GDK_PIXBUF_MODULEDIR", "GSETTINGS_SCHEMA_DIR")

# Spoken shortcuts for sites whose address isn't simply <name>.com. Any other "open <name>" that isn't an
# installed app opens https://<name>.com.
SITES = {"youtube": "youtube.com", "gmail": "mail.google.com", "google": "google.com",
         "github": "github.com", "chatgpt": "chatgpt.com", "whatsapp": "web.whatsapp.com",
         "linkedin": "linkedin.com", "reddit": "reddit.com"}

TRIGGERS = {
    "open": ("open", "launch", "start", "go to", "show me"),
    # Google is only opened by an explicit "search Google ..." request. "play" remains a YouTube action.
    "search": ("search", "google search", "google", "look up", "play", "change song", "change the song"),
    "volume": ("volume", "mute", "unmute", "louder", "quieter", "sound"),
    "timer": ("timer", "remind", "alarm"),
    "lock": ("lock",),
    "info": ("time", "date", "day", "battery", "charge"),
    "math": ("calculate", "plus", "minus", "times", "divided", "multiplied", "+", "*", "/", "percent of"),
    "bluetooth": ("bluetooth", "headphone", "headphones", "headset", "earphone", "earphones", "earbuds",
                  "buds", "neckband", "speaker", "connect", "disconnect", "pair"),
    "wifi": ("wifi", "wi-fi", "wi fi", "internet", "network"),
    "brightness": ("brightness", "brighter", "dimmer", "dim"),
    "media": ("pause", "resume", "next song", "next track", "next video", "previous song", "previous track",
              "skip", "play music", "stop music", "forward", "fast forward", "rewind", "backward", "backwards",
              "go back", "seek", "stop the music", "stop the video", "stop the song"),
    "dnd": ("do not disturb", "notifications"),
    "power": ("shut down", "shutdown", "restart", "reboot", "sleep", "suspend", "power off"),   # see power_request
    "video_log": ("log",),
    "close": ("close", "quit", "kill"),
}

# query: raw command text. done: Needle sometimes emits several calls; only the first runs.
# pending: (handler, expires_at) for a follow-up answer like "yes" or a device name.
state = {"query": "", "done": False, "pending": None, "detail": "", "spoken": ""}


# ---------- helpers ----------

def clean_env():
    env = {k: v for k, v in os.environ.items() if k not in SNAP_LEAKS and not k.startswith("SNAP")}
    for k in ("XDG_DATA_DIRS", "XDG_CONFIG_DIRS"):  # restore pre-snap values if VS Code saved them
        if f"{k}_VSCODE_SNAP_ORIG" in env:
            env[k] = env.pop(f"{k}_VSCODE_SNAP_ORIG")
    return env


def launch(cmd):
    """Start a program in the background (apps, browser, notifications). It gets its own systemd scope,
    like apps started from the dock, so it keeps running when the assistant stops or restarts."""
    scope = ["systemd-run", "--user", "--scope", "--slice=app.slice", "--quiet", "--collect", "--"]
    subprocess.Popen(scope + list(cmd), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     start_new_session=True, env=clean_env())


def sh(*cmd, timeout=20):
    """Run a command and return stdout+stderr ("" on timeout or missing program)."""
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.stdout + r.stderr
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return ""


def read_file(path):
    with builtins.open(path) as f:  # the tool below is named `open`, so use the builtin explicitly
        return f.read()


def said(word, text):
    """Whole-word match (so 'time' doesn't match 'timer'); symbols like '+' match anywhere."""
    return bool(re.search(rf"\b{re.escape(word)}\b", text)) if word[0].isalnum() else word in text


POLITE = r"^(?:(?:please|can you|could you|will you|would you|i want to|i want you to|just|now|okay|ok)\s+)+"
# Words a built-in command may start with, besides the trigger words themselves.
LEAD_INS = set("turn set increase decrease raise lower reduce make what what's whats how tell is remind go "
               "switch shut put enable disable stop the my a max maximum full join".split())
QUESTION = r"(?:is|are|was|what|what's|whats|which|who|when|where|does|did|should)\b"
# Words that may surround "time", "battery", ... in a real request for them ("what's the time right now").
INFO_FILLER = set("what what's whats s is it the my current right now today today's tell me show give how much "
                  "left level percent percentage please check".split())
# Seek and skip words only control playback when nothing but these, numbers and units is said with them.
SEEK_WORDS = ("skip", "forward", "fast forward", "rewind", "backward", "backwards", "go back", "seek")
SEEK_FILLER = set("go back forward fast rewind backward backwards seek skip ahead by the a an and half it this "
                  "video song track please little bit".split())
COMPUTER = {"computer", "laptop", "system", "pc", "machine"}


def command_shaped(t):
    """True when the sentence starts like a built-in command ("open ...", "volume up", "what time ..."), so
    "rename the photos by date" goes to the agent and isn't answered with today's date."""
    first = (re.findall(r"[a-z0-9'+*/-]+", re.sub(POLITE, "", t.strip())) or [""])[0]
    return first in LEAD_INS or first in NUMBER_WORDS or first[:1].isdigit() or \
        any(first == w.split()[0] for ws in TRIGGERS.values() for w in ws)


def power_request(t):
    """'shut down', 'restart the laptop', 'go to sleep': about the computer itself, unlike 'restart chrome'."""
    m = re.fullmatch(r"(shut ?down|power off|turn off|switch off|restart|reboot|sleep|suspend|go to sleep|put)\b"
                     r"(.*?)[.!?]*", re.sub(POLITE, "", t.strip().lower()))
    if not m:
        return False
    rest = set(words(m.group(2)))
    if not rest <= COMPUTER | {"it", "now", "the", "my", "this", "please", "to", "sleep"}:
        return False
    return m.group(1) not in ("turn off", "switch off", "put") or bool(rest & COMPUTER)


def matched(text):
    t = text.lower()
    core = re.sub(POLITE, "", t.strip())
    if re.match(r"(?:do not|don't|dont|never)\b", core) and "do not disturb" not in core:
        return []                                        # "do not close chrome" must not close it
    if key_command(text):                                # "type ...", "press enter", "copy", "scroll down"
        return ["keys"]
    if power_request(t):
        return ["power"]
    if not command_shaped(t):
        return []
    if re.match(r"(?:open|launch|show)\b", core) and said("settings", core):
        return ["open"]                                  # "open bluetooth settings" opens Settings
    if re.match(r"(?:search|google search)\b", core):
        return ["search"]                                # free text: "search for volume and brightness tips"
    groups =[g for g, ws in TRIGGERS.items() if g != "power" and any(said(w, t) for w in ws)]
    # "connect to the internet" is Wi-Fi, not Bluetooth, unless a Bluetooth word was also said
    bt_specific = [w for w in TRIGGERS["bluetooth"] if w not in ("connect", "disconnect", "pair")]
    if "wifi" in groups and "bluetooth" in groups and not any(said(w, t) for w in bt_specific):
        groups.remove("bluetooth")
    if re.fullmatch(r"\s*(play|resume)( it| again| the video| video| the song| music)?\s*[.!]?\s*", t):
        return ["media"]                                  # bare "play it" = resume, not a search
    if "media" not in groups and re.search(r"\b(back|ahead)\b.*\b(seconds?|secs?|minutes?|mins?)\b", t):
        groups.append("media")                            # "back 1 minute 30 seconds"
    if "close" in groups and "open" in groups:           # "close the rest and keep terminal open"
        groups.remove("open")
    if "info" in groups and [w for w in re.findall(r"[a-z']+", core) if w not in INFO_FILLER | set(TRIGGERS["info"])]:
        groups.remove("info")                            # "what time is it in tokyo" is a question
    if "info" in groups and "open" in groups:
        groups.remove("open")                            # "show me the time"
    if "lock" in groups and not (re.search(r"\b(screen|computer|laptop|pc)\b", core)
                                 or re.fullmatch(r"lock( it| now)?[.!]*", core)):
        groups.remove("lock")                            # "turn off caps lock"
    if "video_log" in groups and not (re.search(r"\b(videos?|songs?|music|playing|this|it)\b", core)
                                      or re.fullmatch(r"log[.!]*", core)):
        groups.remove("video_log")                       # "log out"
    if "media" in groups and not any(said(w, t) for w in TRIGGERS["media"] if w not in SEEK_WORDS) and \
            [w for w in re.findall(r"[a-z]+", spoken_numbers(core))
             if w not in SEEK_FILLER and not re.fullmatch(r"(hour|hr|minute|min|second|sec)s?", w)]:
        groups.remove("media")                           # "forward this email to john", "skip the ad"
    if "media" in groups and "search" in groups and re.search(r"\b(next|previous|pause|resume|stop)\b", core):
        groups.remove("search")                          # "play the next song"
    if re.match(QUESTION, core):                         # "is the wifi off" must not turn it off
        groups = [g for g in groups if g in ("info", "math")]
    return groups


def claim(action):
    """True if this tool call may run: first call for this command, and a trigger word was said."""
    if state["done"] or action not in state.get("groups", matched(state["query"])):
        return False
    state["done"] = True
    return True


def ask_follow_up(handler):
    state["pending"] = (handler, time.time() + CONFIRM_SECONDS)


def turning_off(q):
    return bool(re.search(r"\b(off|disable|disconnect|stop|shut)\b", q))


_SMALL = ("zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen sixteen "
          "seventeen eighteen nineteen").split()
_TENS = "twenty thirty forty fifty sixty seventy eighty ninety".split()
NUMBER_WORDS = {w: i for i, w in enumerate(_SMALL)} | {w: 20 + 10 * i for i, w in enumerate(_TENS)}
_UNDER_100 = rf"(?:(?:{'|'.join(_TENS)})(?:[\s-]+(?:{'|'.join(_SMALL[1:10])}))?|{'|'.join(_SMALL)})"
SPOKEN_NUMBER = re.compile(rf"\b(?:(?:a|{'|'.join(_SMALL[1:10])})\s+hundred(?:\s+(?:and\s+)?{_UNDER_100})?"
                           rf"|{_UNDER_100})\b")
UNIT = r"(hour|hr|minute|min|second|sec)s?\b"


def spoken_numbers(text):
    """Number words to digits: 'twenty five' -> '25', 'a hundred' -> '100', 'one hundred twenty' -> '120'."""
    def value(m):
        total = 0
        for w in re.split(r"[\s-]+", m.group()):
            total = (total or 1) * 100 if w == "hundred" else total + NUMBER_WORDS.get(w, 0)   # 'a', 'and' add 0
        return str(total)
    return SPOKEN_NUMBER.sub(value, text)


def number_in(q):
    m = re.search(r"\d+(?:\.\d+)?", spoken_numbers(q))
    return float(m.group()) if m else None


def duration_seconds(q):
    """Sum of every 'number unit' pair: '1 hour 30 minutes' -> 5400, 'an hour and a half' -> 5400,
    'half a minute' -> 30, 'forward 1 minute 30' -> 90. 0 when no duration was said."""
    q = spoken_numbers(q.lower())
    q = re.sub(r"\bhalf (?:a|an) (hour|hr|minute|min)\b", r"0.5 \1", q)
    q = re.sub(rf"\b(?:a|an) (?={UNIT})", "1 ", q)
    q = re.sub(r"(\d+(?:\.\d+)?) (?:(hour|hr|minute|min)s? and a half|and a half (hour|hr|minute|min)s?)\b",
               lambda m: f"{float(m.group(1)) + 0.5:g} {m.group(2) or m.group(3)}", q)
    q = re.sub(r"\b(minutes?|mins?) (?:and )?(\d+)\b(?! *(?:hour|hr|minute|min|second|sec|percent|%))",
               r"\1 \2 seconds", q)
    return sum(float(n) * {"h": 3600, "m": 60, "s": 1}[u[0]] for n, u in re.findall(rf"(\d+(?:\.\d+)?)\s*{UNIT}", q))


def say_duration(secs):
    """5400 -> '1 hour and 30 minutes'."""
    h, rest = divmod(round(secs), 3600)
    parts = [f"{n} {unit}{'s' if n != 1 else ''}"
             for n, unit in ((h, "hour"), (rest // 60, "minute"), (rest % 60, "second")) if n]
    return one_of(parts, "and") if parts else "0 seconds"


def seek_seconds(q):
    """'forward 30 seconds' -> 30, 'back 1 minute 30 seconds' -> 90, 'rewind two minutes' -> 120; default 10."""
    return duration_seconds(q) or 10


DIRECTION = r"\b(up|down|increase|decrease|raise|reduce|lower|louder|quieter|softer|less|more|higher|brighter|dimmer|dim|darker)\b"


def adjust(q, current, default_step=10):
    """New percentage. A number is absolute ('set to 60', 'brightness 70') unless a direction word comes
    without 'to' ('up 20', 'up by 20'). Clamped to 1..100."""
    n = number_in(q)
    down = re.search(r"\b(down|decrease|reduce|lower|less|quieter|softer|dim|dimmer|darker)\b", q)
    if n is not None and (re.search(r"\bto\b", q) or not re.search(DIRECTION, q)) and not re.search(r"\bby\b", q):
        new = n                                            # absolute: "set volume to 60"
    else:
        new = current + (-1 if down else 1) * (n if n is not None else default_step)
    return int(max(1, min(new, 100)))


def failed(out):
    """True when a command's output reports an error (sh() only returns the text)."""
    return bool(re.search(r"\b(error|failed|not available|no default controller)\b", out, re.I))


# ---------- matching spoken names against real things (apps, networks, devices) ----------

# Words that never identify a specific app/network/device: grammar, command words, product filler.
STOPWORDS = set("""a an the my me to on in of for with and or please can could you would will i it is are this
that which what open launch start go show connect disconnect pair join use switch turn set app application
wifi wi fi network internet hotspot bluetooth headphone headphones headset earphone earphones earbuds
neckband speaker device best fastest strongest speed signal s pro wireless elite anc web browser new""".split())


def words(text):
    return re.findall(r"[a-z0-9]+", text.lower())


def fuzzy_score(name, spoken):
    """(how much of what was said matches, fraction of the name covered). Each spoken word counts once,
    so long keyword lists can't pile up points. Fuzzy, so 'cms' finds 'CMF', 'rockers' finds 'Rockerz'."""
    name_words = [w for w in words(name) if w not in STOPWORDS and len(w) >= 2]
    total, hit = 0.0, set()
    for sw in spoken:
        ratio, nw = max(((difflib.SequenceMatcher(None, nw, sw).ratio(), nw) for nw in name_words),
                        default=(0, None))
        if nw and ratio >= (0.66 if len(nw) <= 4 else 0.8):     # 0.75 let "cancel" match "channel"
            total += ratio
            hit.add(nw)
    return round(total, 3), (round(len(hit) / len(name_words), 3) if name_words else 0)


def best_match(text, items, names_of):
    """Best item for the spoken text. names_of(item) gives names, or (name, weight) pairs so e.g. an app's
    real name beats its keywords. Returns (item, ties): item is None when nothing matches or several tie."""
    said_words = [w for w in words(text) if w not in STOPWORDS]
    spoken = said_words + [a + b for a, b in zip(said_words, said_words[1:])]   # "geca tech" -> "gecatech"
    scored = []
    for item in items:
        best = (0, 0)
        for n in names_of(item):
            name, weight = n if isinstance(n, tuple) else (n, 1.0)
            if name:
                sc = fuzzy_score(name, spoken)
                best = max(best, (round(sc[0] * weight, 3), sc[1]))
        if best[0] > 0 and best[0] >= 0.5 * len(said_words):   # must explain at least half of what was said
            scored.append((best, item))
    scored.sort(key=lambda x: x[0], reverse=True)
    if not scored:
        return None, []
    top = scored[0][0][0]
    tied = [(sc, it) for sc, it in scored if sc[0] == top]
    ties = [it for _, it in tied]
    exact = [it for sc, it in tied if sc[1] == 1]           # whole name said: "officenet" beats "officenet guest"
    candidates = exact or ties
    if len({str(names_of(c)[0]) for c in candidates}) == 1:  # one match, or several with the same name
        return candidates[0], ties
    return None, ties


def one_of(names, joiner="or"):
    names = list(dict.fromkeys(names))
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + f" {joiner} " + names[-1]


_YES = r"(?:yes|yeah|yep|yup|sure|okay|ok|confirm|do it|go ahead|please do)"
REFUSALS = r"\b(?:no|nope|not|don't|dont|cancel|stop|wait|never ?mind|none|nothing)\b"
ORDINALS = {"first": 0, "second": 1, "third": 2, "fourth": 3, "last": -1}


def yes_no(text):
    """'no' when any refusal word was said ('no okay never mind'); 'yes' only when the whole answer is a yes
    ('ok', 'yes please', 'yeah go ahead'); None for anything else, which is then handled as a new command."""
    t = " ".join(re.findall(r"[a-z']+", text.lower()))
    if re.search(REFUSALS, t):
        return "no"
    return "yes" if re.fullmatch(rf"{_YES}(?: {_YES})*(?: please)?", t) else None


def ask_choice(options, label, act, verb, suggest=False):
    """Ask which option to use. The answer can be a name, 'the second one', 'any of them' or 'cancel'.
    act(option) does it and returns the reply. suggest: ask "Did you mean <first>?" and take "yes" for it."""
    options = list({label(o): o for o in reversed(options)}.values())[::-1]   # one per name, order kept
    shown = options[:4]

    def answer(text):
        t = text.lower().strip(" .!?")
        verdict = yes_no(text)
        if verdict == "no":
            return "Okay, cancelled"
        if matched(text):
            return None                                 # "set a timer ..." is a new command, not an answer
        m = re.fullmatch(r"(?:the )?(first|second|third|fourth|last)(?: one)?", t)
        if m and ORDINALS[m.group(1)] < len(shown):
            return act(shown[ORDINALS[m.group(1)]])
        chosen, _ = best_match(text, options, lambda o: [label(o)])
        if chosen:
            return act(chosen)
        if verdict == "yes":
            if suggest:
                return act(shown[0])
            ask_follow_up(answer)                       # "yes" to "which one?": ask again
            return f"Which one? {one_of([label(o) for o in shown])}?"
        if any(said(w, t) for w in ("any", "anyone", "whatever", "you choose", "you pick", "doesn't matter")):
            return act(shown[0])
        return None                                     # anything else: treat as a new command
    ask_follow_up(answer)
    names = [label(o) for o in shown]
    if suggest:
        return f"Did you mean {names[0]}?" + (f" Or {one_of(names[1:])}?" if len(names) > 1 else "")
    more = f", and {len(options) - 4} more" if len(options) > 4 else ""
    return f"I found {one_of(names, 'and')}{more}. Which one should I {verb}?"


APP_DIRS = ["/usr/share/applications", "/usr/local/share/applications", "~/.local/share/applications",
            "/var/lib/snapd/desktop/applications", "/var/lib/flatpak/exports/share/applications",
            "~/.local/share/flatpak/exports/share/applications"]
_apps_cache = []


def installed_apps():
    """Apps from the system's .desktop launchers (what the app grid shows). Cached after the first call."""
    if _apps_cache:
        return _apps_cache
    seen = set()
    for d in APP_DIRS:
        d = os.path.expanduser(d)
        if not os.path.isdir(d):
            continue
        for fn in sorted(os.listdir(d)):
            if not fn.endswith(".desktop") or fn in seen:
                continue
            try:
                text = read_file(os.path.join(d, fn))
            except OSError:
                continue
            entry = text.split("[Desktop Entry]", 1)[-1].split("\n[", 1)[0]   # main section only, not actions
            f = dict(re.findall(r"^([A-Za-z]+)=(.*)$", entry, re.M))
            if f.get("Type") != "Application" or f.get("NoDisplay") == "true" or f.get("Hidden") == "true":
                continue
            seen.add(fn)
            kinds = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", f.get("Categories", ""))   # "WebBrowser" -> "Web Browser"
            _apps_cache.append({"id": fn[:-8], "name": f.get("Name", ""), "generic": f.get("GenericName", ""),
                                "kinds": {singular(w) for w in words(f"{kinds} {f.get('GenericName', '')}")},
                                "keywords": f.get("Keywords", "").replace(";", " "), "exec": f.get("Exec", ""),
                                "browser": "WebBrowser" in f.get("Categories", "")})
    return _apps_cache


def sound(word):
    """Rough sound code so misheard words still match: 'play'/'brave' -> 'BRA'/'BRAF', 'crave' -> 'GRAF'.
    Similar consonants share a letter (b/p, r/l, f/v, ...), and 'ay' sounds (ay, ai, ei, a_e) become 'A'."""
    w = word.lower()
    w = re.sub(r"a([^aeiou])e$", r"A\1", w)                     # brave -> brAv
    w = re.sub(r"ay|ai|ei|ey", "A", w)
    w = re.sub(r"ph", "f", w)
    for letters, code in (("bp", "B"), ("dt", "D"), ("gkcq", "G"), ("fv", "F"), ("szx", "S"),
                          ("lr", "R"), ("mn", "N"), ("hyw", ""), ("eiou", "E")):
        w = re.sub(f"[{letters}]", code, w)
    return re.sub(r"(.)\1+", r"\1", w.replace("a", "E")).rstrip("E") or w


def sounds_like(a, b):
    return max(difflib.SequenceMatcher(None, a, b).ratio(), difflib.SequenceMatcher(None, sound(a), sound(b)).ratio())


def singular(w):
    return w[:-1] if len(w) > 3 and w.endswith("s") and not w.endswith("ss") else w


def apps_of_kind(key):
    """'browser' -> every installed browser, 'editor' -> every editor. Each word said must be one of the app's
    categories (from its launcher) or part of its name."""
    said_words = [singular(w) for w in words(key) if w not in ("app", "apps", "application", "some", "any")]
    if not said_words:
        return []
    found = [a for a in installed_apps()
             if all(w in a["kinds"] | {singular(n) for n in words(a["name"])} for w in said_words)]
    # Closest first: the word in the app's name, then in its description ("Text Editor"), then category only.
    return sorted(found, key=lambda a: -sum(2 * (w in map(singular, words(a["name"])))
                                            + (w in map(singular, words(a["generic"]))) for w in said_words))


def find_app(text, browsers_only=False):
    apps = [a for a in installed_apps() if a["browser"] or not browsers_only]
    return best_match(text, apps, lambda a: [(a["name"], 1.0), (a["generic"], 0.8), (a["keywords"], 0.6)])


def named_by(app, text):
    """True when what was said matches the app's name or description, not only its search keywords."""
    spoken = [w for w in words(text) if w not in STOPWORDS]
    spoken += [a + b for a, b in zip(spoken, spoken[1:])]
    return any(fuzzy_score(n, spoken)[0] for n in (app["name"], app["generic"]) if n)


def launch_app(app, *args):
    if not args:
        launch(["gtk-launch", app["id"]])
        return
    cmd = [a for a in shlex.split(app["exec"]) if not re.fullmatch(r"%[a-zA-Z]", a)]   # drop %U %f %i ...
    launch(cmd + list(args))


def find_browser(text):
    """A browser named after in/on/using ('youtube in brave'), or None for the default browser."""
    m = re.search(r"\b(?:in|on|using|with)\s+(.+)$", text.lower())
    if not m:
        return None
    app, _ = find_app(m.group(1), browsers_only=True)
    return app


def browse(url):
    b = find_browser(state["query"]) or state.get("browser")     # keep using the browser you last named
    state["browser"] = b
    if b:
        launch_app(b, url)
    else:
        launch(["xdg-open", url])
    return b["name"] if b else None


def open_file_in_editor(path):
    desktop = sh("xdg-mime", "query", "default", "text/plain").strip()
    launch(["gtk-launch", desktop[:-8], path] if desktop.endswith(".desktop") else ["xdg-open", path])


def site_url(name):
    """URL for a spoken site name, or None if it doesn't look like a site."""
    site = next((s for s in SITES if said(s, name)), None)
    if site:
        return "https://" + SITES[site], site
    if re.fullmatch(r"[a-z0-9-]+(\.[a-z0-9-]+)+", name):          # "open example.org"
        return "https://" + name, name
    if re.fullmatch(r"[a-z0-9-]+", name):                         # single word: "open reddit" -> reddit.com
        return f"https://{name}.com", name
    return None


@cactus.tool
def open(name: str, browser: str = ""):
    "Open an installed app or a website. name: app name (text editor, settings, vs code, brave...) or website (youtube, gmail, github...). browser: browser to use if the user names one."
    if not claim("open"):
        return {"skipped": name}
    key = re.sub(r"^(the|my|a)\s+", "", name.lower().strip(" .!?"))
    key = re.split(r"\s+(?:in|on|using|with)\s+", key)[0].strip()        # "youtube in brave" -> "youtube"
    if not key or key in ("it", "that", "this", "the", "my", "over", "them", "something"):
        return {"say": "Open what?"}
    folder = re.fullmatch(r"(downloads|documents|pictures|photos|music|videos|desktop|home)( folder| directory)?", key)
    if folder and (folder.group(2) or folder.group(1) in ("downloads", "documents", "pictures", "photos")):
        sub = {"photos": "Pictures", "home": ""}.get(folder.group(1), folder.group(1).capitalize())
        launch(["xdg-open", os.path.join(os.path.expanduser("~"), sub)])
        return {"say": f"Opening your {folder.group(1)} folder"}
    def start(a):
        if a["browser"]:
            state["browser"] = a                    # "open brave", then "open youtube" goes to Brave
            if any(r[0]["id"] == a["id"] for r in running_apps()):   # already open: a tab, not a new window
                launch_app(a, "about:newtab" if "firefox" in a["id"] else "chrome://newtab")
                return f"Opening a new tab in {a['name']}"
        launch_app(a)
        return f"Opening {a['name']}"
    exact = lambda k: next((a for a in installed_apps() if a["name"].lower() == k), None)
    if not exact(key) and said("settings", key):   # "open sound settings": the Settings app
        key = "settings"
    if exact(key):                                  # "google chrome" is the app, not google.com
        return {"say": start(exact(key))}
    if any(said(s, key) for s in SITES):                                  # spoken site shortcut
        url, label = site_url(key)
        b = browse(url)
        return {"say": f"Opening {label}" + (f" in {b}" if b else "")}
    kind = apps_of_kind(key)                                               # "open browser" -> which one?
    if len(kind) > 1 and not any(a["name"].lower() == key for a in kind):
        return {"say": ask_choice(kind, lambda a: a["name"], start, "open")}
    if not kind:                                    # "open wave browser": the kind was heard, the name misheard
        for w in words(key):
            group = apps_of_kind(w)
            rest = [x for x in words(key) if x != w and x not in STOPWORDS]
            if len(group) < 2 or not rest:
                continue
            def sounds(a):                          # how close the misheard word is to the app's own name
                names = [n for n in words(a["name"]) if n not in STOPWORDS and n != w]
                return max((sounds_like(r, n) for r in rest for n in names), default=0)
            ranked = sorted(group, key=sounds, reverse=True)
            if sounds(ranked[0]) >= 0.75 and sounds(ranked[0]) > sounds(ranked[1]):
                return {"say": start(ranked[0])}
            return {"say": ask_choice(ranked, lambda a: a["name"], start, "open",
                                      suggest=sounds(ranked[0]) >= 0.4)}
    app, ties = (kind[0], []) if len(kind) == 1 else find_app(key)
    if app:
        if not named_by(app, key):                  # found only through its keywords: check first
            return {"say": ask_choice([app], lambda a: a["name"], start, "open", suggest=True)}
        return {"say": start(app)}
    if ties:
        return {"say": ask_choice(ties, lambda a: a["name"], start, "open")}
    site = site_url(key)
    if not site:
        return {"say": f"I couldn't find an app called {key}"}
    if "." in key:                                  # "open example.org"
        b = browse(site[0])
        return {"say": f"Opening {site[1]}" + (f" in {b}" if b else "")}

    def go(text):                                   # an unknown word may be a mishearing: ask before a website
        verdict = yes_no(text)
        if verdict == "yes":
            b = browse(site[0])
            return f"Opening {site[1]}.com" + (f" in {b}" if b else "")
        return "Okay" if verdict == "no" else None
    ask_follow_up(go)
    return {"say": f"I don't know an app called {key}. Should I open {key} dot com?"}


@cactus.tool
def search(query: str, site: str = "", browser: str = ""):
    "Search the web or YouTube. query: what to search for. site: youtube or google. browser: brave, firefox or chrome if named."
    if not claim("search"):
        return {"skipped": query}
    q = without_place(query.strip())                                       # drop "on youtube in brave"
    q = re.sub(r"^(?:search google|google search|search|google|look up|play)(?: for| about)?\s+", "", q,
               flags=re.I).strip(" .!?")
    if not q:
        return {"say": "What should I search for?"}
    play = said("play", state["query"].lower())
    on_yt = "youtube" in f"{site} {state['query']}".lower() or play
    url = "https://www.youtube.com/results?search_query=" if on_yt else "https://www.google.com/search?q="
    video = first_youtube_video(url + urllib.parse.quote_plus(q)) if play else None
    if video:                                             # "play X": open the top result, it starts playing
        browse(video)
        return {"say": f"Playing {q} on YouTube"}
    browse(url + urllib.parse.quote_plus(q))
    return {"say": f"Searching {'YouTube' if on_yt else 'the web'} for {q}"}


def without_place(q):
    """Drop a trailing 'on youtube' / 'in brave', but keep 'restaurants in chennai' and 'walking on sunshine'."""
    for m in re.finditer(r"\s+(?:on|in|using|with)\s+", q, re.I):
        pieces = re.split(r"\s+(?:on|in|using|with)\s+", q[m.end():].strip(" .!?"), flags=re.I)
        if all(p.lower() in ("youtube", "google", "the web") or find_app(p, browsers_only=True)[0] for p in pieces):
            return q[:m.start()]
    return q


def first_youtube_video(results_url):
    """Link to the top video on a YouTube search page, or None (offline, or YouTube changed its page)."""
    try:
        req = urllib.request.Request(results_url, headers={"User-Agent": "Mozilla/5.0", "Accept-Language": "en"})
        page = urllib.request.urlopen(req, timeout=8).read().decode("utf-8", "ignore")
    except OSError:
        return None
    m = re.search(r'"videoId":"([\w-]{11})"', page)
    return f"https://www.youtube.com/watch?v={m.group(1)}" if m else None


def direct_open_or_search(group, text):
    """Parse 'open X [in Y]' / 'search X' without Needle. None if there's no target after the verb."""
    m = re.search(r"\b(?:open|launch|start|go to|show me|search(?: for)?|google|look up|find|play)\s+(.+)",
                  text, re.I)
    if not m:
        return None
    return search(m.group(1)) if group == "search" else open(m.group(1))


# ---------- volume / brightness / media ----------

@cactus.tool
def volume(change: str = ""):
    "Change speaker volume. change: up, down, mute, unmute, or a percentage like 50."
    if not claim("volume"):
        return {"skipped": change}
    q = f"{change} {state['query']}".lower()
    sink = "@DEFAULT_AUDIO_SINK@"
    if said("unmute", q) or re.search(r"\b(turn|switch) (the )?(volume|sound) on\b|\b(turn|switch) on (the )?(volume|sound)\b", q):
        return {"say": "I couldn't unmute" if failed(sh("wpctl", "set-mute", sink, "0")) else "Unmuted"}
    if said("mute", q) or re.search(r"\b(off|zero|silent|silence|stop)\b", q) or number_in(q) == 0:
        return {"say": "I couldn't mute" if failed(sh("wpctl", "set-mute", sink, "1")) else "Muted"}
    m = re.search(r"Volume:\s*([\d.]+)", sh("wpctl", "get-volume", sink))
    if not m:                                          # don't guess: a wrong guess could blast the speakers
        return {"say": "I couldn't read the volume, so I left it as it is"}
    current = round(float(m.group(1)) * 100)
    if re.search(r"\b(max|maximum|full)\b", q):
        new = 100
    elif number_in(q) is None and not re.search(DIRECTION, q):
        return {"say": f"Volume is at {current} percent"}
    else:
        new = adjust(q, current)
    if failed(sh("wpctl", "set-mute", sink, "0") + sh("wpctl", "set-volume", sink, f"{new}%")):
        return {"say": "I couldn't change the volume"}
    return {"say": f"Volume {new} percent"}


@cactus.tool
def brightness(change: str = ""):
    "Change screen brightness. change: up, down, or a percentage like 70."
    if not claim("brightness"):
        return {"skipped": change}
    try:
        cur, mx = (int(read_file(f"{BACKLIGHT}/{n}")) for n in ("brightness", "max_brightness"))
    except OSError:
        return {"say": "I can't control the brightness on this screen"}
    q = f"{change} {state['query']}".lower()
    new = 100 if re.search(r"\b(max|maximum|full)\b", q) else adjust(q, round(cur * 100 / mx))
    out = sh("busctl", "call", "org.freedesktop.login1", "/org/freedesktop/login1/session/auto",
             "org.freedesktop.login1.Session", "SetBrightness", "ssu", "backlight", os.path.basename(BACKLIGHT),
             str(mx * new // 100))
    return {"say": "I couldn't change the brightness" if failed(out) else f"Brightness {new} percent"}


def mpris_players():
    try:
        names = json.loads(sh("busctl", "--user", "--json=short", "list") or "[]")
    except json.JSONDecodeError:
        return []
    return [n["name"] for n in names if n.get("name", "").startswith("org.mpris.MediaPlayer2.")]


def mpris_prop(player, prop):
    out = sh("busctl", "--user", "--json=short", "get-property", player, "/org/mpris/MediaPlayer2",
             "org.mpris.MediaPlayer2.Player", prop)
    try:
        return json.loads(out)["data"]
    except (json.JSONDecodeError, KeyError, TypeError):
        return None                                   # e.g. AppArmor denied (snap browser from snap VS Code)


@cactus.tool
def media(action: str = ""):
    "Control music or video playback. action: play, pause, next or previous."
    if not claim("media"):
        return {"skipped": action}
    q = f"{action} {state['query']}".lower()
    players = mpris_players()
    if not players:
        return {"say": "Nothing is playing"}
    statuses = {p: mpris_prop(p, "PlaybackStatus") for p in players}
    if all(s is None for s in statuses.values()):
        return {"say": "The browser won't let me control it. Run me from the normal Terminal app."}
    seek = re.search(r"\b(forward|ahead|rewind|backwards?|back|seek)\b", q) or \
        (said("skip", q) and re.search(r"\b(seconds?|secs?|minutes?|mins?)\b", q))     # "skip 10 seconds"
    wanted = "Paused" if not (seek or re.search(r"\b(pause|stop|next|skip|previous)\b", q)) else "Playing"
    player = next((p for p, s in statuses.items() if s == wanted), players[0])   # resume the paused one
    if seek:                                              # MPRIS Seek takes microseconds, negative = back
        secs = seek_seconds(q)
        back = re.search(r"\b(rewind|backwards?|back)\b", q)
        sh("busctl", "--user", "call", player, "/org/mpris/MediaPlayer2", "org.mpris.MediaPlayer2.Player",
           "Seek", "x", str(int((-secs if back else secs) * 1_000_000)))
        return {"say": f"{'Back' if back else 'Forward'} {say_duration(secs)}"}
    if re.search(r"\b(next|skip)\b", q):
        method, say = "Next", "Next"
    elif re.search(r"\bprevious\b", q):
        method, say = "Previous", "Previous"
    elif re.search(r"\b(pause|stop)\b", q):
        method, say = "Pause", "Paused"
    else:
        method, say = "Play", "Playing"
    sh("busctl", "--user", "call", player, "/org/mpris/MediaPlayer2", "org.mpris.MediaPlayer2.Player", method)
    return {"say": say}


@cactus.tool
def log_video():
    "Log the video or song that is currently playing (title, channel, link) to a notes file and open it."
    if not claim("video_log"):
        return {"skipped": "log"}
    players = mpris_players()
    if not players:
        return {"say": "Nothing is playing right now"}
    entries = []
    for p in players:
        meta = mpris_prop(p, "Metadata")
        if not meta:
            continue
        get = lambda k: (meta.get(k) or {}).get("data")
        title, artist, url = get("xesam:title"), get("xesam:artist"), get("xesam:url")
        if isinstance(artist, list):
            artist = ", ".join(artist)
        if title:
            status = mpris_prop(p, "PlaybackStatus")
            entries.append((title, artist or "", url or "", status or ""))
    if not entries:
        return {"say": "The browser won't share what's playing. Run me from the normal Terminal app, not VS Code."}
    try:
        old = read_file(VIDEO_LOG) if os.path.exists(VIDEO_LOG) else ""
        os.makedirs(os.path.dirname(VIDEO_LOG), exist_ok=True)
        stamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
        last = old.strip().splitlines()[-1] if old.strip() else ""
        new_lines = []
        for title, artist, url, status in entries:
            if f"| {title}" in last:
                continue                               # same video as the last entry: don't duplicate
            parts = [stamp, title, artist, url, status.lower() if status != "Playing" else ""]
            new_lines.append("- " + " | ".join(x for x in parts if x) + "\n")
        if not new_lines:
            open_file_in_editor(VIDEO_LOG)
            return {"say": f"{entries[0][0]} is already logged"}
        with builtins.open(VIDEO_LOG, "a") as f:
            if not old:
                f.write("# Videos logged by Needle\n\n")
            f.writelines(new_lines)
    except OSError as e:
        return {"say": f"I couldn't write the log file: {e.strerror}"}
    open_file_in_editor(VIDEO_LOG)
    more = f" and {len(new_lines) - 1} more" if len(new_lines) > 1 else ""
    return {"say": f"Logged {entries[0][0]}{more}"}


# ---------- timer / lock / info / maths ----------

@cactus.tool
def timer(duration: str = "", message: str = ""):
    "Set a timer or reminder. duration: like 5 minutes, 30 seconds, 1 hour. message: what to remind about."
    if not claim("timer"):
        return {"skipped": duration}
    q = f"{duration} {state['query']}".lower()
    secs = duration_seconds(q)
    if not secs:
        original = state["query"]

        def how_long(text):
            if not duration_seconds(text):
                return None                             # not a duration: a new command
            state.update(query=f"{text}. {original}", done=False, groups=["timer"])
            return timer().get("say")
        ask_follow_up(how_long)
        return {"say": "For how long?"}
    label = say_duration(secs)
    if secs < 1 or secs > 24 * 3600:
        return {"say": "I can set timers from one second up to 24 hours"}
    rem = re.search(r"\bto (.+)$", q) if said("remind", q) else None
    text = message or (f"Reminder: {rem.group(1)}" if rem else "Time's up")

    def ring():
        launch(["notify-send", "-u", "critical", "Needle", text])
        launch(["spd-say", text])

    t = threading.Timer(secs, ring)
    t.daemon = True                                    # don't block Ctrl+C
    t.start()
    return {"say": f"Timer set for {label}"}


@cactus.tool
def lock_screen():
    "Lock the computer screen."
    if not claim("lock"):
        return {"skipped": "lock"}
    launch(["loginctl", "lock-session"])
    return {"say": "Locking the screen"}


def battery():
    out = sh("upower", "-i", "/org/freedesktop/UPower/devices/battery_BAT0")
    pct, st = re.search(r"percentage:\s*(\S+)", out), re.search(r"state:\s*(\S+)", out)
    return f"{pct.group(1)} ({st.group(1)})" if pct and st else "unknown"


@cactus.tool
def tell(info: str = ""):
    "Tell the current time, date, day or battery level. info: time, date, day or battery."
    if not claim("info"):
        return {"skipped": info}
    q = f"{info} {state['query']}".lower()
    now = datetime.datetime.now()
    if said("battery", q) or said("charge", q):
        level = battery()
        return {"say": "I couldn't read the battery" if level == "unknown" else f"Battery is at {level}"}
    if said("date", q) or said("day", q):
        return {"say": now.strftime("Today is %A, %d %B %Y")}
    return {"say": now.strftime("It's %I:%M %p").replace(" 0", " ")}


WORD_OPS = {"plus": "+", "minus": "-", "times": "*", "multiplied by": "*", "x": "*",
            "divided by": "/", "over": "/", "to the power of": "**"}
OPS = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv,
       ast.Mod: operator.mod, ast.Pow: operator.pow, ast.USub: operator.neg}


def safe_eval(expr):
    """+ - * / % ** and brackets on numbers only; refuses huge powers that would hang the CPU."""
    def ev(n):
        if isinstance(n, ast.Constant) and isinstance(n.value, (int, float)):
            return n.value
        if isinstance(n, ast.BinOp) and type(n.op) in OPS:
            a, b = ev(n.left), ev(n.right)
            if isinstance(n.op, ast.Pow) and (abs(b) > 100 or abs(a) > 1e6):
                raise ValueError("too big")
            return OPS[type(n.op)](a, b)
        if isinstance(n, ast.UnaryOp) and type(n.op) in OPS:
            return OPS[type(n.op)](ev(n.operand))
        raise ValueError("unsupported")
    return ev(ast.parse(expr, mode="eval").body)


@cactus.tool
def calculate(expression: str = ""):
    "Do arithmetic. expression: the maths to compute, e.g. 25 * 4 or 15 percent of 200."
    if not claim("math"):
        return {"skipped": expression}
    q = state["query"].lower().replace(",", "")
    m = re.search(r"([\d.]+)\s*(?:percent|%)\s*of\s*([\d.]+)", q)
    expr = f"{m.group(1)}/100*{m.group(2)}" if m else q
    for w, o in WORD_OPS.items():
        expr = re.sub(rf"\b{w}\b", o, expr)
    expr = re.sub(r"[^\d.+\-*/%() ]", " ", expr).strip()
    try:
        val = safe_eval(expr)
    except ZeroDivisionError:
        return {"say": "You can't divide by zero"}
    except (OverflowError, ValueError) as e:
        if isinstance(e, OverflowError) or str(e) == "too big":
            return {"say": "That number is too big for me"}
        return {"error": "bad expression"}
    except SyntaxError:
        return {"error": "bad expression"}
    if isinstance(val, complex):                       # "(0-8) to the power of 0.5"
        return {"say": "That has no real-number answer"}
    val = round(val, 4)
    return {"say": f"That's {int(val) if val == int(val) else val}"}


# ---------- wifi ----------

def nmcli_fields(line):
    """Split an nmcli -t line on unescaped ':' and unescape '\\:'."""
    return [f.replace("\\:", ":").replace("\\\\", "\\") for f in re.split(r"(?<!\\):", line)]


def saved_wifi_names():
    """Saved Wi-Fi profile names, or None when NetworkManager couldn't be asked (it always lists something)."""
    out = sh("nmcli", "-t", "-f", "NAME,TYPE", "connection", "show")
    if not out.strip():
        return None
    return [nmcli_fields(l)[0] for l in out.splitlines() if l.endswith(":802-11-wireless")]


def is_saved(ssid, saved):
    # NetworkManager names profiles after the SSID, adding " 1", " 2" for duplicates
    return any(n == ssid or re.fullmatch(re.escape(ssid) + r" \d+", n) for n in saved)


def wifi_aps():
    """Visible networks, strongest access point per SSID: [{ssid, bssid, signal, in_use, saved, secure}]."""
    saved = saved_wifi_names()
    groups = {}
    for l in sh("nmcli", "-t", "-f", "IN-USE,BSSID,SSID,SIGNAL,SECURITY", "device", "wifi", "list",
                "--rescan", "yes", timeout=30).splitlines():
        f = nmcli_fields(l)
        if len(f) == 5 and f[2] and f[3].isdigit():    # hidden networks have no SSID
            groups.setdefault(f[2], []).append(f)
    aps = []
    for ssid, rows in groups.items():
        top = max(rows, key=lambda f: int(f[3]))
        current = next((f for f in rows if f[0] == "*"), None)
        aps.append({"ssid": ssid, "bssid": top[1], "signal": int(top[3]), "in_use": current is not None,
                    "current_signal": int(current[3]) if current else 0,
                    "saved": None if saved is None else is_saved(ssid, saved), "secure": top[4] not in ("", "--")})
    # strongest first; on equal signal prefer the network already in use (no pointless switch to a hotspot)
    return sorted(aps, key=lambda a: (-a["signal"], not a["in_use"])), saved


def wifi_radio_on():
    if "enabled" in sh("nmcli", "radio", "wifi"):
        return True
    sh("nmcli", "radio", "wifi", "on")
    for _ in range(5):                                  # radio takes a few seconds to come up
        time.sleep(1)
        if "enabled" in sh("nmcli", "radio", "wifi"):
            return True
    return False


def wifi_join(ap):
    if ap["in_use"] and ap["signal"] - ap["current_signal"] < MIN_WIFI_GAIN:
        return f"You're already connected to {ap['ssid']}"
    if ap["saved"] is False and ap["secure"]:      # None: saved profiles unknown, let nmcli try
        return f"{ap['ssid']} needs a password. Connect to it once from Settings and I'll remember it."
    out = sh("nmcli", "device", "wifi", "connect", ap["bssid"], timeout=45)   # reuses the saved profile
    if "successfully activated" in out:
        return f"Connected to {ap['ssid']}, signal {ap['signal']} percent"
    if re.search(r"secrets|password", out, re.I):
        return f"{ap['ssid']} needs a password. Connect to it once from Settings and I'll remember it."
    return f"I couldn't connect to {ap['ssid']}"


@cactus.tool
def wifi(state_: str = "", network: str = ""):
    "Turn Wi-Fi on or off, or connect to a Wi-Fi network by name, or to the strongest known network if none is named. state_: on, off or connect. network: network name if said."
    if not claim("wifi"):
        return {"skipped": state_}
    q = f"{state_} {network} {state['query']}".lower()
    if turning_off(q):
        return {"say": "I couldn't turn Wi-Fi off" if failed(sh("nmcli", "radio", "wifi", "off")) else "Wi-Fi off"}
    if said("restart", q):
        sh("nmcli", "radio", "wifi", "off")
        return {"say": "Wi-Fi restarted" if wifi_radio_on() else "Wi-Fi is blocked. Check airplane mode."}
    if not wifi_radio_on():
        return {"say": "Wi-Fi is blocked. Check airplane mode or the wireless switch."}
    named = [w for w in words(state["query"]) if w not in STOPWORDS
             and w not in ("on", "off", "enable", "activate", "now", "up", "am", "connected")]
    if not named and not re.search(r"\b(connect|best|fastest|strongest|speed|internet|join)\b", q):
        return {"say": "Wi-Fi on"}
    aps = []
    for _ in range(3):                                  # scans can come back empty right after enabling
        aps, saved = wifi_aps()
        if aps:
            break
        time.sleep(2)
    if named:                                           # "connect to oppo's wifi"
        # Saved networks out of range compete too, so "airtel 5g" doesn't join another network that has "5G".
        far = [{"ssid": n, "far": True} for n in saved or [] if not any(is_saved(a["ssid"], [n]) for a in aps)]
        join = lambda a: f"{a['ssid']} isn't in range right now. Is it switched on?" if a.get("far") else wifi_join(a)
        ap, ties = best_match(state["query"], aps + far, lambda a: [a["ssid"]])
        if ap:
            return {"say": join(ap)}
        if ties:
            return {"say": ask_choice(ties, lambda a: a["ssid"], join, "join")}
        nearby = one_of(a["ssid"] for a in aps[:5]) if aps else "nothing"
        return {"say": f"I can't find a network called {' '.join(named)}. Nearby I see {nearby}."}
    if saved is None:
        return {"say": "I couldn't read your saved Wi-Fi networks"}
    known = [a for a in aps if a["saved"]]              # no name: strongest saved network
    if not known:
        return {"say": "I can't see any saved Wi-Fi network nearby"}
    current = next((a for a in aps if a["in_use"]), None)
    if current and current["saved"] and known[0]["signal"] - current["current_signal"] < MIN_WIFI_GAIN:
        return {"say": f"You're already on the best network, {current['ssid']}, "
                       f"signal {current['current_signal']} percent"}
    for ap in known[:3]:                                # fall back to the next best if one fails
        reply = wifi_join(ap)
        if reply.startswith("Connected"):
            return {"say": reply}
    return {"say": "I couldn't connect to any nearby saved network"}


# ---------- bluetooth ----------

def bt_devices():
    out = sh("bluetoothctl", "devices", "Paired")
    return [tuple(l.split(" ", 2)[1:]) for l in out.splitlines() if l.startswith("Device ") and l.count(" ") >= 2]


def pick_device(q, devices):
    """(device, None) or (None, question) when nothing or several devices match."""
    if not devices:
        return None, "I couldn't find any paired Bluetooth devices. If none are paired, pair one in Settings first."
    dev, ties = best_match(q, devices, lambda d: [d[1]])
    if dev:
        return dev, None
    return None, f"Which one, {one_of(d[1] for d in (ties or devices))}?"


def bt_power_on():
    """Turn Bluetooth on, unblocking it first if needed. After an unblock the adapter takes a few seconds
    to reappear ("No default controller available"), so keep retrying for up to 10 s."""
    for attempt in range(10):
        if "succeeded" in sh("bluetoothctl", "power", "on"):
            return True
        if attempt == 0:
            sh("rfkill", "unblock", "bluetooth")       # soft-blocked by the Bluetooth toggle / airplane mode
        time.sleep(1)
    return False


def bt_connect(device):
    mac, name = device
    if not bt_power_on():
        return "I couldn't turn Bluetooth on. Check airplane mode."
    if "Connected: yes" in sh("bluetoothctl", "info", mac):
        return f"{name} is already connected"
    for attempt in range(2):
        if "Connection successful" in sh("bluetoothctl", "connect", mac, timeout=25):
            return f"Connected to {name}"
        time.sleep(2)
    return f"I couldn't connect to {name}. Is it switched on and nearby?"


@cactus.tool
def bluetooth(action: str = "", device: str = ""):
    "Control Bluetooth. action: on, off, connect or disconnect. device: name of headphones or earbuds to connect."
    if not claim("bluetooth"):
        return {"skipped": action}
    q = f"{action} {device} {state['query']}".lower()
    if re.search(r"\b(off|shut|disable)\b", q) and not said("disconnect", q):
        return {"say": "I couldn't turn Bluetooth off" if failed(sh("bluetoothctl", "power", "off")) else "Bluetooth off"}
    if not said("disconnect", q) and not bt_power_on():  # also needed to list devices: no adapter, no list
        return {"say": "I couldn't turn Bluetooth on. Check airplane mode."}
    devices = bt_devices()
    if said("disconnect", q):
        def disconnect(d):
            out = sh("bluetoothctl", "disconnect", *([d[0]] if d else []))
            if "no default controller" in out.lower():
                return "Bluetooth is off, so nothing is connected"
            return f"I couldn't disconnect {d[1] if d else 'Bluetooth'}" if failed(out) else \
                f"Disconnected {d[1] if d else 'Bluetooth devices'}"
        named = [w for w in words(state["query"]) if w not in STOPWORDS and w not in ("all", "everything", "devices")]
        if not named:                                   # "disconnect bluetooth": everything
            return {"say": disconnect(None)}
        if not devices:
            return {"say": "I couldn't read the paired Bluetooth devices, so I disconnected nothing"}
        dev, ties = best_match(state["query"], devices, lambda d: [d[1]])
        if dev:
            return {"say": disconnect(dev)}
        if ties:
            return {"say": ask_choice(ties, lambda d: d[1], disconnect, "disconnect")}
        return {"say": f"I don't know a Bluetooth device called {' '.join(named)}"}
    wants_device = said("connect", q) or best_match(q, devices, lambda d: [d[1]])[1] or \
        any(said(w, q) for w in ("headphone", "headphones", "earbuds", "buds", "neckband", "headset", "speaker"))
    if not wants_device:
        return {"say": "Bluetooth on"}
    dev, question = pick_device(q, devices)
    if question:
        if devices:
            ties = best_match(q, devices, lambda d: [d[1]])[1]
            return {"say": ask_choice(ties or devices, lambda d: d[1], bt_connect, "connect")}
        return {"say": question}
    return {"say": bt_connect(dev)}


# ---------- notifications / power ----------

@cactus.tool
def do_not_disturb(state_: str = ""):
    "Turn Do Not Disturb (hide notifications) on or off. state_: on or off."
    if not claim("dnd"):
        return {"skipped": state_}
    q = f"{state_} {state['query']}".lower()
    off = turning_off(q)
    quiet = (not off) if "disturb" in q else off       # "DND on" = quiet; "notifications off" = quiet
    sh("gsettings", "set", "org.gnome.desktop.notifications", "show-banners", "false" if quiet else "true")
    return {"say": "Do not disturb on" if quiet else "Notifications on"}


@cactus.tool
def power(action: str = ""):
    "Sleep, restart or shut down the computer. action: sleep, restart or shutdown. Asks for confirmation."
    if not claim("power"):
        return {"skipped": action}
    q = f"{action} {state['query']}".lower()
    if re.search(r"\b(restart|reboot)\b", q):
        cmd, what = ["systemctl", "reboot"], "restart the computer"
    elif re.search(r"\b(shut ?down|power off|turn off|switch off)\b", q):
        cmd, what = ["systemctl", "poweroff"], "shut down the computer"
    else:
        cmd, what = ["systemctl", "suspend"], "put the computer to sleep"

    def confirm(text):
        verdict = yes_no(text)
        if verdict == "yes":
            launch(cmd)
            return f"Okay, I'll {what}"
        return "Cancelled" if verdict == "no" else None  # anything else: a new command
    ask_follow_up(confirm)
    return {"say": f"Should I {what}?"}


# ---------- keyboard and mouse by voice ----------

KEYSYMS = {"enter": 0xFF0D, "return": 0xFF0D, "tab": 0xFF09, "escape": 0xFF1B, "backspace": 0xFF08,
           "delete": 0xFFFF, "space": 0x20, "left": 0xFF51, "up": 0xFF52, "right": 0xFF53, "down": 0xFF54,
           "home": 0xFF50, "end": 0xFF57, "page up": 0xFF55, "page down": 0xFF56, "plus": 0x2B, "minus": 0x2D,
           "control": 0xFFE3, "ctrl": 0xFFE3, "shift": 0xFFE1, "alt": 0xFFE9, "super": 0xFFEB, "windows": 0xFFEB,
           **{f"f{i}": 0xFFBD + i for i in range(1, 13)}}
# Keys that work when said alone ("enter", "down 3 times").
BARE_KEYS = ("enter", "tab", "escape", "backspace", "delete", "space", "page up", "page down",
             "up", "down", "left", "right", "home", "end")
SHORTCUTS = {"copy": "control c", "paste": "control v", "cut": "control x", "undo": "control z",
             "redo": "control shift z", "select all": "control a", "save": "control s",
             "new tab": "control t", "close tab": "control w", "next tab": "control tab",
             "previous tab": "control shift tab", "reopen tab": "control shift t", "new window": "control n",
             "address bar": "control l", "previous page": "alt left", "next page": "alt right",
             "refresh": "f5", "reload": "f5", "full screen": "f11", "zoom in": "control plus",
             "zoom out": "control minus", "switch window": "alt tab", "close window": "alt f4",
             "maximize": "super up", "minimize": "super h", "show apps": "super"}
NUM = r"\d+"                    # key_command turns number words into digits first


def to_int(n, default):
    return int(n) if n else default


def key_command(text):
    """Parse a keyboard/mouse command (it must start the sentence), or None. Returns ("type", text),
    ("keys", keysyms, times, label), ("click", button, times), ("scroll", steps), ("move", dx, dy)
    or ("unknown", what)."""
    t = re.sub(r"^(?:please|can you|could you)\s+", "", text.strip().rstrip(".!?"), flags=re.I)
    m = re.match(r"(?:type|dictate)\s+(.+)", t, re.I | re.S)
    if m:
        return ("type", m.group(1))
    low = spoken_numbers(t.lower())
    times = 1
    m = re.search(rf"\s+(?:({NUM})\s+times|(twice))$", low)
    if m:
        times, low = min(2 if m.group(2) else to_int(m.group(1), 1), 20), low[:m.start()]
    m = re.fullmatch(r"(double|right|left)?\s*click", low)
    if m:
        return ("click", 273 if m.group(1) == "right" else 272, 2 if m.group(1) == "double" else times)
    m = re.fullmatch(rf"scroll (up|down)(?: (?:by )?({NUM}))?", low)
    if m:
        return ("scroll", to_int(m.group(2), 3) * (-1 if m.group(1) == "up" else 1))
    m = re.fullmatch(rf"(?:move )?(?:the )?(?:mouse|cursor|pointer) (left|right|up|down)(?: (?:by )?({NUM}))?(?: pixels)?", low)
    if m:
        d = to_int(m.group(2), 100)
        return ("move",) + {"left": (-d, 0), "right": (d, 0), "up": (0, -d), "down": (0, d)}[m.group(1)]
    low = re.sub(r"^(close|new|next|previous|reopen) (?:this|the|current|a) (tab|window)$", r"\1 \2", low)
    combo = SHORTCUTS.get(low) or (low if low in BARE_KEYS else None)
    m = re.fullmatch(r"press\s+(?:the\s+)?(.+?)(?:\s+key)?", low)
    if m:
        combo = SHORTCUTS.get(m.group(1), m.group(1))
    if not combo:
        return None
    tokens = re.findall(r"page up|page down|[a-z0-9]+", combo.replace("+", " "))
    syms = [KEYSYMS.get(k) or (ord(k) if len(k) == 1 else None) for k in tokens]
    if not syms or None in syms:
        return ("unknown", combo)
    return ("keys", syms, times, combo)


def inject(action, *args):
    """Every keyboard/mouse event goes through here, so tests can fake it."""
    getattr(control, action)(*args)


def keys():
    "Type text, press keys or shortcuts, click, scroll or move the mouse."
    cmd = key_command(state["query"])
    try:
        if cmd[0] == "type":
            inject("type_text", cmd[1])
            return {"say": "Typed"}
        if cmd[0] == "keys":
            for _ in range(cmd[2]):
                inject("tap", cmd[1])
            return {"say": f"Pressed {cmd[3]}" + (f" {cmd[2]} times" if cmd[2] > 1 else "")}
        if cmd[0] == "click":
            inject("click", cmd[1], cmd[2])
            return {"say": "Clicked"}
        if cmd[0] == "scroll":
            inject("scroll", cmd[1])
            return {"say": "Scrolled " + ("down" if cmd[1] > 0 else "up")}
        if cmd[0] == "move":
            inject("move", cmd[1], cmd[2])
            return {"say": "Moved"}
    except control.NoPermission:
        return {"say": "I need permission to control the keyboard and mouse. "
                       "Click Share in the dialog, then try again."}
    return {"say": f"I don't know the key {cmd[1]}"}


# ---------- closing apps ----------

def unit_of(pid):
    """The systemd unit GNOME runs this process in, e.g. 'snap.brave.brave-<uuid>.scope', and its slice."""
    try:
        parts = read_file(f"/proc/{pid}/cgroup").strip().split("/")
    except OSError:
        return "", ""
    return parts[-1], (parts[-2] if len(parts) > 1 else "")


def running_apps():
    """[(app, how_to_close)] for open apps. how_to_close is ('gtk', bus_name) for GNOME apps (they quit
    themselves and can ask to save) or ('unit', unit) for an app's own unit under app.slice. Never this
    assistant's own window, and never tray tools that start at login."""
    own = unit_of(os.getpid())[0]
    autostart = {f[:-8] for d in ("/etc/xdg/autostart", "~/.config/autostart")
                 if os.path.isdir(os.path.expanduser(d)) for f in os.listdir(os.path.expanduser(d))}
    apps = installed_apps()
    found = {}
    live = {}                                            # D-Bus names with a running process
    for line in sh("busctl", "--user", "list", "--no-legend").splitlines():
        p = line.split()
        if len(p) > 1 and p[1].isdigit():
            live[p[0]] = p[1]
    for a in apps:
        pid = live.get(a["id"])
        if pid and unit_of(pid)[0] != own and "quit" in sh("gdbus", "call", "--session", "--dest", a["id"],
                "--object-path", "/" + a["id"].replace(".", "/"), "--method", "org.gtk.Actions.List"):
            found[a["id"]] = (a, ("gtk", a["id"]))
    units = {}
    for pid in filter(str.isdigit, os.listdir("/proc")):
        unit, slc = unit_of(pid)
        if slc == "app.slice" and unit.endswith(".scope") and unit != own:   # app windows, never services
            try:
                units.setdefault(unit, set()).add(read_file(f"/proc/{pid}/comm").strip())
            except OSError:
                pass
    for unit, comms in units.items():
        uid = re.sub(r"^(app-gnome-|app-flatpak-|app-|snap\.)", "", unit.replace("\\x2d", "-"))
        uid = re.sub(r"(-[0-9a-f]{8}-[0-9a-f-]{27}|-\d+)?(@\d*)?\.(scope|service)$", "", uid)
        if uid in autostart:
            continue
        uw = set(words(uid)) - {"com", "org", "io", "app"}

        def same_id(a):
            aw = set(words(a["id"])) - {"com", "org", "io", "app"}
            return uw and aw and (uw <= aw or aw <= uw)

        def same_exe(a):
            return next((os.path.basename(t) for t in shlex.split(a["exec"] or "x")
                         if t != "env" and "=" not in t), "") in comms
        app = next((a for a in apps if same_id(a)), None) or next((a for a in apps if same_exe(a)), None)
        if app and app["id"] not in found:
            found[app["id"]] = (app, ("unit", unit))
    return list(found.values())


def close_app(how):
    """Ask the app to quit; returns the command output."""
    kind, target = how
    if kind == "gtk":
        return sh("gdbus", "call", "--session", "--dest", target, "--object-path", "/" + target.replace(".", "/"),
           "--method", "org.gtk.Actions.Activate", "quit", "[]", "{}")
    else:                                                # same signal as clicking close; apps save their state
        return sh("systemctl", "--user", "kill", "--signal=SIGTERM", target)


def app_names(r):
    return [(r[0]["name"], 1.0), (r[0]["generic"], 0.8)]


def close_all(targets):
    stuck = [a["name"] for a, how in targets if failed(close_app(how))]
    closed = [a["name"] for a, _ in targets if a["name"] not in stuck]
    reply = f"Closed {one_of(closed, 'and')}" if closed else ""
    if stuck:
        reply += (". " if reply else "") + f"I couldn't close {one_of(stuck, 'and')}"
    return reply


def confirm_close(targets, note=""):
    """'I'll close A, B and C. Okay?' The answer can be yes, no, or 'keep B' to change the list."""
    def answer(text):
        t = text.lower()
        m = re.search(r"\b(?:keep|leave|except|not|don't close)\s+(.+)", t)
        if m:
            keep = [r for r in targets if best_match(m.group(1), [r], app_names)[0]]
            if keep:
                rest = [r for r in targets if r not in keep]
                return confirm_close(rest) if rest else "Okay, nothing closed"
        verdict = yes_no(text)
        if verdict == "yes" or re.fullmatch(r"all(?: of them)?", t.strip(" .!?")):
            return close_all(targets)
        return "Okay, nothing closed" if verdict == "no" else None
    ask_follow_up(answer)
    return f"{note}I'll close {one_of([a['name'] for a, _ in targets], 'and')}. Okay?"


@cactus.tool
def close(name: str = ""):
    "Close running apps. name: the app to close, or 'all', or 'all except terminal'."
    if not claim("close"):
        return {"skipped": name}
    q = state["query"].lower()
    running = running_apps()
    if not running:
        return {"say": "Nothing else is open"}
    note = "I can't close single tabs, only whole apps. " if re.search(r"\btabs?\b", q) else ""
    keep_m = re.search(r"\b(?:except|but(?: not)?|other than|apart from|keep(?: only)?|leave)\s+(.+)", q)
    target = re.sub(r"\b(close|quit|kill)\b", " ", q[:keep_m.start()] if keep_m else q)
    target = re.sub(r"\s+and\s*$", "", target)       # "close chrome and leave the rest"
    names = [p for p in re.split(r"\s*(?:,|\band\b)\s*", target) if [w for w in words(p) if w not in STOPWORDS]]
    everything = re.search(r"\b(all|everything|every|unwanted|others?|rest|tabs?|windows?)\b", target)
    if everything or (keep_m and not names):            # named apps before "but keep ..." are the targets
        keep = [r for r in running if keep_m and best_match(keep_m.group(1), [r], app_names)[0]]
        targets = [r for r in running if r not in keep]
        if not targets:
            return {"say": "Nothing else to close"}
        return {"say": confirm_close(targets, note)}
    if not names:                                                      # just "close"
        return {"say": ask_choice(running, lambda r: r[0]["name"], lambda r: close_all([r]), "close")}
    if len(names) > 1:                                                 # "close chrome and brave"
        found = [best_match(n, running, app_names)[0] for n in names]
        if all(found):
            return {"say": close_all([r for i, r in enumerate(found) if r not in found[:i]])}
        missing = [n.strip() for n, r in zip(names, found) if not r]
        return {"say": f"I can't tell which open app you mean by {one_of(missing, 'and')}, so I closed nothing. "
                       f"Open now: {one_of([a['name'] for a, _ in running[:5]], 'and')}"}
    app, ties = best_match(target, running, app_names)
    if app:
        return {"say": close_all([app])}
    if ties:
        return {"say": ask_choice(ties, lambda r: r[0]["name"], lambda r: close_all([r]), "close")}
    installed, _ = find_app(target)
    if installed:
        return {"say": f"{installed['name']} isn't open"}
    return {"say": f"I can't find an open app called {target.strip()}. "
                   f"Open now: {one_of([a['name'] for a, _ in running[:5]], 'and')}"}


TOOLS = [open, search, volume, timer, lock_screen, tell, calculate,
         bluetooth, wifi, brightness, media, do_not_disturb, power, log_video, close]
# Commands that clearly match only one of these skip Needle entirely (instant, no model call).
DIRECT = {"volume": volume, "timer": timer, "lock": lock_screen, "info": tell, "math": calculate,
          "bluetooth": bluetooth, "wifi": wifi, "brightness": brightness, "media": media,
          "dnd": do_not_disturb, "power": power, "video_log": log_video, "close": close, "keys": keys}


# ---------- LLM (questions, chat, writing) ----------

history = []
session_context = []                    # recent local actions, kept in RAM for follow-up questions


def remember(user, reply):
    session_context.append(f"User: {user}\nNeedle: {reply}")
    del session_context[:-10]


def ask_llm(text):
    if not LLM_MODEL:
        return ("Sorry, I can't answer that offline. I can open apps and sites, search, control volume, "
                "brightness, Wi-Fi and Bluetooth, set timers, and tell the time or battery.")
    now = datetime.datetime.now().strftime("%A, %d %B %Y, %I:%M %p")
    system = (f"You are Needle, a helpful offline voice assistant on the user's Linux laptop. "
              f"Current time: {now}. Battery: {battery()}. You have no internet access. "
              f"Recent actions and conversation:\n" + ("\n".join(session_context) or "None") + "\n"
              f"Resolve follow-ups from that context. Reply in 1-3 short spoken-style sentences unless asked for more. "
              f"No markdown, no emoji. Never claim you searched the web.")
    history.append({"role": "user", "content": text})
    body = {"model": LLM_MODEL, "stream": True, "keep_alive": "5m",
            "options": {"temperature": 0, "num_ctx": 2048, "num_predict": 120},   # short spoken answers
            "messages": [{"role": "system", "content": system}] + history[-12:]}
    req = urllib.request.Request(OLLAMA, json.dumps(body).encode(), {"Content-Type": "application/json"})
    reply, pending = "", ""
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            for line in r:                         # one JSON object per generated piece
                piece = json.loads(line).get("message", {}).get("content", "")
                reply, pending = reply + piece, pending + piece
                # Speak each finished sentence at once, so the answer starts in under a second
                # instead of after the whole reply (~3 s on this CPU).
                done = re.match(r"(.+?[.!?])(\s+|$)(.*)", pending, re.S)
                if on_sentence and done and done.group(2):
                    on_sentence(done.group(1).strip())
                    state["spoken"] += done.group(1).strip() + " "
                    pending = done.group(3)
    except Exception:
        history.pop()
        return "My language model isn't running. Start Ollama to get answers to questions."
    reply = reply.strip()
    if on_sentence and pending.strip():
        on_sentence(pending.strip())
        state["spoken"] += pending.strip()
    history.append({"role": "assistant", "content": reply or "Sorry, I don't know."})
    del history[:-24]
    return reply or "Sorry, I don't know."


# ---------- answers from the web (no account or key) ----------

def fetch_json(url):
    """GET a JSON document; None when offline or not found. Every web lookup goes through here (tests fake it)."""
    req = urllib.request.Request(url, headers={"User-Agent": "NeedleAssistant/1.0 (personal voice assistant)"})
    try:
        with urllib.request.urlopen(req, timeout=6) as r:
            return json.load(r)
    except (OSError, ValueError):
        return None


def short(text, sentences=2):
    """First sentences only, without bracketed asides, so the spoken answer stays brief."""
    text = re.sub(r"\s*\([^)]*\)", "", text).strip()
    return " ".join(re.split(r"(?<=[.!?])\s+", text)[:sentences])


def web_answer(question):
    """A short answer to a factual question, or None: weather (wttr.in), DuckDuckGo instant answers, Wikipedia."""
    q = question.strip(" ?.!")
    m = re.search(r"\bweather\b(?:.*?\b(?:in|at|for)\s+(.+))?", q, re.I)
    if m:
        place = re.sub(r"\b(today|right now|now|tomorrow|please)\b", "", m.group(1) or "", flags=re.I).strip()
        w = fetch_json(f"https://wttr.in/{urllib.parse.quote(place)}?format=j1")
        try:
            now, area = w["current_condition"][0], w["nearest_area"][0]["areaName"][0]["value"]
            return (f"{now['weatherDesc'][0]['value'].strip()} and {now['temp_C']} degrees in {area}, "
                    f"feels like {now['FeelsLikeC']}")
        except (TypeError, KeyError, IndexError):
            return None
    d = fetch_json("https://api.duckduckgo.com/?format=json&no_html=1&skip_disambig=1&q="
                   + urllib.parse.quote_plus(q)) or {}
    text = d.get("Answer") or d.get("AbstractText") or d.get("Definition")
    if isinstance(text, str) and text:
        return short(text)
    # Wikipedia's summary only answers "what/who is X"; for "how tall is X" it would read out the wrong thing.
    m = re.match(r"(?:(?:who|what)(?:'s| is| are| was| were)|tell me about|define|explain)\s+(.+)", q, re.I)
    if not m:
        return None
    topic = m.group(1)
    found = fetch_json("https://en.wikipedia.org/w/api.php?action=query&list=search&srlimit=1&format=json&srsearch="
                       + urllib.parse.quote_plus(topic))
    try:
        title = found["query"]["search"][0]["title"]
    except (TypeError, KeyError, IndexError):
        return None
    page = fetch_json("https://en.wikipedia.org/api/rest_v1/page/summary/"
                      + urllib.parse.quote(title.replace(" ", "_"), safe=""))
    if page and page.get("type") == "standard" and page.get("extract"):
        return f"According to Wikipedia's article on {title}: {short(page['extract'])}"
    return None


def answer(text):
    """Answer locally. Web search is an action and only runs for "search/google/look up ..." commands."""
    # Unmatched machine-changing requests still go to the guarded local-action planner. It can inspect the
    # laptop, but every proposed change requires confirmation. Questions and chat stay entirely in the local LLM.
    if re.match(r"\s*(?:rename|delete|remove|move|copy|create|make|install|uninstall|update|download|organize|clean|"
                r"fix|edit|write|save|kill|stop|enable|disable|configure|wipe|restart|reboot|shut)\b", text, re.I):
        return ask_agent(text) or "I can't do that locally yet"
    if len(words(text)) <= 2 and not text.rstrip().endswith("?") and not re.match(
            r"\s*(?:who|what|when|where|which|why|how|thanks?|thank you|hi|hello|bye|good|yes|yeah|no|nope|ok|okay|sure)\b",
            text, re.I):
        ask_follow_up(lambda again: None)    # a misheard command: let the user just say it again (no wake word)
        return "Sorry, I didn't catch that. Say it again?"
    return ask_llm(text)


# ---------- "do anything": a brain proposes shell commands, you approve, Needle runs them ----------

AGENT_MODEL = "haiku"
AGENT_PROMPT = ('You are the brain of Needle, a hands-free voice assistant on the user\'s Ubuntu laptop (GNOME, Wayland). '
                'The request was spoken and transcribed, so it may contain mishearings. You may read files and run '
                'read-only commands to investigate, but you cannot change anything yourself. Reply with ONE line of '
                'JSON and nothing else. Use {"say": "<answer in at most two short spoken sentences, no markdown>"} '
                'when you can answer by looking or from knowledge. When the request needs any change to the machine '
                '(files, settings, installs, starting or stopping programs), use {"plan": "<one short spoken sentence '
                'saying exactly what will happen>", "commands": ["<bash command>", ...]}. The commands are joined into ONE '
                'bash script (set -e, so it stops at the first failure; a cd carries over to later commands) that runs '
                'as the normal user, starting in the home directory, only after the user says yes. They must be '
                'complete, non-interactive, and must not use sudo (no password can be typed). If it cannot be done '
                'without sudo, or the request is unclear, use {"say": "<short explanation or clarifying question>"}. '
                'Never try to make a change yourself: your tools are read-only and the attempt will be blocked. Your '
                'entire reply must be that single JSON line: no prose before or after, no markdown, no code fence.')
# Never run these, even after a yes: a misheard "yes" must not be able to wipe the home folder, stop the
# computer or ask for a password. See script_risk().
HOME = os.path.expanduser("~")
PRIVILEGE = {"sudo", "su", "pkexec", "doas"}
POWER_COMMANDS = {"halt", "poweroff", "reboot", "shutdown"}
SHELLS = {"sh", "bash", "zsh", "dash"}
# Words that run the next word as the command: "env X=1 rm ...", "nice -n 5 rm ...", "if ...; then rm ...".
WRAPPERS = {"env", "command", "builtin", "exec", "nohup", "time", "nice", "ionice", "setsid", "stdbuf", "timeout",
            "xargs", "{", "}", "!", "if", "then", "else", "elif", "do", "while", "until"}


def resolve(path, cwd):
    """Absolute path of a command argument, or None when it can't be known ($VAR, $(...), `...`, ~user)."""
    p = re.sub(r"^(?:\$HOME|\$\{HOME\})(?=/|$)", HOME, path)
    if p == "~" or p.startswith("~/"):
        p = HOME + p[1:]
    if "$" in p or "`" in p or p.startswith("~"):
        return None
    if not p.startswith("/"):
        if cwd is None:
            return None
        p = os.path.join(cwd, p)
    return os.path.normpath(p)


def wipes(target, cwd):
    """True if deleting or overwriting this argument could take out the home folder or the system."""
    if target in (".", "..", "./", "../", "*", ".*", "./*", "../*"):
        return True
    path = resolve(target, cwd)
    if path is None:
        return True
    near_root = lambda p: p == "/" or p == HOME or HOME.startswith(p.rstrip("/") + "/")   # an ancestor of home
    head, base = os.path.split(path)
    return near_root(path) or (base in ("*", ".*") and near_root(head))


def destructive_targets(name, args, cwd):
    """What a deleting/overwriting command acts on, or None if the command isn't one of those."""
    plain = [a for a in args if not a.startswith("-")]
    here = cwd or "$UNKNOWN"
    if name in ("rm", "rmdir", "shred", "truncate", "unlink"):
        return plain
    if name == "mv":
        return plain[:-1]
    if name in ("chmod", "chown", "chgrp") and ("-R" in args or "--recursive" in args):
        return plain[1:]
    if name == "find" and ("-delete" in args or any(
            a in ("-exec", "-execdir", "-ok") and os.path.basename((args[i + 1:] or [""])[0]) in ("rm", "shred", "unlink", "mv")
            for i, a in enumerate(args))):
        starts = []
        for a in args:
            if a.startswith(("-", "(", "!")):
                break
            starts.append(a)
        return starts or [here]
    if name == "git" and args[:1] == ["clean"]:
        return plain[1:] or [here]
    if name == "git" and args[:1] == ["reset"] and "--hard" in args:
        return [here]
    if name == "rsync" and any(a.startswith("--delete") for a in args):
        return plain[-1:]
    return None


def script_risk(script, cwd=HOME, depth=0):
    """'refuse', 'careful' (it deletes or overwrites files) or 'ok' for the brain's approved script.
    This is a safety net against obvious disasters after a misheard yes, NOT a guarantee: bash can hide what it
    does in ways no check like this can follow (python -c, a downloaded script, an alias, a variable set by
    another program). The spoken plan and the yes remain the real control."""
    if depth > 3 or re.search(r":\(\)\s*\{", script):    # too deeply nested to follow; fork bomb
        return "refuse"
    try:
        lex = shlex.shlex(script.replace("\n", " ; "), posix=True, punctuation_chars=True)
        lex.whitespace_split = True
        tokens = list(lex)
    except ValueError:                                     # e.g. unbalanced quotes: can't tell what would run
        return "refuse"
    commands, cmd, redirects, piped = [], [], [], False   # simple commands: (words, redirects, fed by a pipe)
    tokens = iter(tokens)
    for tok in tokens:
        if tok and all(c in "();<>|&" for c in tok):
            if "<" in tok or ">" in tok:
                redirects.append((tok, next(tokens, "")))
                continue
            commands.append((cmd, redirects, piped))
            cmd, redirects, piped = [], [], tok in ("|", "|&")
        else:
            cmd.append(tok)
    commands.append((cmd, redirects, piped))
    risk = "ok"
    for cmd, redirects, piped in commands:
        if any(t in PRIVILEGE or t in POWER_COMMANDS or os.path.basename(t).startswith("mkfs") for t in cmd):
            return "refuse"
        for op, target in redirects:
            if re.match(r"/dev/(sd|nvme|mmcblk|hd|vd)", target):
                return "refuse"
            if op in (">", ">|", "&>") and target != "/dev/null" and not target.isdigit():
                path = resolve(target, cwd)
                if path and path.startswith(HOME + "/."):
                    return "refuse"                        # "> ~/.bashrc" empties a config file
                risk = "careful"
        wrapped = False
        while cmd and (cmd[0] in WRAPPERS or re.fullmatch(r"\w+=.*", cmd[0])):
            wrapped = wrapped or cmd[0] == "xargs"
            cmd = cmd[1:]
            while cmd and (cmd[0].startswith("-") or re.fullmatch(r"[\d.]+[smhd]?", cmd[0])):
                cmd = cmd[1:]                              # the wrapper's own options: "nice -n 10", "timeout 5"
        if not cmd:
            continue
        name, args = os.path.basename(cmd[0]), cmd[1:]
        if name in SHELLS and piped:
            return "refuse"                                # "curl ... | bash"
        if name == "eval" or (name in SHELLS and "-c" in args):
            inner = " ".join(args) if name == "eval" else " ".join(args[args.index("-c") + 1:][:1])
            inner_risk = script_risk(inner, cwd, depth + 1)
            if inner_risk == "refuse":
                return "refuse"
            risk = "careful" if inner_risk == "careful" else risk
            continue
        if name == "cd":
            cwd = HOME if not args else None if args[0] == "-" else resolve(args[0], cwd)
            continue
        needle = lambda: any(re.search(r"needle|voice_assistant", a) for a in args)
        if (name == "dd" and any(a.startswith("of=/dev/") for a in args)) or name == "killall5" \
                or (name == "init" and set(args) & {"0", "6"}) or (name == "kill" and "-1" in args) \
                or (name in ("pkill", "killall") and ("-u" in args or needle())) \
                or (name == "loginctl" and any(re.match(r"(terminate|kill)-", a) for a in args)) \
                or (name == "systemctl" and set(args) & {"stop", "disable", "mask", "kill"} and needle()) \
                or (name == "crontab" and "-r" in args):
            return "refuse"
        targets = destructive_targets(name, args, cwd)
        if targets is not None:
            if wrapped or any(wipes(t, cwd) for t in targets) or \
                    (name in ("shred", "truncate") and any(c in t for t in targets for c in "*?")):
                return "refuse"                            # xargs hides its targets; wildcard shreds are unrecoverable
            risk = "careful"
    return risk

# What the brain may use by itself while investigating: reading only. Anything else is blocked by its
# permission system, so the only way to change the machine is the approved commands below.
AGENT_READ_ONLY = ["Read", "Glob", "Grep"] + [f"Bash({c})" for c in (
    "ls *", "cat *", "head *", "tail *", "wc *", "du *", "df *", "free *", "stat *", "file *", "which *",
    "ps *", "uptime", "lscpu", "lsblk *", "uname *", "date", "whoami", "id", "pwd", "sort *", "uniq *",
    "dpkg -l *", "apt list *", "snap list *", "flatpak list *", "pip list *", "systemctl status *",
    "systemctl --user status *", "systemctl --user list-units *", "ip addr *", "nmcli device *",
    "nmcli connection show *", "git status *", "git log *", "git diff *")]
on_wait = lambda text: None          # the voice loop sets this to speak "One moment" while the brain works
on_sentence = None                   # the voice loop sets this to speak an answer sentence by sentence


def agent_binary():
    """Claude Code's command-line program: the newest one bundled with the VS Code extension, else on PATH."""
    found = glob.glob(os.path.expanduser("~/.vscode/extensions/anthropic.claude-code-*/resources/native-binary/claude"))
    return max(found, key=os.path.getmtime) if found else shutil.which("claude")


def run_agent(request):
    """Ask the brain (Claude Code, look-only: its permission system blocks every write) and return its final
    text; None if it isn't installed or failed. Tests replace this."""
    binary = agent_binary()
    if not binary:
        return None
    cmd = [binary, "-p", request, "--output-format", "json", "--no-session-persistence", "--strict-mcp-config",
           "--model", AGENT_MODEL, "--system-prompt", AGENT_PROMPT, "--permission-mode", "manual",
           "--setting-sources", "local",        # skip personal plugins/hooks: measured 8/8 correct vs 5/8 with them
           "--allowedTools", *AGENT_READ_ONLY]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=180, cwd=os.path.expanduser("~"),
                             env=clean_env()).stdout
        data = json.loads(out.strip().splitlines()[-1])
    except (OSError, ValueError, IndexError, subprocess.TimeoutExpired):
        return None
    return None if data.get("is_error") else data.get("result")


def run_shell(script):
    """Run the approved script in one bash from the home folder, in its own session. Output goes to a temporary
    file rather than a pipe, so a program it leaves running can't keep us waiting. (ok, last line of output).
    Tests replace this."""
    with tempfile.TemporaryFile("w+") as out:
        p = subprocess.Popen(["bash", "-c", script], stdout=out, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                             cwd=HOME, env=clean_env(), start_new_session=True)
        try:
            code = p.wait(timeout=120)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(p.pid, signal.SIGKILL)               # the whole session, not just bash
            except ProcessLookupError:
                pass
            p.wait()
            return False, "it took longer than two minutes"
        out.seek(0)
        lines = out.read().strip().splitlines()
    return code == 0, (lines[-1] if lines else "")


def ask_agent(text):
    """Hand over a request no built-in command covers. Answers come straight back; a change is described,
    its commands are shown (state["detail"]), and it only runs after a spoken yes. None if there is no brain."""
    on_wait("One moment")
    raw = run_agent(text)
    if raw is None:
        return None
    unreadable = "Sorry, I got an answer I couldn't read, so I did nothing"
    m = re.search(r"\{.*\}", raw, re.S)
    if not m:                                                      # prose instead of JSON: speak it briefly
        return short(re.sub(r"[*`#_]|\n+", " ", raw)).strip() or unreadable
    try:
        reply = json.loads(m.group())
    except ValueError:
        reply = None
    if not isinstance(reply, dict):
        return unreadable
    plan, commands = reply.get("plan"), reply.get("commands")
    if plan is None and commands is None:                          # an answer
        say = reply.get("say")
        return say.strip() if isinstance(say, str) and say.strip() else unreadable
    if not (isinstance(plan, str) and plan.strip() and isinstance(commands, list) and commands
            and all(isinstance(c, str) and c.strip() for c in commands)):
        return "Sorry, the plan I got back was incomplete, so I did nothing"
    plan = plan.strip().rstrip(". ")
    script = "set -e\n" + "\n".join(commands)
    risk = script_risk(script)
    if risk == "refuse":
        return f"I won't do that by voice, it is too risky or needs the admin password: {plan}."
    state["detail"] = "\n".join(commands)

    def confirm(answer_):
        verdict = yes_no(answer_)
        if verdict == "yes":
            on_wait("Working on it")
            ok, last = run_shell(script)
            return "Done" if ok else f"That failed: {last or 'the command returned an error'}"
        return "Okay, cancelled" if verdict == "no" else None
    ask_follow_up(confirm)
    warning = "Careful, this deletes or overwrites files. " if risk == "careful" else ""
    return f"{warning}{plan}. Should I do it?"


# ---------- router ----------

VERB_START = (r"\s*(?:please\s+)?(?:turn|switch|set|increase|decrease|raise|lower|reduce|enable|disable|open|launch|start|"
              r"close|quit|kill|connect|disconnect|pair|mute|unmute|pause|resume|play|stop|search|lock|put|go|show|tell|"
              r"what|type|press|make|skip|log|remind|shut|restart)\b")


def split_commands(text):
    """'connect CMF headphone and open text editor' -> two commands, only if every part is a command."""
    if (key_command(text) or ("",))[0] == "type":        # dictated text may contain "and" or command words
        return [text]
    if "close" in matched(text) and re.search(r"\b(keep|except|but|other than|apart from|leave)\b", text, re.I):
        return [text]                                    # "close all and keep only terminal open" is one command
    parts = [p for p in re.split(r"\s*(?:,|\band then\b|\bthen\b|\band\b)\s*", text, flags=re.I) if p.strip()]
    # A lone verb between "and"s ("... brave browser and type and play X") is a slip, not a command.
    parts = [p for p in parts if not re.fullmatch(r"\s*(?:type|press|search|play|open|launch|start|go|also|so|now)\s*",
                                                  p, re.I)] or parts
    if re.match(r"\s*(?:please\s+)?(?:search|google search|play)\b", text, re.I):
        return [text]                                    # a search takes free text: "search for volume and brightness"
    lead = re.match(r"\s*((?:turn|switch)\s+(?:on|off)|set|increase|decrease|raise|lower|reduce|enable|disable)\b",
                    parts[0] if parts else "", re.I)
    if lead:                                             # "turn off wifi and bluetooth": the verb carries over
        parts = [parts[0]] + [p if re.match(VERB_START, p, re.I) else f"{lead.group(1)} {p}" for p in parts[1:]]
    # Let coordinated targets inherit "open": "open Brave and YouTube and play Beggin" becomes
    # "open Brave", "open YouTube", "play Beggin". Only fill bare pieces when another explicit command
    # follows, so ordinary names such as "Tom and Jerry" remain one search.
    first = re.match(r"\s*(open|launch|start|close|quit)\b", text, re.I)
    if first and any(matched(p) for p in parts[1:]):
        parts = [parts[0]] + [p if matched(p) else f"{first.group(1)} {p}" for p in parts[1:]]
    if len(parts) > 1 and all(matched(p) for p in parts):
        return parts
    return [text]


def resolve_connect(text, groups):
    """'connect to X' with no 'wifi'/'headphone' word: X is whatever exists, a Wi-Fi network or a paired device."""
    generic = ("connect", "disconnect", "pair")
    t = text.lower()
    if groups != ["bluetooth"] or any(said(w, t) for w in TRIGGERS["bluetooth"] if w not in generic):
        return groups
    devices = [d[1] for d in bt_devices()]
    networks = (saved_wifi_names() or []) + [nmcli_fields(l)[0] for l in
                                     sh("nmcli", "-t", "-f", "SSID", "device", "wifi", "list", "--rescan", "no").splitlines()]
    dev, dev_ties = best_match(text, devices, lambda n: [n])
    net, net_ties = best_match(text, networks, lambda n: [n])
    if (net or net_ties) and not (dev or dev_ties):
        return ["wifi"]
    if devices and not (dev or dev_ties or net or net_ties) and [w for w in words(text) if w not in STOPWORDS]:
        return []                                        # "connect to the vpn": no device or network by that name
    return groups


def run_one(agent, text):
    groups = resolve_connect(text, matched(text))
    state.update(query=text, done=False, groups=groups)
    if not groups:
        return None
    if len(groups) == 1 and groups[0] in DIRECT:                     # instant path
        return DIRECT[groups[0]]().get("say")
    if groups == ["open"] or groups == ["search"]:                    # simple "open X" / "search X"
        r = direct_open_or_search(groups[0], text)
        if r and "say" in r:
            return r["say"]
        state.update(query=text, done=False)
    results = agent.run(text, max_steps=1)["results"] or []          # Needle decides
    return next((r["say"] for r in results if isinstance(r, dict) and "say" in r), None)


def handle(agent, text):
    """Return the reply to speak/print for one utterance. Never raises."""
    text = text.strip()
    state["detail"] = ""                                               # commands awaiting a yes, for the screen
    state["spoken"] = ""                                               # answer text already spoken while streaming
    if not text:
        return "Sorry, I didn't catch that."
    try:
        pending, state["pending"] = state["pending"], None
        if pending and time.time() < pending[1]:                       # answer to "Should I...?" / "Which one?"
            reply = pending[0](text)
            if reply:
                return reply
        if re.match(r"(why|how (do|does|to|can|could|should|come))\b", text, re.I):
            return answer(text)                                        # "why is my wifi slow" is not a command
        m = re.fullmatch(r"(open|launch|start|search|search for|google|look up|type|press|go to|show me)"
                         r"(?:\s+(?:it|that|this|the|my|them|something))?[\s.!?]*", text, re.I)
        if m:                                                          # "open" ... pause ... "brave browser"
            verb = m.group(1).lower()
            ask_follow_up(lambda rest: "Okay, cancelled" if yes_no(rest) == "no" else
                          handle(agent, rest if re.match(rf"{verb}\b", rest, re.I) else f"{verb} {rest}"))
            return f"{verb.capitalize()} what?"
        # Natural media follow-up: "change the song to X" means play X on YouTube.
        text = re.sub(r"^change (?:the )?song(?: to)?\s+", "play ", text, flags=re.I)
        # A lone "pause" over music is often heard as one of these (seen in the logs: "pass", "boss").
        text = re.sub(r"^(?:pass|paas|pas|pose|paws|pours|boss|bors)((?: it| the (?:song|video|music))?)[.!]?$",
                      r"pause\1", text, flags=re.I)
        commands = split_commands(text)
        if len(commands) > 1:                                          # "open youtube and brave browser and ..."
            using = " ".join(c for c in commands if re.match(r"\s*(?:open|launch|start)\b", c, re.I)
                             or re.search(r"\b(?:in|on|using|with)\s+\S", c, re.I)).lower()   # not "close chrome"
            named = [a for a in installed_apps() if a["browser"] and any(
                said(w, using) for w in words(a["name"]) if w not in STOPWORDS and w != "google")]
            if len(named) == 1:
                state["browser"] = named[0]                            # one browser named: use it for every part
                # "open brave and open youtube": YouTube opens as a tab in Brave, no extra blank window or tab.
                names = [w for w in words(named[0]["name"]) if w not in STOPWORDS]
                bare = r"\s*(?:open|launch|start)\s+(?:the\s+)?(?:%s)(?:\s+(?:web\s+)?browser)?(?:\s+app)?\s*" % "|".join(names)
                rest = [c for c in commands if not re.fullmatch(bare, c, re.I)]
                if rest and len(rest) < len(commands):
                    commands = rest
        if any(re.match(r"\s*play\s+\S", c, re.I) for c in commands):      # the song's own tab is enough
            commands = [c for c in commands if not re.fullmatch(r"\s*(?:open|launch|start)\s+you\s?tube\s*", c, re.I)] \
                or commands
        replies, left = [], []
        for i, cmd in enumerate(commands):
            replies.append(run_one(agent, cmd))
            if state["pending"] and i + 1 < len(commands):          # it asked something: one question at a time,
                left = commands[i + 1:]                              # so a "yes" can only mean one thing
                break
        if len(commands) == 1 and not replies[0] and not matched(text):
            return answer(text)                                        # a question, not a command
        # Command replies stay out of the LLM history: the tiny model just repeats them back.
        parts = [r or f"I can't do that locally yet: {c}" for r, c in zip(replies, commands)]
        if len(parts) > 1:
            parts = [p.rstrip(".") for p in parts]                     # no ".." when joining
        if left:
            parts.insert(-1, f"I'll leave {one_of(left, 'and')} for later")
        reply = ". ".join(parts)
        remember(text, reply)
        return reply
    except Exception as e:                                             # never crash the voice loop
        return f"Sorry, something went wrong: {type(e).__name__}"


class LazyAgent:
    """Needle, loaded (73 MB, ~5 s) only the first time a command is ambiguous enough to need it."""
    _agent = None

    def run(self, *args, **kwargs):
        if self._agent is None:
            self._agent = cactus.Cactus(tools=TOOLS, stateless=True).__enter__()
        return self._agent.run(*args, **kwargs)


def warm_up():
    """Load the LLM into memory so the first answer isn't slow."""
    if not LLM_MODEL:
        return
    try:
        # Same options as ask_llm: a different num_ctx would make Ollama load the model a second time.
        body = {"model": LLM_MODEL, "messages": [], "keep_alive": "5m", "options": {"num_ctx": 2048}}
        urllib.request.urlopen(urllib.request.Request(OLLAMA, json.dumps(body).encode()), timeout=120).read()
    except Exception:
        pass


if __name__ == "__main__":
    threading.Thread(target=warm_up, daemon=True).start()
    with cactus.Cactus(tools=TOOLS, stateless=True) as agent:
        print("Needle assistant (typed). Ctrl+C or 'quit' to exit.")
        while True:
            try:
                q = input("> ").strip()
            except (EOFError, KeyboardInterrupt):
                break
            if q.lower() in {"quit", "exit"}:
                break
            if q:
                print(handle(agent, q))
                if state["detail"]:
                    print(state["detail"])
