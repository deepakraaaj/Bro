"""Regression test: every command below must keep giving the same reply and the same (faked) actions.

Run:      needle tests/test_commands.py
Re-record after an intended behaviour change:  needle tests/test_commands.py --record
"""
import json
import os
import re
import sys

import harness

SNAPSHOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "expected.json")

CASES = [
    "turn on bluetooth and connect to cmf",      # the real failure from the terminal
    "connect to internet which is best speed",
    "connect to galaxy's wifi",                     # named network (the real bug)
    "connect to officenet guest",                 # visible but not saved + secured -> needs password
    "connect to cafe free",                       # open network, not saved -> connects
    "connect to home fiber wifi",                     # saved but not in range
    "connect to starbucks wifi",                  # unknown
    "open settings", "open postman", "open camera", "open terminal", "open reddit",
    "open youtube in brave", "open github in firefox", "open my secret project folder",
    "connect to cmf headphone",
    "open text editor",
    "log the videos that are playing in my youtube",
    "log the videos that are playing",            # duplicate -> not logged twice
    "connect to cms headphone",                   # misheard name
    "connect my oneplus buds",                    # Nord Buds (has 'buds' + oneplus) vs Bullets
    "connect my oneplus",                         # ambiguous -> asks
    "bullets",                                    # answer to the question
    "connect headphones",                         # no name -> asks which
    "disconnect bluetooth",
    "increase volume", "increase volume by 20", "set volume to 150", "turn the volume down", "mute", "unmute",
    "brightness 0", "make the screen dimmer brightness",
    "set a timer for half an hour", "remind me in five minutes to call mom", "set a timer",
    "what is 10 divided by 0", "what is 9 ** 9 ** 9", "what is 15 percent of 200",
    "shut down", "yes",                           # confirmed
    "restart", "no",                              # cancelled
    "connect cmf headphone and open text editor", # two commands
    "search tom and jerry on youtube",            # 'and' inside a search, not split
    "open youtube in brave", "what time is it", "what's the battery", "turn off wifi",
    "pause", "next video", "", "what is the capital of Japan",
    # --- interactive questions ---
    "open browser", "the second one",
    "open editor", "cancel",
    "open music", "spotify",
    "open a browser", "any",
    "connect to officenet", "the first one",
    "connect headphones", "the last one",
    # --- closing apps ---
    "close all unwanted tabs and keep only terminal open", "keep brave", "yes",
    "close everything except vs code", "no",
    "close brave",
    "close chrome and open text editor",
    "close", "files",
    "close firefox",
    "close spotify music thing",
    "quit everything", "yes",
    # --- keyboard and mouse ---
    "type hello world", "type open brave and close everything", "press enter", "enter", "press control c",
    "copy and paste", "close tab", "new tab", "switch window", "close window", "down 3 times", "click",
    "double click", "right click", "scroll down", "scroll up 10", "move mouse left 200", "press banana",
    # --- youtube ---
    "play tom and jerry", "forward 30 seconds", "go back 15 seconds", "rewind two minutes", "play it",
    # --- incomplete commands and browser memory ---
    "open", "brave browser", "open youtube", "search", "lofi music", "type", "hello there", "press", "enter",
    "open", "cancel", "open firefox", "open github",
    # --- questions (web lookup faked) ---
    "what is the capital of Japan", "what's the weather", "what is the weather in chennai today", "who is ada lovelace",
    "why is my wifi slow", "how are you", "thank you needle",
    # --- do anything (agent faked) ---
    "what is eating my disk space", "rename the photos in downloads by date", "yes",
    "install vlc", "no", "delete the temp files in downloads", "open brave browser",
    "delete the temp files in downloads", "yes",      # second command fails -> stops, third never runs
    "rename the photos in downloads by date", "no",
    "wipe my home folder",                             # refused even before asking
    # ===== review findings (2026-11-10): each group reproduces one finding =====
    # 1: a "no" containing a yes-word is a no; only a whole "yes" confirms
    "shut down", "no okay never mind", "shut down", "okay cancel", "restart", "okay open chrome",
    "shut down", "yes please",
    "delete the temp files in downloads", "no don't do it", "delete the temp files in downloads", "okay wait",
    "close everything", "no not all of them", "close everything", "okay stop", "close everything", "all of them",
    # 2: a pending "Which one?" doesn't swallow the next command
    "close", "wait a second", "close", "open chrome", "connect headphones", "set a timer for 30 second",
    "open browser", "no okay",
    # 3: "do not ..." is not the command
    "do not close chrome", "do not turn off the wifi", "do not mute", "do not lock the screen", "do not disturb on",
    # 4: power only for the computer itself
    "restart chrome", "yes", "shut down chrome", "power off", "no", "go to sleep", "cancel",
    "turn off the computer", "no",
    # 5, 6, 20: one approved script, refusals, malformed brain replies (the refusal rules: test_safety.py)
    "organize my home folder", "clean up my downloads folder", "yes", "remove that file", "yes",
    "fix the blank answer",
    # 7: compound commands
    "turn off wifi and bluetooth", "turn off bluetooth and wifi", "search for volume and brightness tips",
    "open date and time settings", "restart and close everything", "no",
    # 8: close the named apps only
    "close chrome and leave the rest", "close brave but keep the others", "close chrome but not brave",
    "close chrome and brave", "close this window", "close the tab", "close chrome and banana",
    # 9: questions and look-alikes are not actions
    "is the wifi off", "is do not disturb on", "what notifications do i have", "what speaker should i buy",
    "turn off caps lock", "log out", "forward this email to john", "go back to the previous page", "skip the ad",
    "what is the time complexity of quicksort", "what time is it in tokyo", "what day is christmas",
    "what is the charge of an electron", "show me the time", "how much battery is left",
    # 10: volume words
    "turn off the volume", "turn the sound off", "volume zero", "volume to maximum", "volume", "turn the volume on",
    # 11: numbers
    "set volume to twenty five", "set volume to one hundred", "set volume to twelve", "volume up 20",
    "increase the volume 10 percent", "set brightness to seventy five", "brightness up 20",
    # 12: durations
    "set a timer for twenty five minutes", "set a timer for one hundred twenty seconds",
    "set a timer for half a minute", "set a timer for an hour and a half", "set a timer for 1 hour 30 minutes",
    "go back twenty five seconds", "forward a minute", "forward 1 minute 30", "remind me at 5 pm to call mom",
    "in ten minutes",
    # 13, 14: open
    "open google chrome", "open it", "cancel", "open downloads", "open music folder", "open sound settings",
    "open banana", "no", "open notepad", "yes", "start recording", "no", "open", "open firefox",
    # 15: searches keep place names
    "search google for best restaurants in chennai", "play walking on sunshine", "search google cats in firefox",
    # 16, 17: Wi-Fi and Bluetooth names
    "connect to home fiber 5g", "enable wifi", "restart the wifi", "shut down the wifi", "connect to the vpn",
    "disconnect oneplus", "the first one", "disconnect banana headphones", "disconnect cmf",
    # 19: built-in commands that used to reach the brain
    "five minute timer", "max volume", "stop the music", "join the officenet wifi", "play the next song",
    "calculate (0-8) to the power of 0.5",
    # --- real sentences from the 2026-11-11 voice history ---
    "open YouTube and brave browser and type and play Radhe Maa song",
    "open brave and search for Ridhima song", "search for Ridhima song",
    "google best biryani in chennai", "look up weather in chennai",
    "open brave browser and open YouTube in brave Browser", "close chrome and open text editor",
    # --- music drowning out short commands (2026-11-11 hands-free log) ---
    "pass", "boss", "pass the song", "boss is coming", "pass me the salt",
    "hisar vishwas", "open calculator", "thank you"
]

