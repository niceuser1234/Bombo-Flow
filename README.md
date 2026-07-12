# whisper-flow

A personal Wispr Flow clone for macOS: **hold Right-Option, speak, release** — polished text appears at your cursor in whatever app is focused.

- **Free to run**: speech-to-text happens locally on the M-series GPU (NVIDIA Parakeet via MLX, auto-detects German/English, <1 s per dictation)
- **Optional AI cleanup**: an LLM removes filler words and fixes punctuation — via OpenRouter (default: Gemini 2.5 Flash-Lite, ~$0.00006 per dictation) or the Anthropic API. Falls back to the raw transcript if offline.
- Lives in the menu bar: 🎤 idle · 🔴 recording · ✍️ processing · ⏳ loading · ⚠️ error

## The Mac app (recommended)

Build once, then it's a normal double-clickable app:

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
./packaging/build.sh
```

This produces `dist/WhisperFlow.app` and `dist/WhisperFlow.dmg` (drag-to-Applications installer). On first launch the app guides you through the permissions (see below) and downloads the STT model (~2.3 GB, one time).

**Signing note:** the build ad-hoc-signs by default, which makes macOS forget the app's permission grants on every rebuild. To keep grants stable across rebuilds, create a self-signed code-signing certificate named `WhisperFlow Dev` (Keychain Access → Certificate Assistant → Create a Certificate → type "Code Signing") — `build.sh` picks it up automatically. Opening the app on *another* Mac requires System Settings → Privacy & Security → "Open Anyway" once (the app is not notarized).

## Run from source (dev)

```sh
.venv/bin/python -m flow
```

## Permissions (one time)

macOS prompts for three permissions; they attach to the **host app** (WhisperFlow.app, or your terminal when running from source). The app checks at startup and offers to open the right Settings pane; there's also **Check permissions** in the menu.

| Permission | Needed for | Prompted when |
|---|---|---|
| Input Monitoring | detecting the hotkey | app start |
| Microphone | recording | first recording |
| Accessibility | the synthetic Cmd+V paste | first paste |

After granting Input Monitoring or Accessibility, quit and reopen the app.

## Enable AI cleanup

Menu → **Set API key…** — the key is stored in the macOS Keychain, never in a file. Get a key at [openrouter.ai](https://openrouter.ai/settings/keys) (default provider) or use an Anthropic key with `provider = "anthropic"` in the config.

Equivalent terminal command:

```sh
security add-generic-password -s whisper-flow -a openrouter -w YOUR_KEY
```

The **AI cleanup** menu item toggles it per-session; `[cleanup] enabled` in the config sets the default. Without a key the app pastes the raw transcript (Parakeet's own punctuation is decent).

## Configuration

Menu → **Open config**. The app reads `~/Library/Application Support/whisper-flow/config.toml` (seeded on first launch); source runs read `config.toml` in the repo. Options: hotkey (`alt_r`/`cmd_r`/`ctrl_r`/`f13`), min/max recording duration, STT engine, cleanup provider/model/timeout, insert mode (`paste` or `type` for apps that block programmatic paste).

## Start at login

Menu → **Start at login** installs a LaunchAgent pointing at whatever is currently running (the .app or your dev checkout) and loads it.

## Troubleshooting

- Log file: `~/Library/Logs/whisper-flow.log` (also via menu → Open log)
- ⚠️ icon right after releasing the key + "recording is silent" in the log → Microphone permission missing
- Hotkey does nothing → Input Monitoring permission missing (menu → Check permissions)
- Text lands in clipboard but doesn't paste → Accessibility permission missing
- Some app pastes "Paste and Match Style" instead → keep holding the key shorter; the app waits for all modifiers to be released before pasting
- Want Whisper instead of Parakeet: `.venv/bin/pip install mlx-whisper`, set `engine = "whisper"` in the config

## Architecture

```
hotkey.py (pynput, Right-Option) ─► audio.py (sounddevice, 16 kHz)
    ─► stt.py (parakeet-mlx on GPU) ─► cleanup.py (LLM via OpenRouter/Anthropic, optional)
    ─► insert.py (clipboard + Cmd+V, clipboard restored afterwards)
menubar.py (rumps) shows state + settings; permissions.py checks TCC grants;
pipeline.py runs the worker thread; packaging/ builds WhisperFlow.app + DMG.
```
