"""Safe test harness: runs assistant.handle() with every effect on the laptop faked.

Nothing run through this file can change the machine. The fakes below answer like the real system, and as a
second line of defence every real command, network request and keyboard/mouse event is hard-blocked: if code
reaches one without going through a fake, it is recorded in `blocked` and the tests fail.

Use:  reply, would_run = run("open brave browser")
"""
import json
import os
import sys
import tempfile
import time
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("CACTUS_TELEMETRY", "0")
import assistant as A  # noqa: E402

blocked = []            # attempts to reach the real system; must stay empty
would_run = []          # what the assistant would have done, for the current command


def _block(what):
    def stop(*args, **kwargs):
        blocked.append(f"{what}: {str(args[:1])[:120]}")
        raise AssertionError(f"BLOCKED: real {what}")
    return stop


_sub, _url = A.subprocess, A.urllib
A.subprocess = types.SimpleNamespace(run=_block("subprocess.run"), Popen=_block("subprocess.Popen"),
                                     TimeoutExpired=_sub.TimeoutExpired, DEVNULL=_sub.DEVNULL)
A.urllib = types.SimpleNamespace(parse=_url.parse, request=types.SimpleNamespace(
    Request=_url.request.Request, urlopen=_block("network request")))
A.control = types.SimpleNamespace(NoPermission=A.control.NoPermission, **{
    name: _block("keyboard/mouse " + name) for name in ("connect", "tap", "type_text", "click", "move", "scroll")})
_timer = lambda *a, **k: types.SimpleNamespace(start=lambda: None, daemon=True)   # timers never fire in tests
A.threading = types.SimpleNamespace(Thread=_timer, Timer=_timer)
A.time = types.SimpleNamespace(time=time.time, sleep=lambda s: None)

A.VIDEO_LOG = os.path.join(tempfile.gettempdir(), "needle-test-youtube-log.md")   # never the real log
if os.path.exists(A.VIDEO_LOG):
    os.remove(A.VIDEO_LOG)
A.LLM_MODEL = ""        # no local chat model in tests
_read_file = A.read_file
A.read_file = lambda p: {"brightness": "913", "max_brightness": "15360"}.get(os.path.basename(p)) or _read_file(p)
sim = {"bt_blocked": True, "volume": 0.59, "adapter_delay": 3}


def fake_running():
    apps = {a["name"]: a for a in A.installed_apps()}
    return [(apps["Brave Web Browser"], ("unit", "snap.brave.brave-x.scope")),
            (apps["Google Chrome"], ("unit", "app-com.google.Chrome-1.scope")),
            (apps["Files"], ("gtk", "org.gnome.Nautilus")),
            (apps["Visual Studio Code"], ("unit", "snap.code.code-y.scope"))]
A.running_apps = fake_running
def fake_fetch(url):
    would_run.append("FETCH " + url[:60])
    if "wttr.in" in url:
        return {"current_condition": [{"weatherDesc": [{"value": "Partly cloudy"}], "temp_C": "29", "FeelsLikeC": "33"}],
                "nearest_area": [{"areaName": [{"value": "Chennai"}]}]}
    if "duckduckgo" in url:
        return {"AbstractText": "Ada Lovelace was an English mathematician. She worked on the Analytical Engine. More."} if "lovelace" in url.lower() else {"AbstractText": ""}
    if "list=search" in url:
        return {"query": {"search": [{"title": "Capital of Japan"}] if "japan" in url.lower() else []}}
    if "page/summary" in url:
        return {"type": "standard", "extract": "The capital of Japan is Tokyo (pop. 14 million). It moved from Kyoto in 1868. Third sentence."}
A.fetch_json = fake_fetch
def fake_agent(request):
    would_run.append(f"BRAIN {request[:50]!r}")
    low = request.lower()
    if "rename" in low:
        return '{"plan": "Rename the 12 photos in Downloads to their capture date.", "commands": ["cd ~/Downloads && exiftool -d %Y-%m-%d.%%e \'-FileName<CreateDate\' *.jpg"]}'
    if "install" in low:
        return '{"say": "Installing VLC needs the admin password, which I cannot type."}'
    if "wipe" in low:
        return '{"plan": "Delete everything in your home folder.", "commands": ["rm -rf ~"]}'
    if "organize" in low:
        return '{"plan": "Tidy the home folder.", "commands": ["rm -rf ~/"]}'
    if "clean up" in low:
        return '{"plan": "Delete the temp files in Downloads.", "commands": ["cd ~/Downloads", "rm -f *.tmp"]}'
    if "remove" in low:
        return '{"plan": "Remove the file.", "commands": "sudo rm -rf /"}'
    if "blank" in low:
        return '{"say": ""}'
    if "delete" in low:
        return '{"plan": "Delete the 3 temp files in Downloads.", "commands": ["rm ~/Downloads/a.tmp", "false", "echo never"]}'
    return '{"say": "FAKE BRAIN ANSWER"}'


