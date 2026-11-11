"""Needle voice pipeline v2, controlled by the "Bro" button in the top bar (needle_tray.py).

Private by default: the microphone stays closed until you click Bro (or press Ctrl+Alt+Space); it then
records one command and closes again. Turn on "Hands-free" in Bro's menu to have it always listen for
"Hey Jarvis" (detected offline on the laptop). Only a captured command is sent to Google speech-to-text.
After Needle asks a question ("Which one?", "Should I do it?") the answer is taken for FOLLOW_UP_SECONDS
without clicking or the wake phrase.
Runs as the user service needle.service. By hand:  needle voice_assistant_v2.py
"""
from collections import deque
from concurrent.futures import ThreadPoolExecutor
import fcntl
import os
import re
import tempfile
import threading
import time
import traceback

import numpy as np
import speech_recognition as sr
from openwakeword.model import Model as WakeModel

import assistant
import needle_tray as tray
import voice_assistant as legacy
from assistant import LazyAgent, handle, matched

RUNTIME = os.environ.get("XDG_RUNTIME_DIR", tempfile.gettempdir())
LOCK_FILE = os.path.join(RUNTIME, "needle.lock")
STATUS_FILE = os.path.join(RUNTIME, "needle-status")     # read by needle_indicator.py (the orb)
PRE_ROLL_FRAMES = 25                 # 2.5 s kept before the wake: never clip the phrase or the first word
END_SILENCE_FRAMES = 10              # 1 s of quiet ends a command
MAX_CAPTURE_FRAMES = 150             # 15 s maximum
WAIT_FOR_SPEECH_FRAMES = 50          # after the wake/shortcut/question, wait up to 5 s for speech to start
FOLLOW_UP_SECONDS = 10
# Your real "Hey Jarvis" scored 0.44-0.99 in the logs, so 0.4 catches it; a capture that turns out to hold
# no command does nothing (command_from_transcript), so a rare false wake only costs one Google request.
WAKE_THRESHOLD = 0.4
WAKE_MODEL = os.path.join(os.path.dirname(__import__("openwakeword").__file__), "resources", "models",
                          "hey_jarvis_v0.1.onnx")
KNOWN_TARGETS = {"brave", "chrome", "firefox", "youtube", "whatsapp", "calculator", "settings",
                 "terminal", "editor", "files", "spotify", "wifi", "bluetooth"}

session = {"armed_until": 0.0}       # until when speech needs no wake phrase (answer to a question)


def set_status(value):
    """idle / listening / processing, shown on the Bro button (and in STATUS_FILE for other tools)."""
    tray.set_status({"idle": "ready" if tray.handsfree() else "off", "processing": "thinking"}.get(value, value))
    try:
        with open(STATUS_FILE, "w") as status:
            status.write(value)
    except OSError:
        pass


def duck():
    """Turn the speakers down while you speak, like Google Assistant, so music doesn't drown out the command.
    Returns the volume to restore afterwards, or None when nothing was changed."""
    m = re.search(r"Volume:\s*([\d.]+)(\s*\[MUTED\])?", assistant.sh("wpctl", "get-volume", "@DEFAULT_AUDIO_SINK@", timeout=3))
    if not m or m.group(2) or float(m.group(1)) <= 0.15:
        return None
    volume = float(m.group(1))
    assistant.sh("wpctl", "set-volume", "@DEFAULT_AUDIO_SINK@", f"{max(volume * 0.25, 0.08):.2f}", timeout=3)
    return volume


def unduck(volume):
    if volume is not None:
        assistant.sh("wpctl", "set-volume", "@DEFAULT_AUDIO_SINK@", f"{volume:.2f}", timeout=3)


def prewarm():
    """Start loading the local brain while the user is still talking, so a question gets no load delay."""
    threading.Thread(target=assistant.warm_up, daemon=True).start()


def wait_for_request():
    """Mic closed: wait until Bro is clicked, the shortcut is pressed, an answer is due, or hands-free is
    switched on. Returns "shortcut", "follow-up" or None (hands-free on: go back to listening)."""
    while True:
        legacy.alive()
        if tray.clicked.is_set() or legacy.hotkey_pressed():
            tray.clicked.clear()
            return "shortcut"
        if time.time() < session["armed_until"]:
            return "follow-up"
        if tray.handsfree():
            return None
        tray.clicked.wait(0.1)