# 18: what is said when a system command fails: (sentence, command that fails, its output)
FAILURES = [
    ("volume up", "get-volume", ""),
    ("what's the battery", "upower", ""),
    ("connect to wifi", "connection show", ""),
    ("turn off wifi", "radio wifi off", "Error: NetworkManager is not running."),
    ("close chrome", "--signal=SIGTERM", "Failed to kill unit app-com.google.Chrome-1.scope: Unit not loaded."),
]


def stable(reply):
    """Replace the clock/date so the snapshot doesn't change every minute, and the home folder so it doesn't
    depend on who runs the tests."""
    reply = reply.replace(os.path.expanduser("~"), "~")
    reply = re.sub(r"It's \d+:\d+ [AP]M", "It's <time>", reply)
    return re.sub(r"Today is \w+, \d+ \w+ \d+", "Today is <date>", reply)


def main(record):
    results = []
    for said in CASES:
        reply, runs = harness.run(said)
        results.append({"said": said, "reply": stable(reply), "runs": [stable(r) for r in runs]})
    real_sh = harness.A.sh
    for said, broken, output in FAILURES:
        harness.A.state["pending"] = None
        harness.A.sh = lambda *cmd, timeout=20: output if broken in " ".join(cmd) else real_sh(*cmd, timeout=timeout)
        reply, runs = harness.run(said)
        results.append({"said": f"{said} [when '{broken}' fails]", "reply": stable(reply), "runs": runs})
    harness.A.sh = real_sh
    problems = [f"reached the real system: {b}" for b in harness.blocked]
    problems += [f"{r['said']!r} crashed: {r['reply']}" for r in results if "went wrong" in r["reply"]]
    if record:
        with open(SNAPSHOT, "w") as f:
            json.dump(results, f, indent=1)
        print(f"recorded {len(results)} commands")
    else:
        with open(SNAPSHOT) as f:
            expected = json.load(f)
        if len(expected) != len(results):
            problems.append(f"snapshot has {len(expected)} commands, the test has {len(results)}: re-record")
        for want, got in zip(expected, results):
            if want != got:
                problems.append(f"{got['said']!r}\n     expected: {want['reply']!r} {want['runs']}"
                                f"\n     got:      {got['reply']!r} {got['runs']}")
    for p in problems:
        print("FAIL", p)
    print(f"{len(results)} commands, {len(problems)} problems")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main("--record" in sys.argv))
