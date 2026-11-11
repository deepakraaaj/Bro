"""Hands-free voice assistant: say "Hey Needle, open YouTube in Brave".

Mic -> cheap loudness check -> Google speech-to-text, Indian English (only when someone speaks; needs internet;
the audio of each utterance is sent to Google over HTTPS and is not kept on the laptop)
    -> wake word? -> assistant.handle() -> spoken reply.
Runs as the user service needle.service. By hand:  needle voice_assistant.py
"""
import difflib
import fcntl
import http.client
import json
import os
import re
import select
import socket
import statistics
import subprocess
import sys
import tempfile
import threading
import time
import traceback

import numpy as np
import onnxruntime
import speech_recognition as sr
from openwakeword.model import Model as WakeModel
from piper import PiperVoice
from piper.config import PiperConfig

import assistant
from assistant import LazyAgent, clean_env, handle, matched

RATE = 16000
FRAME = RATE // 10                      # 0.1 s of audio per read
SILENCE_END = 10                        # allow a natural 1.0 s pause inside a command
MAX_FRAMES = 150                        # 15 s max, enough for a short chain of commands
FOLLOW_UP_SECONDS = 8                   # after "Hey Needle" alone, listen this long for the command
CONTINUOUS_SECONDS = 20                 # follow-up controls need no wake phrase during this window
# "Tick Tick" is deliberately unlike normal assistant phrases and remains easy to recognize over media.
# Accept likely STT spellings, and keep Computer/the old name as fallback aliases.
WAKE = re.compile(r"\b(?:(?:hey[\s,]+)?jarvis|(?:tick|tic|tik)[\s,]+(?:tick|tic|tik)|electric bike|computer|commander|(?:(?:yo|your|hello|hey|hi|wake up)[\s,]+)?"
                  r"(?:needle|neel|neil|nidal|noodle|needles|nidhi|nidle))\b[\s,.!?]*", re.I)
# Neural voice (63 MB).
VOICE = os.path.expanduser("~/.local/share/needle-voices/en_US-lessac-medium.onnx")
_voice = None
_wake_model = None
_bubble = "0"
RUNTIME = os.environ.get("XDG_RUNTIME_DIR", tempfile.gettempdir())
# The Ctrl+Alt+Space shortcut touches this file; the mic loop checks it every 0.1 s (no signals, no PIDs).
LISTEN_FILE = os.path.join(RUNTIME, "needle-listen")
_last_press = os.stat(LISTEN_FILE).st_mtime_ns if os.path.exists(LISTEN_FILE) else None
# Speech-to-text: Google's free web speech service (online). The library defaults to plain http; use https.
# "en-IN" = Indian English, which hears this accent far better than the US/UK-trained offline models.
LANGUAGE = "en-IN"
GOOGLE = "https://www.google.com/speech-api/v2/recognize"
MIC_GAIN = os.environ.get("NEEDLE_MIC_GAIN", "50%")
WAKE_MODEL = os.path.expanduser(
    "~/.local/share/needle-venv/lib/python3.14/site-packages/openwakeword/resources/models/hey_jarvis_v0.1.onnx")
WAKE_THRESHOLD = 0.40                 # lower latency; command routing still prevents random actions
_recognizer = sr.Recognizer()
_recognizer.operation_timeout = 8       # without this a stalled connection hangs the whole loop forever
listen = {"threshold": 0.0}             # loudness above which a 0.1 s frame counts as speech
METER = sys.stdout.isatty()             # live mic meter only in a terminal, not in a background log


def mic_node():
    """The mic to record from, pinned by name so PipeWire never moves the recording to another device.
    Never a Bluetooth headset: opening its mic switches it to call ("handset") mode and music sounds bad.
    Uses the default input when it isn't Bluetooth, otherwise the laptop's built-in mic."""
    try:
        nodes = json.loads(subprocess.run(["pw-dump"], capture_output=True, text=True, timeout=5).stdout)
        default = subprocess.run(["wpctl", "inspect", "@DEFAULT_AUDIO_SOURCE@"], capture_output=True, text=True,
                                 timeout=5).stdout
    except (ValueError, OSError, subprocess.TimeoutExpired):
        return None
    m = re.search(r'node\.name = "([^"]+)"', default)
    if m and not m.group(1).startswith("bluez"):
        return m.group(1)
    found = [((n.get("info") or {}).get("props") or {}) for n in nodes]
    found = [p for p in found if p.get("media.class") == "Audio/Source"
             and str(p.get("node.name", "")).startswith("alsa_input")]
    # The laptop's own mic array is the "Digital"/"Internal" one; the analog one is usually an empty jack.
    found.sort(key=lambda p: not re.search(r"digital|internal|built-?in", str(p.get("node.description", "")), re.I))
    return found[0]["node.name"] if found else None


