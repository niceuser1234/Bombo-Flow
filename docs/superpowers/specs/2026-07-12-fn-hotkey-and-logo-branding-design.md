# Design: fn-Hotkey + Bomboclat-Flow-Logo-Branding

Datum: 2026-07-12
Status: Approved (Design), bereit für Implementierungsplan

## Ziel

Zwei unabhängige Änderungen an Bomboclat Flow:

1. **Hold-to-Talk auf die fn-Taste** (unten links auf der Mac-Tastatur) umstellen — als
   neuer Default, während die bisherigen Tasten wählbar bleiben.
2. **Das Palm-Mic-Logo** als App-Icon und im App-Fenster (Sidebar-Brand, Favicon,
   Akzentfarbe) verankern.

Beide Stränge sind entkoppelt und können getrennt implementiert/getestet werden.

---

## Teil 1 — fn-Taste als Hotkey

### Problem

Der aktuelle Hotkey läuft über `pynput` (`flow/hotkey.py`, `KEY_MAP` → `HotkeyListener`).
**pynput erkennt die fn-Taste unter macOS nicht:** fn ist ein Hardware-Modifier und
sendet keinen normalen Key-Event, sondern setzt beim Drücken/Loslassen das
Secondary-Fn-Flag (`0x800000`) in einem `flagsChanged`-Event. Ein reiner
`config.toml`-Eintrag reicht daher nicht.

### Gewählter Ansatz (A): NSEvent Global Monitor auf `flagsChanged`

Neuer Listener-Typ, der über einen **AppKit `NSEvent` Global Monitor** auf
`NSEventMaskFlagsChanged` lauscht und das Fn-Flag-Bit auswertet. Läuft auf dem
bestehenden AppKit/rumps-Mainloop (kein eigener Thread), nutzt dieselbe
„Input Monitoring"-Permission wie der bestehende pynput-Pfad.

Verworfen: CGEventTap (eigener Runloop-Thread, cross-thread-fehleranfällig — das Projekt
hatte bereits einen MLX-Cross-Thread-Crash) und eine generische Tap-Vereinheitlichung
(unnötig große Umschreibung).

### Komponenten

**`flow/hotkey.py`**
- Neue Klasse `FnHotkeyListener` mit **identischem Interface** wie `HotkeyListener`:
  Konstruktor `(on_hold_start, on_hold_end)`, Methoden `start()` / `stop()`,
  internes `_held`-Flag für saubere Down/Up-Kanten (kein Doppel-Feuern bei Key-Repeat).
  - `start()`: registriert den Global Monitor via
    `NSEvent.addGlobalMonitorForEventsMatchingMask_handler_(NSEventMaskFlagsChanged, handler)`.
  - Handler prüft `event.modifierFlags() & NSEventModifierFlagFunction`:
    gesetzt und noch nicht gehalten → `on_hold_start`; nicht gesetzt und gehalten →
    `on_hold_end`. Ausnahmen werden geloggt (wie im bestehenden Listener), nicht geworfen.
  - `stop()`: `NSEvent.removeMonitor_(handle)`.
- Neue Factory `make_listener(key_name, on_hold_start, on_hold_end)`:
  - `key_name == "fn"` → `FnHotkeyListener`
  - sonst → bestehender `HotkeyListener(key_name, …)` (KEY_MAP unverändert)
- `KEY_MAP` und `HotkeyListener` bleiben unangetastet.

**`flow/__main__.py`**
- Zeile ~95: `HotkeyListener(cfg.hotkey, …)` → `make_listener(cfg.hotkey, …)`.
  Sonst keine Änderung an der Startsequenz.

**`config.toml`**
- `[hotkey] key = "fn"` als neuer Default.
- Kommentar aktualisieren: `"fn"` (Fn-Taste unten links, Standard) plus die
  bestehenden Optionen `alt_r`, `cmd_r`, `ctrl_r`, `f13`.

### UX-Hinweis (Plan, nicht Code-Kern)

macOS belegt fn/🌐 standardmäßig mit „Emoji-Picker / Eingabequelle wechseln". Beim
Halten kann das reinfunken. Beim ersten Start (oder in der README/Onboarding) ein
**einmaliger, nicht-blockierender Hinweis**:
„Systemeinstellungen → Tastatur → ‚🌐-Taste drücken für' → **Nichts tun**."
Der Global Monitor ist passiv und kann den Emoji-Picker technisch nicht schlucken —
deshalb der Hinweis statt eines Code-Workarounds.

### Grenzfälle

- **Key-Repeat / mehrfaches flagsChanged:** durch `_held`-Flag abgefangen.
- **fn + andere Modifier gleichzeitig:** Wir prüfen ausschließlich das Fn-Bit; andere
  Modifier-Änderungen im selben Event lösen kein Start/Stop aus, solange sich das
  Fn-Bit nicht ändert.
- **Permission fehlt:** Verhalten wie beim bestehenden Listener — Menubar-Hinweis auf
  „Input Monitoring" greift weiter (unverändert).

### Test

- Unit: `make_listener("fn", …)` liefert `FnHotkeyListener`; `make_listener("alt_r", …)`
  liefert `HotkeyListener`; unbekannter Key wirft `ValueError` (bestehendes Verhalten).
