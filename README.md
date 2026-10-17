# Bomboclat Flow

A personal Wispr Flow clone for macOS: **hold the Fn/🌐 key, speak, release** — polished text appears at your cursor in whatever app is focused. (Configurable under `[hotkey]` in `config.toml`: Right-Option, Right-Command, Right-Control and F13 also work.)

- **Free to run**: speech-to-text happens locally on the M-series GPU (Whisper large-v3-turbo via MLX, handles German/English mixed mid-sentence)
- **Optional AI cleanup**: an LLM removes filler words and fixes punctuation — via OpenRouter (default: Gemini 2.5 Flash-Lite, ~$0.00006 per dictation) or the Anthropic API. Falls back to the raw transcript if offline.
- **Recording overlay**: a floating waveform pill shows your voice level while you dictate
- **App window** (menu → Open Bomboclat Flow):
  - **Home** — your dictation history, grouped by day, searchable; entries are kept 14 days, 📌-pinned ones forever; copy/pin/delete per entry
  - **Insights** — words per minute, total words, fixes made by Flow, per-app usage, and a day-streak heatmap (all computed locally)
  - **Dictionary** — personal vocabulary; the cleanup LLM corrects phonetically-similar mis-transcriptions to your exact terms ("super base" → "Supabase") and auto-learns new proper nouns/neologisms from your dictations (✨, promoted after two sightings, spam-filtered)
- Lives in the menu bar: 🎤 idle · 🔴 recording · ✍️ processing · ⏳ loading · ⚠️ error
- **All data stays on this Mac** (SQLite in `~/Library/Application Support/whisper-flow/`); only the cleanup call leaves the machine, and only if you set an API key

## Upgrade notes

The default STT engine is now **Whisper large-v3-turbo** (better than Parakeet for mixed-language dictation, e.g. switching between German and English mid-sentence). On first launch with this engine, the app downloads ~0.5 GB from Hugging Face, which takes a few minutes depending on your connection; every launch after that is instant.

**Existing installs are unaffected** — your `config.toml` already has an explicit engine set, so upgrading does not trigger a surprise download. To switch an existing install to Whisper, set `engine = "whisper"` under `[stt]` in `config.toml` (packaged app: `~/Library/Application Support/whisper-flow/config.toml`; source checkout: `config.toml` in the repo root).

Parakeet remains available and is faster, but weaker on mixed-language dictation — set `engine = "parakeet"` under `[stt]` to keep using it.

## The Mac app (recommended)

Build once, then it's a normal double-clickable app:

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
./packaging/build.sh
```

This produces `dist/Bomboclat Flow.app` and `dist/BomboclatFlow.dmg`. On first launch the app guides you through the permissions (see below) and downloads the STT model (~2.3 GB, one time).

### Install from the DMG

1. Open `dist/BomboclatFlow.dmg`.
2. Drag **Bomboclat Flow.app** into **Applications**.
3. **Eject the DMG** before launching — do not run the app from the mounted disk image.
4. Start the app **only from `/Applications`** (Spotlight, Launchpad, or the Applications folder). Do not use `dist/Bomboclat Flow.app` for day-to-day use; a different install path can create duplicate or ineffective permission entries in System Settings.

### Code signing (keep permissions across rebuilds)

`build.sh` signs the app with a local certificate named **`BomboclatFlow Dev`** if it exists in your Keychain. Without it, the build falls back to **ad-hoc signing** and still completes — but macOS treats each rebuild as a new app and **forgets TCC permission grants** (Input Monitoring, Microphone, Accessibility) until you re-grant them.

**One-time setup** (Keychain Access):

1. Keychain Access → menu **Certificate Assistant** → **Create a Certificate…**
2. Name: `BomboclatFlow Dev` (exact spelling)
3. Identity type: **Code Signing**
4. Create, then re-run `./packaging/build.sh` — you should see `signing with identity: BomboclatFlow Dev`.

Why this matters: macOS binds privacy permissions to the app's **code signature**. Ad-hoc signing produces a new signature on every build; a stable dev certificate keeps the same identity so grants survive rebuilds.

More detail: [`packaging/README.md`](packaging/README.md). Opening the app on *another* Mac still requires System Settings → Privacy & Security → **Open Anyway** once (the app is not notarized).

## Run from source (dev)

```sh
.venv/bin/python -m flow
```

## Permissions (one time)

macOS prompts for three permissions; they attach to the **host app** (Bomboclat Flow.app, or your terminal when running from source). The app checks at startup and offers to open the right Settings pane; there's also **Check permissions** in the menu.

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

The **AI cleanup** menu item toggles it per-session; `[cleanup] enabled` in the config sets the default. Without a key the app pastes the raw transcript (and the dictionary/auto-learning stays inactive — Parakeet itself cannot be vocabulary-biased).

## Hands-free & rewrite

- **Hands-free**: double-tap fn to dictate without holding the key. Tap fn once more to finish; the text is cleaned and pasted as usual. A hands-free recording stops on its own after 10 minutes (`handsfree_max_s` under `[audio]`).
- **Rewrite**: select some text, hold **fn + Control**, say what to do with it ("make it more formal", "bullet points", "translate to English"), release — the selection is replaced with the rewritten text. **Cmd+Z** undoes it. Rewrite needs the fn hotkey and an API key (see above).
- System Settings → Keyboard → **"Press 🌐 key to"** must be set to **"Do Nothing"**, otherwise a double-tap opens the emoji picker or dictation instead.
- Known limitation: in VS Code with nothing selected, Cmd+C copies the whole current line, so a rewrite then inserts a rewritten copy of that line.

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
hotkey.py (pynput, Fn/🌐 by default) ─► audio.py (sounddevice, 16 kHz, RMS level)
    ─► stt.py (mlx-whisper on GPU, parakeet-mlx optional)
    ─► cleanup.py (LLM: vocab-aware cleanup + new-term detection, optional)
    ─► insert.py (clipboard + Cmd+V, clipboard restored afterwards)

overlay.py   floating waveform pill while recording
store.py     SQLite: history (14d retention, pinning), dictionary, insights
learn.py     anti-pollution filter for auto-learned dictionary terms
window.py    native window hosting the web UI (WKWebView + JSON bridge)
flow/ui/     the embedded UI: Home / Insights / Dictionary (offline, no CDN)
menubar.py   rumps menu bar; permissions.py TCC checks; pipeline.py worker
packaging/   PyInstaller spec + build.sh → Bomboclat Flow.app + DMG
```
