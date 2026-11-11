# Bro: hands-free voice control for Ubuntu

Bro lets you use an Ubuntu laptop by voice, without touching the keyboard. Click the **Bro** button in the top bar (or say **"Hey Jarvis"** in hands-free mode) and speak:

> "open brave and play Radhe Maa song" · "pause" · "forward 30 seconds" · "connect to my CMF headphones" · "turn off wifi" · "type good morning, press enter" · "close everything except terminal" · "what is a black hole?"

It was built for the case where your hands or your keyboard can't be used. It runs on a normal laptop CPU with no GPU, and is private by default: the microphone stays closed until you ask.

Internally the project is called **Needle**, after the small tool-calling model it uses. That is why the service and some file names say `needle`.

---

## Contents

- [What it can do](#what-it-can-do)
- [How it works](#how-it-works)
- [Privacy](#privacy)
- [Requirements](#requirements)
- [Install](#install)
- [Use](#use)
- [Configuration](#configuration)
- [Tests](#tests)
- [Project layout](#project-layout)
- [Troubleshooting](#troubleshooting)
- [Known limitations](#known-limitations)
- [License](#license)

---

## What it can do

| Area | Examples |
|---|---|
| **Apps** | "open text editor", "open browser" (asks which one), "close chrome", "close everything except terminal" (asks first) |
| **Web and YouTube** | "open youtube in brave", "search for cricket score", "play tom and jerry" (plays the top result), "google best biryani in chennai" |
| **Media** | "pause", "play", "next video", "forward 30 seconds", "go back 1 minute 30 seconds", "log the video that's playing" |
| **Wi-Fi** | "connect to the internet with the best speed", "connect to office wifi", "turn off wifi" |
| **Bluetooth** | "connect to my CMF headphones", "disconnect bluetooth", "turn on bluetooth" |
| **System** | "volume 40", "increase volume by 20", "mute", "brightness up", "do not disturb on", "lock the screen", "shut down" (asks first) |
| **Keyboard and mouse** | "type hello world", "press enter", "copy", "paste", "new tab", "close tab", "scroll down", "click", "right click" |
| **Timers and info** | "set a timer for 25 minutes", "what time is it", "what's the battery", "what is 15 percent of 200" |
| **Questions** | "what is the capital of Japan?", "how do I take a screenshot on Ubuntu?" (answered by a local model on the laptop) |
| **Anything else on Linux** | "rename the photos in Downloads by date", "install VLC": it says what it will do, shows the exact commands, and runs them only after you say **"yes"** (needs the optional cloud agent, see Requirements) |

It understands natural, imperfect speech. Names are matched against what is actually on your machine: installed apps, saved Wi-Fi networks and paired Bluetooth devices. So "connect to cms headphone" still finds "CMF Neckband Pro". When more than one thing matches, it asks "Which one?", and you can answer by name, "the second one" or "cancel".

Chains work too: "open brave and search for lofi music" opens one new tab in Brave, not a new window.

## How it works

```
 click "Bro" / Ctrl+Alt+Space            hands-free: offline "Hey Jarvis" detector
              \                          /
               mic opens (music is turned down while you speak)
                              |
             Google speech-to-text (Indian + US English)
                              |
     router (assistant.py): rules handle most commands instantly, no AI involved
       |                  |                      |                         |
  built-in action    Needle 26M model     local LLM (Ollama,         cloud agent (optional):
  (apps, Wi-Fi,      picks an action      Qwen3 4B): questions        plans shell commands,
   media, keys...)   for odd phrasing      and chat                    runs them only after "yes"
       \__________________|______________________|_________________________/
                                       |
                    spoken reply (Piper neural voice) + on-screen bubble
```

| Part | What it uses | Where it runs |
|---|---|---|
| Wake word | [openWakeWord](https://github.com/dscripka/openWakeWord) "Hey Jarvis" | laptop |
| Speech-to-text | Google's web speech service, `en-IN` and `en-US` alternatives, the best command fit wins | Google |
| Commands | rule-based router plus [Cactus Needle](https://huggingface.co/Cactus-Compute/needle3) (26M parameters) for ambiguous phrasing | laptop |
| Questions | [Ollama](https://ollama.com) with `qwen3:4b-instruct-2507-q4_K_M`, answers spoken sentence by sentence | laptop |
| Changes to the machine | a coding-agent CLI in read-only mode proposes commands; Bro runs them after a spoken "yes" | cloud (optional) |
| Voice | [Piper](https://github.com/OHF-Voice/piper1-gpl) `en_US-lessac-medium` | laptop |
| Keyboard and mouse | the desktop's RemoteDesktop portal (no root, one-time permission prompt) | laptop |
| Top-bar button | StatusNotifierItem over D-Bus (the same mechanism as CopyQ's or Slack's icon) | laptop |

**Resource use** on an Intel i5-13420H with 14 GB of RAM: about 0.2% of one core and 180–250 MB of RAM while idle. The Qwen3 4B model takes about 3 GB more only while you're asking questions, and unloads after 5 idle minutes.

**Speed:** built-in commands run about 2 s after you stop speaking: 1 s to be sure you've finished, then about 0.8 s of speech-to-text. Answers to questions start about 3 s after you stop speaking.

## Privacy

- **Off until asked.** By default the microphone is closed. It opens when you click Bro or press the shortcut, records one command, and closes again.
- **Hands-free is opt-in.** Tick "Hands-free" in the button's menu to have it listen continuously for "Hey Jarvis". The wake word is detected on the laptop; nothing is sent anywhere until you say it.
- **Sent to Google:** the audio of each command you give, for speech-to-text, over HTTPS. Nothing is stored on disk.
- **Stays on the laptop:** questions and chat (local Ollama model), the wake word, the voice, and all built-in actions.
- **Sent to the cloud agent, only if you use it:** requests to change the machine ("rename my photos...").
- **Never the earphone mic.** Bro always records from the laptop's built-in mic, so Bluetooth earphones stay in music quality instead of switching to call mode.

## Requirements

- **Ubuntu 24.04 or newer with GNOME on Wayland.** Developed and tested on Ubuntu 26.04 with GNOME 50 and PipeWire. Other GNOME-based distributions should work; other desktops need a StatusNotifierItem tray and the RemoteDesktop portal.
- **Tools that ship with Ubuntu:** `arecord`/`aplay`, `wpctl`, `nmcli`, `bluetoothctl`, `notify-send`, `gtk-launch`.
- **Python 3.12+**. Tested on 3.14.
- **Internet** for speech-to-text.
- **[Ollama](https://ollama.com)**, for answering questions. Optional: without it Bro still runs every command.
- **A coding-agent CLI**, logged in, for "do anything" requests: see `agent_binary()` and `AGENT_MODEL` in `assistant.py`. Optional: without it those requests get "I can't do that locally yet".
- **About 4 GB of free disk space** for the models, and **about 3 GB of free RAM** while the question model is loaded.

## Install

```bash
git clone https://github.com/deepakraaaj/Bro.git ~/Bro
cd ~/Bro

# 1. Python environment
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

# 2. Voice (63 MB)
mkdir -p ~/.local/share/needle-voices
base=https://huggingface.co/rhasspy/piper-voices/resolve/main/en/en_US/lessac/medium
curl -L -o ~/.local/share/needle-voices/en_US-lessac-medium.onnx      $base/en_US-lessac-medium.onnx
curl -L -o ~/.local/share/needle-voices/en_US-lessac-medium.onnx.json $base/en_US-lessac-medium.onnx.json

# 3. "Hey Jarvis" wake-word model (about 4 MB)
.venv/bin/python -c "import openwakeword.utils as u; u.download_models(['hey_jarvis'])"

# 4. Needle command model (about 35 MB, downloaded once from Hugging Face)
.venv/bin/python -c "import cactus; cactus.Cactus(tools=[], stateless=True).__enter__()"

# 5. Local model for questions (2.5 GB), optional
ollama pull qwen3:4b-instruct-2507-q4_K_M
```

### Keyboard shortcut (Ctrl+Alt+Space)

The shortcut does the same as clicking Bro.

```bash
key=/org/gnome/settings-daemon/plugins/media-keys/custom-keybindings/bro/
gsettings set org.gnome.settings-daemon.plugins.media-keys custom-keybindings "['$key']"
gsettings set org.gnome.settings-daemon.plugins.media-keys.custom-keybinding:$key name 'Bro: listen'
gsettings set org.gnome.settings-daemon.plugins.media-keys.custom-keybinding:$key command "sh -c 'touch \"\$XDG_RUNTIME_DIR/needle-listen\"'"
gsettings set org.gnome.settings-daemon.plugins.media-keys.custom-keybinding:$key binding '<Control><Alt>space'
```

If you already have custom shortcuts, add `'$key'` to your existing list instead of replacing it: check with `gsettings get org.gnome.settings-daemon.plugins.media-keys custom-keybindings`.

### Start at login

```bash
mkdir -p ~/.config/systemd/user
cp systemd/needle.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now needle.service
```

The **Bro** button appears in the top bar a few seconds later. The service restarts itself if it crashes or hangs.

| Task | Command |
|---|---|
| Watch what it hears and does | `journalctl --user -u needle -f` |
| Restart | `systemctl --user restart needle` |
| Stop it and turn off autostart | `systemctl --user disable --now needle` |

To try it without the service, run `HF_HUB_OFFLINE=1 .venv/bin/python voice_assistant_v2.py` from a terminal. Only one copy can run at a time.

### Optional: keep Bluetooth earphones in music quality

Bro itself never opens the earphone mic. Other apps (a browser tab with mic access, a call app) can still switch earphones into call mode. To stop Ubuntu from doing that automatically:

```bash
wpctl settings --save bluetooth.autoswitch-to-headset-profile false
```

The trade-off: calls then use the laptop mic while you still hear through the earphones. Undo it with `... true`.

## Use

| Do | What happens |
|---|---|
| **Left-click Bro** (or Ctrl+Alt+Space) | A beep, then say one command. The mic closes when you finish. |
| **Right-click Bro** | Menu with **Listen now** and **Hands-free**. |
| **Hands-free on**, say "**Hey Jarvis**, ..." | It listens all the time for the wake word, then for your command (pauses up to 5 s are fine). |
| Bro asks something ("Which one?", "Should I shut down the computer?") | Just answer within 10 seconds: no click or wake word needed. |

**The first time** you ask it to type, press keys or click, GNOME asks once whether to allow remote control. Choose **Share** (or **Allow**). The permission is saved in `~/.config/needle/input-token`.

**Safety rules for actions:**
- Shutdown, restart, closing several apps and every shell command need a plain **"yes"**.
- Anything with "no", "not", "cancel", "wait" or "don't" cancels.
- Shell commands that use `sudo`, delete or overwrite your home folder or the system, format disks, or pipe downloads into a shell are refused outright.
- A command that deletes or overwrites files starts with a warning: "Careful, this deletes or overwrites files."

**Tips:**
- If music is playing from the speakers, Bro turns it down while you speak. Earphones avoid the problem entirely.
- Saying "type ..." types the rest of the sentence literally, so "type open brave and close everything" is typed, not executed.

## Configuration

| Setting | Where | Default |
|---|---|---|
| Local question model | env `NEEDLE_LLM`, or `LLM_MODEL` in `assistant.py` (`""` turns it off) | `qwen3:4b-instruct-2507-q4_K_M` |
| Model for "do anything" | `AGENT_MODEL` in `assistant.py` | `haiku` |
| Speech language | `LANGUAGE` in `voice_assistant.py`; `en-US` is always tried as well | `en-IN` |
| Wake word sensitivity | `WAKE_THRESHOLD` in `voice_assistant_v2.py` (higher means fewer false wakes) | `0.4` |
| Answer window after a question | `FOLLOW_UP_SECONDS` in `voice_assistant_v2.py` | 10 s |
| Built-in mic gain | env `NEEDLE_MIC_GAIN` (`""` leaves it alone) | `50%` |
| Voice | `VOICE` in `voice_assistant.py` (any Piper `.onnx` voice) | `en_US-lessac-medium` |
| Hands-free on/off | the button's menu, remembered in `~/.config/needle/handsfree` | off |
| Video log file | `VIDEO_LOG` in `assistant.py` | `~/Documents/needle-youtube-log.md` |

## Tests

Everything that touches the system goes through a few functions (`sh`, `launch`, `inject`, `fetch_json`, `run_agent`, `run_shell`). `tests/harness.py` fakes all of them, simulating Wi-Fi networks, Bluetooth devices, a media player and running apps. It also hard-blocks any real command, network request or key press, so **running the tests can never change your machine**.

```bash
cd tests
../.venv/bin/python test_commands.py    # 282 spoken commands and dialogues against a recorded snapshot
../.venv/bin/python test_safety.py      # 131 checks of the shell-command safety filter
```

After an intended behaviour change, re-record with `test_commands.py --record` and review the diff of `tests/expected.json`.

## Project layout

| Path | Purpose |
|---|---|
| `voice_assistant_v2.py` | Main loop: Bro button and shortcut, hands-free wake word, capture, speech-to-text, reply. Run this. |
| `voice_assistant.py` | Shared audio pieces used by v2: mic selection, loudness gate, Google client, Piper voice, beep, watchdog ping. |
| `assistant.py` | The router and every action: apps, web, media, Wi-Fi, Bluetooth, volume, keys, timers, questions, "do anything". |
| `needle_tray.py` | The Bro top-bar button: StatusNotifierItem and menu over D-Bus. |
| `control.py` | Keyboard and mouse input through the RemoteDesktop portal. |
| `mic_check.py` | Records a few known phrases in your voice, for tuning speech recognition. |
| `systemd/needle.service` | User service: start at login, restart on crash or hang. |
| `tests/` | Safe test harness, command snapshot and safety checks. |

## Troubleshooting

| Problem | Fix |
|---|---|
| **No Bro button** | Make sure the AppIndicator extension is on (`gnome-extensions list --enabled` should show `ubuntu-appindicators@ubuntu.com`) and the service is running (`systemctl --user status needle`). |
| **It hears the wrong words** | Look at `journalctl --user -u needle -f`: the `Candidates:` line shows what Google heard. Speak towards the laptop; turn music down or use earphones. |
| **Commands are ignored in hands-free mode** | The log shows `Activate: wake` when "Hey Jarvis" is detected. If it never appears, lower `WAKE_THRESHOLD` a little. If it fires on its own, raise it. |
| **"I need permission to control the keyboard and mouse"** | Click **Share** in the GNOME dialog. If you dismissed it, delete `~/.config/needle/input-token` and try again. |
| **"Can't reach Google speech-to-text"** | Check the internet connection. Commands need it; the wake word and the local question model don't. |
| **Questions get "My language model isn't running"** | Start Ollama (`systemctl status ollama`) and pull the model (see Install). |
| **Earphones sound like a phone call** | Another app has the earphone mic open. See "Optional: keep Bluetooth earphones in music quality". |
| **It keeps recording for 15 seconds** | Constant noise is louder than the speech threshold. Bro recalibrates after such a capture; check the `Speech threshold` line in the log. |

## Known limitations

- **Speech-to-text needs internet** and sends your command audio to Google. Fully offline models tested here misheard Indian English too often.
- **Questions take about 3 s.** A 4B model on a laptop CPU writes about 12 words per second. Smaller models were faster but noticeably less accurate.
- **The login screen and lock screen need a keyboard**: Bro starts only after you log in. For a fully keyboard-free setup, use GNOME's on-screen keyboard or turn on automatic login.
- **Mouse control is relative** ("move mouse left 200"); there is no "click the Save button" yet. Keyboard shortcuts are usually quicker.
- **Single browser tabs can't be closed by name**; "close tab" closes the current one.
- **The shell-command filter is a safety net, not a guarantee.** Always listen to the plan before saying "yes".

## License

[MIT](LICENSE) © 2026 Deepakraj B