- Unit: `FnHotkeyListener` mit einem gefälschten Event-Objekt
  (`modifierFlags()` mit/ohne Fn-Bit) ruft `on_hold_start`/`on_hold_end` in korrekter
  Kanten-Reihenfolge und feuert bei wiederholtem „Fn-gesetzt" nicht doppelt.
- Manuell: fn halten → Aufnahme startet; loslassen → Transkript wird eingefügt.

---

## Teil 2 — App-Icon

### Ist-Zustand

`packaging/Flow.spec` (`icon=…/BomboclatFlow.icns`) verweist auf das aktuelle
🎤-Emoji-Icon, erzeugt von `packaging/make_icon.py`. Eine fertige Logo-Version liegt
bereits unter `Bombo Logo/BomboclatFlow.icns` (aus dem SVG gerendert).

Wichtig: `LSUIElement = True` → Menüleisten-Agent **ohne Dock-Icon**. Das App-Icon
erscheint in Finder, DMG und App-Umschalter, nicht im Dock.

### Änderungen

1. `Bombo Logo/BomboclatFlow.icns` → **nach `packaging/BomboclatFlow.icns` kopieren**
   (überschreibt das Emoji-Icon). `Flow.spec` bleibt unverändert und greift automatisch.
2. **`packaging/make_icon.py` von Emoji-Zeichnen auf SVG-Rendering umstellen**, damit das
   Icon reproduzierbar neu baubar ist (nicht „magisch aus dem Zip"):
   - Quelle: eine committete Icon-SVG in `packaging/` (siehe Punkt 3).
   - SVG → Master-PNG (1024×1024) via AppKit `NSImage`-aus-SVG-Daten **oder**
     `qlmanage`/`rsvg`-Fallback; anschließend die **bestehende** iconset-/`iconutil`-
     Pipeline (16…512 @1x/@2x) unverändert weiterverwenden.
3. Die saubere **Icon-SVG** (`bomboclat_flow_icon.svg`, das Palm-Mic auf cremefarbenem
   Rounded-Rect) nach `packaging/` committen als Rendering-Quelle.

Bewusst weggelassen (YAGNI): kein Dock-Icon-Handling (`LSUIElement`), kein Neubau des
Menüleisten-Icons (nicht im Scope).

### Test

- `.venv/bin/python packaging/make_icon.py` erzeugt eine `packaging/BomboclatFlow.icns`,
  die das Palm-Mic-Logo zeigt (visuelle Sichtprüfung + Datei existiert, > 0 Bytes,
  mehrere Auflösungen).
- Nach `packaging/build.sh`: „Bomboclat Flow.app" zeigt im Finder das Logo-Icon.

---

## Teil 3 — Logo im App-Fenster

### Ist-Zustand

`flow/ui/index.html` Sidebar `.brand` zeigt eine generische Waveform-SVG (5 Balken)
neben „Bomboclat Flow". Kein Favicon. Akzentfarbe in `flow/ui/style.css` ist Teal
(`--accent: #14595B` hell / `#4FA8A0` dunkel), nicht das Markengrün.

Markenfarben: Grün `#1F7A52` / `#3FAE7B`, Creme `#FFF3E4`, Dunkel `#22313F`.

### Änderungen

1. **Brand-Mark ersetzen** (`index.html`, `.brand-mark`): Waveform-SVG → **Palm-Mic-Logo**
   als inline SVG (aus `bomboclat_flow_icon.svg`, **ohne** den `<rect>`-Hintergrund — nur
   das grüne Palm-Mic-Symbol, damit es als kompaktes Mark neben dem Namen sitzt).
2. **Favicon** (`index.html`, `<head>`): `<link rel="icon" href="data:image/svg+xml,…">`
   mit dem Logo als inline `data:`-URI — kein externes Asset, funktioniert im Bundle.
3. **Akzentfarbe angleichen** (`style.css`, nur die `--accent*`-Variablen):
   - Hell: `--accent` / `--accent-ink` → `#1F7A52`, `--accent-hover` → dunkleres Grün
     (z. B. `#17603F`), `--accent-soft` → cremiger Grünton.
   - Dunkel: `--accent` → `#3FAE7B` (o. ä. gut lesbarer Grünton auf dunklem Grund).
   - Kontrast in Light/Dark gegenprüfen; nur Akzent ändern, restliche Palette bleibt.

Umfang: Nur Sidebar-Brand + Favicon + Akzentfarbe. Nav-Icons, Layout, Views,
sonstige Farben bleiben unangetastet. Kein Redesign.

### Test

- App-Fenster öffnen (Menubar → „Open Bomboclat Flow"): Sidebar zeigt das Palm-Mic-Logo,
  Browser-Tab/Fenster zeigt das Favicon, aktive Nav-/Akzentelemente sind grün.
- Light- und Dark-Mode je einmal sichtprüfen (Lesbarkeit der Akzentflächen).

---

## Nicht im Scope

- Menüleisten-Icon (bleibt 🎤).
- Waveform-Overlay-Branding.
- Signierung/Notarisierung des Bundles (separates, bestehendes TODO).
- fn-Event technisch „schlucken" (bewusst passiv; per Systemeinstellung gelöst).

## Reihenfolge

Teil 1, 2, 3 sind unabhängig. Empfohlene Reihenfolge: **1 (fn) → 2 (Icon) → 3 (UI)**,
da Teil 1 den funktionalen Kern der Anfrage trifft.
