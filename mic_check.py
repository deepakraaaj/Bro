"""Record a few known phrases in your real voice so speech recognition can be tuned on them.
Clips stay on this laptop in ~/.cache/needle-clips/.  Run:  needle mic_check.py
"""
import os
import subprocess
import wave

from voice_assistant import RATE, mic_node

OUT = os.path.expanduser("~/.cache/needle-clips")
PHRASES = ["Hey Needle, open brave browser",
           "Open brave browser",
           "Hey Needle, connect to CMF headphone",
           "Hey Needle, close everything except terminal",
           "Hey Needle, connect to internet which is best speed",
           "Hey Needle, open text editor"]
SECONDS = 4


def record(path, seconds):
    env = dict(os.environ)
    node = mic_node()
    if node:
        env["PIPEWIRE_NODE"] = node
    raw = subprocess.run(["arecord", "-q", "-t", "raw", "-f", "S16_LE", "-r", str(RATE), "-c", "1", "-d", str(seconds)],
                         capture_output=True, env=env).stdout
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(raw)


os.makedirs(OUT, exist_ok=True)
print(f"Mic: {mic_node() or 'system default'}\nSit where you normally sit and talk at your normal volume.\n")
input("First, 3 seconds of silence to measure the room. Press Enter and stay quiet...")
record(f"{OUT}/silence.wav", 3)
with open(f"{OUT}/labels.txt", "w") as labels:
    for i, phrase in enumerate(PHRASES):
        input(f'\n[{i + 1}/{len(PHRASES)}] Press Enter, then say:  "{phrase}"')
        print(f"  recording {SECONDS} s...")
        record(f"{OUT}/{i}.wav", SECONDS)
        labels.write(f"{i}\t{phrase}\n")
print(f"\nDone. Saved to {OUT}")