def mic():
    env = dict(os.environ)
    node = mic_node()
    if node:
        env["PIPEWIRE_NODE"] = node
    return subprocess.Popen(["arecord", "-q", "-t", "raw", "-f", "S16_LE", "-r", str(RATE), "-c", "1"],
                            stdout=subprocess.PIPE, env=env, bufsize=0)


def prepare_mic():
    """Give the internal mic enough gain to retain quiet consonants. This targets only Needle's selected
    source, not the current default (which may be a Bluetooth headset). Set NEEDLE_MIC_GAIN="" to opt out."""
    node = mic_node()
    if not node or not MIC_GAIN:
        return
    try:
        nodes = json.loads(subprocess.run(["pw-dump"], capture_output=True, text=True, timeout=5).stdout)
        props = [((n.get("info") or {}).get("props") or {}) for n in nodes]
        target = next(p.get("object.id") for p in props if p.get("node.name") == node)
        subprocess.run(["wpctl", "set-volume", str(target), MIC_GAIN], capture_output=True, timeout=5)
    except (StopIteration, ValueError, OSError, subprocess.TimeoutExpired):
        pass


def read_frame(rec):
    """One 0.1 s frame. b"" when the recorder ended, None when it delivered nothing for 5 s (the mic stalled:
    suspend/resume, PipeWire restart, or the stream moved to a silent Bluetooth mic)."""
    data = b""
    while len(data) < FRAME * 2:
        if not select.select([rec.stdout], [], [], 5)[0]:
            return None
        chunk = os.read(rec.stdout.fileno(), FRAME * 2 - len(data))
        if not chunk:
            return b""
        data += chunk
    return data


def stop(proc):
    proc.terminate()
    try:
        proc.wait(timeout=2)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()


def rms(frame):
    """Loudness in the speech range only. 90% of this room's noise is fan/AC rumble below 250 Hz, which
    used to trip the speech trigger on its own."""
    s = np.frombuffer(frame[:len(frame) & ~1], "<i2").astype(np.float32)
    if not len(s):
        return 0
    spectrum = np.fft.rfft(s)
    spectrum[np.fft.rfftfreq(len(s), 1 / RATE) < 250] = 0
    return float(np.sqrt(np.mean(np.fft.irfft(spectrum, len(s)) ** 2)))


def load_voice():
    """Piper with 2 threads and no busy-waiting. By default it spins on all cores: a short reply cost
    2.6 s of CPU and took 360 ms to start; this way it is 0.15 s of CPU and 83 ms."""
    options = onnxruntime.SessionOptions()
    options.intra_op_num_threads = 2
    options.inter_op_num_threads = 1
    options.add_session_config_entry("session.intra_op.allow_spinning", "0")
    with open(VOICE + ".json", encoding="utf-8") as f:
        config = PiperConfig.from_dict(json.load(f))
    session = onnxruntime.InferenceSession(VOICE, sess_options=options, providers=["CPUExecutionProvider"])
    return PiperVoice(session=session, config=config)


def show(heard, reply):
    """On-screen bubble with what was heard and what was done. Replaces the previous bubble, never piles up."""
    global _bubble
    try:
        out = subprocess.run(["notify-send", "-a", "Needle", "-p", "-r", _bubble, "-t", "4000",
                              "-h", "boolean:transient:true", f"\u201c{heard}\u201d", reply],
                             capture_output=True, text=True, env=clean_env(), timeout=5).stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        return                                       # no bubble is never a reason to stop
    _bubble = out or _bubble