def fake_shell(script):
    """The approved commands arrive as one script; a line 'false' makes it fail there (set -e)."""
    would_run.append("SHELL " + script)
    return (False, "rm: cannot remove a.tmp") if "false" in script.splitlines() else (True, "")


A.run_shell = fake_shell
A.run_agent = fake_agent
A.inject = lambda action, *args: would_run.append(f"INPUT {action} {list(args)}")
A.first_youtube_video = lambda url: "https://www.youtube.com/watch?v=FAKE0000001"


def fake_sh(*cmd, timeout=20):
    c = " ".join(cmd)
    if c == "nmcli radio wifi": return "enabled"
    if "connection show" in c: return ("OFFICENET:802-11-wireless\nGalaxy A55 5G:802-11-wireless\nGalaxy A55 5G 1:802-11-wireless\n"
                                       "Home_Fiber:802-11-wireless\nWired:802-3-ethernet")
    if "-f SSID device wifi list" in c: return "OFFICENET\nOFFICENET\nGalaxy A55 5G\nOFFICENET_GUEST\nCafe Free\n"
    if "device wifi list" in c: return (" :02\\:00\\:00\\:00\\:01\\:01:OFFICENET:100:WPA2 WPA3\n*:02\\:00\\:00\\:00\\:01\\:02:OFFICENET:69:WPA2 WPA3\n"
        " :11\\:22\\:33\\:44\\:55\\:66:Galaxy A55 5G:100:WPA2 WPA3\n :AA\\:BB\\:CC\\:DD\\:EE\\:FF:OFFICENET_GUEST:95:WPA2 WPA3\n"
        " :AA\\:BB\\:CC\\:DD\\:EE\\:00:Cafe Free:80:\n :AA\\:BB\\:CC\\:DD\\:EE\\:01::70:WPA2")
    if sim["bt_blocked"] or sim["adapter_delay"] > 0:
        if c.startswith("bluetoothctl") and c != "bluetoothctl power on": return "No default controller available"
    if "devices Paired" in c: return ("Device 02:00:00:00:00:02 CMF Neckband Pro\nDevice 02:00:00:00:00:01 Airdopes 141 Elite ANC\n"
        "Device 02:00:00:00:00:03 Rockerz 430\nDevice 02:00:00:00:00:05 OnePlus Nord Buds 2r\nDevice 02:00:00:00:00:04 OnePlus Bullets Wireless Z2")
    if "bluetoothctl info" in c: return "Connected: no"
    if c == "bluetoothctl power on":
        would_run.append(c)
        if sim["bt_blocked"]: return "No default controller available"
        if sim["adapter_delay"] > 0: sim["adapter_delay"] -= 1; return "No default controller available"
        return "Changing power on succeeded"
    if c == "rfkill unblock bluetooth": would_run.append(c); sim["bt_blocked"] = False; return ""
    if "busctl --user --json=short list" in c: return json.dumps([{"name": ":1.1"}, {"name": "org.mpris.MediaPlayer2.brave.instance8148"}])
    if "get-property" in c and c.endswith("Metadata"):
        return json.dumps({"type": "a{sv}", "data": {"xesam:title": {"type": "s", "data": "Don't Stop Me Now (Live)"},
               "xesam:artist": {"type": "as", "data": ["Queen Official"]}, "xesam:url": {"type": "s", "data": "https://www.youtube.com/watch?v=abc"}}})
    if "get-property" in c and c.endswith("PlaybackStatus"): return json.dumps({"type": "s", "data": "Playing"})
    if "get-volume" in c: return f"Volume: {sim['volume']}"
    if c.startswith("xdg-mime"): return "org.gnome.TextEditor.desktop"
    if c.startswith("upower"): return "state: charging\npercentage: 90%"
    would_run.append(c)                                         # every state-changing command is only recorded
    if "device wifi connect" in c: return "Device 'wlp0s20f3' successfully activated with 'x'."
    if "bluetoothctl connect" in c: return "Connection successful"
    return ""

A.sh = fake_sh
A.launch = lambda cmd: would_run.append("LAUNCH " + " ".join(cmd))

_agent = A.LazyAgent()   # the real Needle model; it only picks tools, and every tool is faked


def run(text):
    """(reply, commands it would have run) for one spoken sentence. State carries over between calls."""
    would_run.clear()
    reply = A.handle(_agent, text)
    return reply, list(would_run)