def command_from_transcript(text, activation):
    """Remove a recognized wake phrase without assuming Google transcribed it perfectly."""
    text = text.strip(" .,!?")
    m = re.search(r"\b(?:hey\s+)?jarvis\b[\s,.!?]*", text, re.I)
    if m:
        return text[m.end():].strip(" .,!?")
    # Wake phrases are commonly mangled into one or two junk words ("reservation open Brave", "hijab is
    # open Brave"). On an explicitly activated recording, recover the first recognizable local command.
    starts = r"open|close|quit|launch|start|connect|disconnect|turn|switch|set|change|increase|decrease|mute|unmute|volume|brightness|play|pause|resume|next|skip|search|google|log|lock|shut|restart|forward|rewind|go|back|seek|type|press"
    local = re.search(rf"\b(?:{starts})\b.*", text, re.I)
    if local and len(text[:local.start()].split()) <= 3:
        recovered = local.group().strip(" .,!?")
        if legacy.looks_like_command(recovered):
            return recovered
    if activation == "wake" and legacy.looks_like_command(text):
        return text                              # Google omitted the wake phrase but kept the command
    return text if activation != "wake" else ""


def _google_candidates(audio, language):
    """Return every Google alternative for one locale, preserving confidence when supplied."""
    try:
        result = legacy._recognizer.recognize_google(sr.AudioData(audio, legacy.RATE, 2), language=language,
                                                     endpoint=legacy.GOOGLE, show_all=True)
        alternatives = result.get("alternative", [])
        return [(a.get("transcript", "").strip(), float(a.get("confidence", 0)))
                for a in alternatives if a.get("transcript", "").strip()]
    except Exception:
        return []


def transcribe_best(audio, activation):
    """Run two accent models concurrently and prefer alternatives that form concrete local commands."""
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [(lang, pool.submit(_google_candidates, audio, lang)) for lang in ("en-IN", "en-US")]
        candidates = [(text, confidence, lang) for lang, future in futures
                      for text, confidence in future.result()]
    candidates = list(dict.fromkeys(candidates))
    if not candidates:
        return "", []

    def score(item):
        text, confidence, lang = item
        command = command_from_transcript(text, activation)
        tokens = set(re.findall(r"[a-z]+", command.lower()))
        return (20 * legacy.looks_like_command(command)
                + 5 * len(matched(command))
                + 3 * len(tokens & KNOWN_TARGETS)
                + 2 * bool(re.search(r"\b(?:hey\s+)?jarvis\b", text, re.I))
                + confidence
                + (0.25 if lang == "en-IN" else 0))

    ranked = sorted(candidates, key=score, reverse=True)
    return ranked[0][0], [(lang, round(score((text, conf, lang)), 2), text) for text, conf, lang in ranked]


class LazyWake:
    """The "Hey Jarvis" detector (about 100 MB), loaded the first time hands-free mode is switched on."""
    _model = None

    def _get(self):
        if self._model is None:
            self._model = WakeModel(wakeword_models=[WAKE_MODEL], inference_framework="onnx")
        return self._model

    def predict(self, samples):
        return self._get().predict(samples)

    def reset(self):
        self._get().reset()


def events(wake):
    """Yield (audio, activation) for each command. One recorder hears both the wake phrase and the command,
    so nothing said straight after "Hey Jarvis" is lost. activation: "wake", "shortcut" or "follow-up"."""
    while True:
        set_status("idle")
        requested = None if tray.handsfree() else wait_for_request()
        if requested == "shortcut":
            legacy.beep(wait=True)               # played before the mic opens, so it isn't recorded
        ducked = duck() if requested else None   # click / shortcut / answer: turn the music down now
        if requested:
            prewarm()
        rec = legacy.mic()
        opened = time.time()
        pre = deque(maxlen=PRE_ROLL_FRAMES)
        capture, activation, frames_seen = [], requested, 0
        speech_started, quiet, waited, loud_frames, grace = False, 0, 0, 0, 0
        try:
            while True:
                frame = legacy.read_frame(rec)
                if not frame:                    # recorder ended or stalled: reopen it
                    break
                frames_seen += 1
                if frames_seen % 50 == 0:
                    legacy.alive()
                if activation is None:
                    pre.append(frame)
                    if not tray.handsfree():          # switched off while listening: close the mic
                        break
                    if tray.clicked.is_set() or legacy.hotkey_pressed():
                        tray.clicked.clear()
                        activation = "shortcut"
                        legacy.beep()
                        pre.clear()              # the beep must not be part of the command
                    elif time.time() < session["armed_until"]:
                        activation = "follow-up"
                    elif max(wake.predict(np.frombuffer(frame, "<i2")).values(), default=0.0) >= WAKE_THRESHOLD:
                        activation = "wake"
                        wake.reset()
                        legacy.beep()
                        capture = list(pre)      # keep "Hey Jarvis" and any command already started
                        # Wait for the command (up to 5 s) instead of ending on the pause after "Hey Jarvis";
                        # the next 0.3 s is still the tail of "Jarvis", so it doesn't count as the command.
                        grace = 3
                    if activation is None:
                        if frames_seen >= 300:   # 30 s idle: reopen, in case the default mic changed
                            break
                        continue
                    print(f"Activate: {activation}", flush=True)
                    set_status("listening")
                    prewarm()
                    ducked = duck()
                    if activation != "wake":
                        continue
                elif frames_seen == 1:                # asked for by click/shortcut/answer: mic was just opened
                    print(f"Activate: {activation}", flush=True)
                    set_status("listening")
                capture.append(frame)
                if grace:
                    grace -= 1
                    continue
                loud = legacy.rms(frame) > legacy.listen["threshold"]
                loud_frames += loud
                waited += 1
                if loud:
                    speech_started, quiet = True, 0
                elif speech_started:
                    quiet += 1
                if (speech_started and quiet >= END_SILENCE_FRAMES) or len(capture) >= MAX_CAPTURE_FRAMES:
                    break
                if not speech_started and waited >= WAIT_FOR_SPEECH_FRAMES:
                    print("Capture: no speech", flush=True)
                    capture = []
                    break
        finally:
            legacy.stop(rec)
            unduck(ducked)                       # music back before the command runs ("volume up" sees the real level)
        if activation == "follow-up":
            session["armed_until"] = 0.0         # one answer per question
        if activation is None and tray.handsfree() and time.time() - opened < 1:
            print("Microphone not available, retrying in 3 s...", flush=True)
            time.sleep(3)                        # don't spin the CPU while the mic is missing
            continue
        if capture and (activation == "wake" or loud_frames >= 2):
            print(f"Capture: {len(capture) / 10:.1f}s via {activation}", flush=True)
            set_status("processing")
            yield b"".join(capture), activation, len(capture) >= MAX_CAPTURE_FRAMES