def say(text):
    """Speak with the Piper neural voice (~170 ms to first sound), streamed sentence by sentence.
    Falls back to the robotic system voice if the voice file is missing."""
    global _voice
    print(f"Needle: {text}")
    text = re.sub(r"[*#`_]", "", text)
    player = None
    try:
        if not os.path.exists(VOICE):
            subprocess.run(["spd-say", "-w", text], env=clean_env(), timeout=30)
            return
        if _voice is None:
            _voice = load_voice()
        for chunk in _voice.synthesize(text):
            if player is None:
                player = subprocess.Popen(["aplay", "-q", "-t", "raw", "-f", "S16_LE", "-c", "1",
                                           "-r", str(chunk.sample_rate)], stdin=subprocess.PIPE)
            player.stdin.write(chunk.audio_int16_bytes)
        if player:
            player.stdin.close()
            player.wait(timeout=30)
    except (OSError, subprocess.TimeoutExpired):     # speaker gone or stuck: the reply is still on screen
        if player:
            player.kill()


def hotkey_pressed():
    """True once per press of the keyboard shortcut, which just touches LISTEN_FILE."""
    global _last_press
    try:
        mtime = os.stat(LISTEN_FILE).st_mtime_ns
    except OSError:
        return False
    if mtime == _last_press:
        return False
    _last_press = mtime
    return True


