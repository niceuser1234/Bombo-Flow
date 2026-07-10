# whisper-flow

A personal Wispr Flow clone for macOS: **hold Right-Option, speak, release** — polished text appears at your cursor in whatever app is focused.

- **Free to run**: speech-to-text happens locally on the M-series GPU (NVIDIA Parakeet via MLX, auto-detects German/English, <1 s per dictation)
- **Optional AI cleanup**: Claude Haiku removes filler words and fixes punctuation (~$0.0007 per dictation, falls back to the raw transcript if offline)
- Lives in the menu bar: 🎤 idle · 🔴 recording · ✍️ processing · ⏳ loading · ⚠️ error

## Run it

```sh
cd ~/dev/whisper-flow
.venv/bin/python -m flow
```

## First-run permissions (one time)

macOS will prompt for three permissions the first time each feature is used. The permissions attach to the **host app** — Terminal while you run it from a terminal, **Python** (`Python.app` inside the python.org framework) once it runs via the LaunchAgent. Check them in **System Settings → Privacy & Security**:

| Permission | Needed for | Prompted when |
|---|---|---|
| Input Monitoring | detecting the hotkey | app start |
| Microphone | recording | first recording |
| Accessibility | the synthetic Cmd+V paste | first paste |

If a pane doesn't prompt, add the app manually with the “+” button. **Note:** upgrading/reinstalling Python invalidates these grants — re-grant after a Python update.

## Enable AI cleanup

Store your Anthropic API key in the Keychain (never written to disk in plain text):

```sh
security add-generic-password -s whisper-flow -a anthropic -w YOUR_API_KEY
```

Restart the app. The "AI cleanup" menu item toggles it per-session; `[cleanup] enabled` in `config.toml` sets the default. Without a key the app pastes the raw transcript (Parakeet's own punctuation is decent).

## Start at login

Click **Start at login** in the menu — it installs `launchagent/io.julius.whisperflow.plist` into `~/Library/LaunchAgents` and loads it. On the first launchd-run, approve the permission prompts again for "Python".

## Configuration

Edit `config.toml`: hotkey (`alt_r`/`cmd_r`/`ctrl_r`/`f13`), min/max recording duration, STT engine, cleanup model/timeout, insert mode (`paste` or `type` for apps that block programmatic paste).

## Troubleshooting

- Log file: `~/Library/Logs/whisper-flow.log` (also via menu → Open log)
- ⚠️ icon right after releasing the key + "recording is silent" in the log → Microphone permission missing for the host app
- Hotkey does nothing → Input Monitoring permission missing
- Text lands in clipboard but doesn't paste → Accessibility permission missing
- Some app pastes "Paste and Match Style" instead → keep holding the key shorter; the app waits for all modifiers to be released before pasting
- Want Whisper instead of Parakeet: `.venv/bin/pip install mlx-whisper`, set `engine = "whisper"` in config.toml

## Architecture

```
hotkey.py (pynput, Right-Option) ─► audio.py (sounddevice, 16 kHz)
    ─► stt.py (parakeet-mlx on GPU) ─► cleanup.py (Claude Haiku, optional)
    ─► insert.py (clipboard + Cmd+V, clipboard restored afterwards)
menubar.py (rumps) shows state; pipeline.py runs the worker thread.
```