def respond(agent, audio, activation, hit_limit):
    started = time.perf_counter()
    text, candidates = transcribe_best(audio, activation)
    print(f"Candidates: {candidates}", flush=True)
    print(f"Transcript: {text!r} [{time.perf_counter() - started:.1f}s]", flush=True)
    if not text and hit_limit:                   # 15 s of "speech" with no words: the room got louder
        print(f"Constant noise, recalibrating. New threshold {legacy.calibrate():.0f}", flush=True)
    command = command_from_transcript(text, activation)
    if not command:
        if activation == "wake" and re.search(r"\bjarvis\b", text, re.I):
            legacy.say("Yes?")                   # only the wake phrase was said: wait for the command
            session["armed_until"] = time.time() + FOLLOW_UP_SECONDS
        else:
            print("Capture ignored: no command", flush=True)
        return
    reply = handle(agent, command)
    legacy.show(text, reply + ("\n" + assistant.state["detail"] if assistant.state["detail"] else ""))
    if matched(command) == ["keys"] and not reply.startswith("I "):
        print(f"Needle: {reply}", flush=True)
        legacy.beep(wait=True)
    else:
        if not assistant.state["spoken"]:        # streamed answers were already spoken sentence by sentence
            legacy.say(reply)
    # Needle is waiting on an answer ("Which one?", "Should I shut down? Say yes to confirm."): take it
    # without the wake phrase. The question expires with that window, counted from when Needle stopped talking.
    if assistant.state["pending"]:
        session["armed_until"] = time.time() + FOLLOW_UP_SECONDS
        assistant.state["pending"] = (assistant.state["pending"][0], session["armed_until"])
    else:
        session["armed_until"] = 0.0


def main():
    lock = open(LOCK_FILE, "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)        # two copies would both act on every command
    except OSError:
        print("Needle is already running (as the background service). Not starting a second copy.", flush=True)
        return
    legacy._voice = legacy.load_voice() if os.path.exists(legacy.VOICE) else None   # no delay on first reply
    wake = LazyWake()
    legacy.prepare_mic()
    print(f"Mic: {legacy.mic_node() or 'system default'}", flush=True)
    print(f"Speech threshold {legacy.calibrate():.0f}", flush=True)
    agent = LazyAgent()                          # the Needle model loads only if a command needs it
    assistant.on_wait = legacy.say               # "One moment" while a slow request is worked out
    assistant.on_sentence = legacy.say           # speak each answer sentence as soon as it is written
    tray.start()
    legacy.alive()
    print('Click "Bro" in the top bar (or press Ctrl+Alt+Space) and speak. Hands-free mode: say "Hey Jarvis".',
          flush=True)
    for audio, activation, hit_limit in events(wake):
        try:
            respond(agent, audio, activation, hit_limit)
        except Exception:                        # one bad command must never stop the assistant
            traceback.print_exc()
            session["armed_until"] = 0.0
        legacy.alive()
        set_status("idle")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("Bye")