def beep(wait=False):
    """Short tone. wait=True plays it before returning, so the mic doesn't record it as speech."""
    t = np.arange(int(RATE * 0.12)) / RATE
    tone = (np.sin(2 * np.pi * 880 * t) * np.minimum(1, np.minimum(t, t[::-1]) * 40) * 8000).astype("<i2").tobytes()

    def play():
        try:
            subprocess.run(["aplay", "-q", "-t", "raw", "-f", "S16_LE", "-r", str(RATE), "-c", "1"],
                           input=tone, timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            pass
    if wait:
        play()
    else:
        threading.Thread(target=play, daemon=True).start()


def alive():
    """Tell systemd the loop is still running (it restarts the service if this stops for 7 minutes)."""
    address = os.environ.get("NOTIFY_SOCKET")
    if not address:
        return
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sock:
            sock.sendto(b"READY=1\nWATCHDOG=1", "\0" + address[1:] if address.startswith("@") else address)
    except OSError:
        pass


def utterances():
    """Yield (audio_bytes, by_hotkey, hit_limit) for speech segments. by_hotkey: the shortcut was pressed just
    before, so no wake word is needed. hit_limit: it ran to the 10 s cap (constant noise, not a sentence).
    The mic is restarted after each one so we never hear ourselves, and every 30 s when idle so a changed
    default microphone (Bluetooth headset connected) is picked up."""
    armed_until = 0.0
    while True:
        rec = mic()
        started = time.time()
        frames, quiet, speaking, skip, idle, loud_frames = [], 0, False, 0, 0, 0
        force_capture = False
        pre = []                                       # keep 0.5 s before speech starts
        try:
            while True:
                f = read_frame(rec)
                if not f:                              # recorder ended, or stalled for 5 s: reopen the mic
                    break
                idle += 1
                if idle % 50 == 0:
                    alive()
                pressed = hotkey_pressed()
                if pressed and not armed_until:        # ignore GNOME key-repeat while already listening
                    print("Shortcut: listening")
                    beep()
                    armed_until = time.time() + FOLLOW_UP_SECONDS
                    frames, quiet, speaking, pre, skip, loud_frames = [], 0, False, [], 2, 0
                    force_capture = True               # Bluetooth speech may fail the high-frequency RMS gate
                    if METER:
                        print("\r  listening for your command..." + " " * 30, end="", flush=True)
                    continue
                if skip:
                    skip -= 1
                    continue
                if force_capture and not speaking:
                    speaking, frames, loud_frames, quiet = True, [f], 2, 0
                    force_capture = False
                    continue
                if _wake_model is not None and time.time() >= armed_until:
                    samples = np.frombuffer(f, "<i2")
                    score = max(_wake_model.predict(samples).values(), default=0.0)
                    if score >= WAKE_THRESHOLD:
                        print(f"Wake: Hey Jarvis [{score:.2f}]")
                        beep()                         # immediate acknowledgement; don't stop/reopen the mic
                        armed_until = time.time() + FOLLOW_UP_SECONDS
                        # Bluetooth headset speech can be strong enough for the neural wake model but weak after
                        # our >250 Hz RMS filter. Once explicitly awakened, force recording instead of asking the
                        # crude loudness gate to approve the command too.
                        if not speaking:
                            speaking, frames, loud_frames, quiet = True, pre[:] + [f], 2, 0
                        try:
                            _wake_model.reset()
                        except AttributeError:
                            pass
                        # Keep the audio already collected: the acoustic model often fires near the end of
                        # "Hey Jarvis", when the user has already started saying the command.
                if armed_until and time.time() > armed_until and not speaking:
                    armed_until = 0.0
                level = rms(f)
                loud = level > listen["threshold"]
                if not speaking:
                    pre = (pre + [f])[-5:]
                    if METER:
                        bar = "#" * min(int(level / listen["threshold"] * 10), 30)
                        print(f"\r  mic {level:5.0f} / {listen['threshold']:.0f} {bar:<30}", end="", flush=True)
                    if loud:
                        speaking, frames, loud_frames = True, pre[:], 1
                        if METER:
                            print("\r  hearing you..." + " " * 40, end="", flush=True)
                    elif idle >= 300 and not armed_until:
                        break                          # 30 s of quiet: reopen, in case the default mic changed
                    continue
                frames.append(f)
                loud_frames += loud
                quiet = 0 if loud else quiet + 1
                if quiet >= SILENCE_END or len(frames) >= MAX_FRAMES:
                    break
        finally:                                       # also on Ctrl+C: release the mic
            stop(rec)
        if not frames and time.time() - started < 1:   # mic missing/busy: wait instead of spinning the CPU
            print("Microphone not available, retrying in 3 s...")
            time.sleep(3)
            continue
        if loud_frames >= 2:                           # short wake phrases such as "Tick Tick" are often 0.2 s
            by_hotkey = time.time() < armed_until + MAX_FRAMES * 0.1
            armed_until = 0.0
            yield b"".join(frames), by_hotkey, len(frames) >= MAX_FRAMES


def transcribe(audio):
    """Text Google heard, "" for no speech, or None when Google can't be reached or answers with garbage
    (no internet, timeout, a captive-portal page)."""
    try:
        result = _recognizer.recognize_google(sr.AudioData(audio, RATE, 2), language=LANGUAGE, endpoint=GOOGLE,
                                              show_all=True)
        # SpeechRecognition already unwraps Google's newline-delimited response. With show_all=True its
        # result is {"alternative": [...]}, not the original nested {"result": [...]} wire format.
        raw_alternatives = result.get("alternative", [])
        if not raw_alternatives:                       # tolerate older SpeechRecognition response shapes
            raw_alternatives = [a for block in result.get("result", []) for a in block.get("alternative", [])]
        alternatives = [a.get("transcript", "").strip() for a in raw_alternatives]
        alternatives = [t for t in alternatives if t]
        # A lower-ranked alternative often preserves a distinctive wake phrase that the top guess mangles.
        return next((t for t in alternatives if WAKE.search(t)), alternatives[0] if alternatives else "")
    except sr.UnknownValueError:
        return ""
    except (sr.RequestError, OSError, ValueError, KeyError, http.client.HTTPException):
        return None


def calibrate():
    """Speech threshold just above room noise, sensitive enough to catch a short, normally spoken wake phrase."""
    while True:
        rec = mic()
        try:
            frames = [read_frame(rec) for _ in range(30)]          # 3 s; the median ignores a cough or a word
        finally:
            stop(rec)
        if all(frames):
            break
        print("Microphone not ready, retrying in 3 s...")            # a dead mic must not be taken for silence
        time.sleep(3)
    noise = statistics.median(rms(f) for f in frames)
    # Never let a fan, music, or a noisy calibration make normal speech impossible to trigger. False audio
    # segments are harmless because wake-word matching still gates every action.
    listen["threshold"] = min(max(noise * 1.35, noise + 80, 120), 650)
    return listen["threshold"]


def wake_word(text):
    """Where the command starts after the wake word, or None. Fuzzy on the first 3 words, so accents and
    mishearings ('Hey Neel', 'A noodle', 'He needle') still wake it."""
    m = WAKE.search(text)
    if m:
        return m.end()
    for w in re.finditer(r"[A-Za-z']+", text):
        if w.start() > 0 and len(text[:w.start()].split()) >= 3:
            break
        word = w.group().lower()
        after_greeting = re.match(r"\s*(hey|hi|ok|okay)\W*$", text[:w.start()], re.I)   # "Hey Midle"
        if word not in ("need", "needs", "needed", "needy", "needful") and \
                difflib.SequenceMatcher(None, word, "needle").ratio() >= (0.5 if after_greeting else 0.6):
            return w.end() + len(re.match(r"[\s,.!?]*", text[w.end():]).group())
    return None


# Speech starting with one of these is taken as a command even without "Hey Needle".
COMMAND_STARTS = {"open", "close", "quit", "launch", "start", "connect", "disconnect", "turn", "switch", "set", "change",
                  "increase", "decrease", "mute", "unmute", "volume", "brightness", "play", "pause", "resume",
                  "next", "skip", "search", "google", "log", "lock", "shut", "restart",
                  "forward", "fast", "rewind", "go", "back", "backward", "seek", "ahead"}


def looks_like_command(text):
    text = re.sub(r"^(?:(?:please|can you|could you|will you|would you|i want to|i want you to|just|now|okay|ok)\s+)+",
                  "", text.strip(), flags=re.I)
    first = re.findall(r"[a-z]+", text.lower())[:1]
    groups = matched(text)
    return groups == ["keys"] or (bool(first) and first[0] in COMMAND_STARTS and bool(groups))


def respond(agent, audio, by_hotkey, hit_limit, awake_until):
    """Handle one utterance; returns until when the next one needs no wake word."""
    t0 = time.perf_counter()
    text = transcribe(audio)
    heard_in = time.perf_counter() - t0
    if text is None:
        print("Can't reach Google speech-to-text. Check the internet connection.")
        if by_hotkey:
            say("I need internet to hear you")
        return awake_until
    if METER:
        print("\r" + " " * 50 + "\r", end="")               # clear the mic meter line
    if not text:
        if hit_limit:                                          # 10 s of "speech" with no words: the room got louder
            print(f"Constant noise, recalibrating. New threshold {calibrate():.0f}")
        # A noise must not use up the shortcut press: keep listening without the wake word.
        return time.time() + FOLLOW_UP_SECONDS if by_hotkey else awake_until
    start = wake_word(text)
    if start is not None:
        command = text[start:].strip(" .,!?")
    elif by_hotkey or time.time() < awake_until or looks_like_command(text):
        command = text.strip(" .,!?")
    else:
        print(f"  (heard, no wake word: {text!r})")
        return awake_until
    print(f"You: {text}   [heard in {heard_in:.1f} s]")
    if not command:
        say("I'm listening")
        return time.time() + FOLLOW_UP_SECONDS
    alive()
    reply = handle(agent, command)
    alive()
    show(text, reply + ("\n" + assistant.state["detail"] if assistant.state["detail"] else ""))
    if matched(command) == ["keys"] and not reply.startswith("I "):
        print(f"Needle: {reply}")                           # typing/keys: a beep, no talking over you
        beep(wait=True)
    else:
        say(reply)
    # Keep the conversation open after every action. This makes "pause", "next song", or another command work
    # naturally without repeating the wake phrase. The window starts after Needle finishes its reply.
    if not reply.rstrip().endswith("?"):
        return time.time() + CONTINUOUS_SECONDS
    if assistant.state["pending"]:
        assistant.state["pending"] = (assistant.state["pending"][0], time.time() + FOLLOW_UP_SECONDS)
    return time.time() + FOLLOW_UP_SECONDS


def main():
    lock = open(os.path.join(RUNTIME, "needle.lock"), "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)        # two copies would both act on every command
    except OSError:
        print("Needle is already running (as the background service). Not starting a second copy.")
        return
    globals()["_voice"] = load_voice() if os.path.exists(VOICE) else None   # no delay on first reply
    if os.path.exists(WAKE_MODEL):
        globals()["_wake_model"] = WakeModel(wakeword_models=[WAKE_MODEL], inference_framework="onnx",
                                               vad_threshold=0)
    prepare_mic()
    print(f"Mic: {mic_node() or 'system default'}")
    print("Calibrating mic, stay quiet for 3 seconds...")
    print(f"Speech threshold {calibrate():.0f}. If the meter barely moves when you talk, raise the mic volume.")
    awake_until = 0.0
    agent = LazyAgent()                 # Needle loads only if a command needs it
    assistant.on_wait = say             # "One moment" while a slow request is worked out
    alive()
    say("Needle is ready")
    print('Say "Hey Jarvis" and the command in one sentence; the beep confirms activation. '
          'Ctrl+Alt+Space also works. (Ctrl+C to quit)')
    for audio, by_hotkey, hit_limit in utterances():
        try:
            awake_until = respond(agent, audio, by_hotkey, hit_limit, awake_until)
        except Exception:               # one bad utterance must never stop the assistant
            traceback.print_exc()
            awake_until = 0.0


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nBye")
