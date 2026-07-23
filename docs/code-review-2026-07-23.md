# Code-Review Bomboclat Flow — 23.07.2026

Vollständige manuelle Durchsicht aller Quelldateien in `flow/` (~2.900 Zeilen Python),
`flow/ui/` (JS/HTML/CSS) und `packaging/`. Stand: Commit `a099b84`.

**Testlage:** `108 passed in 4.91s` — die vorhandene Suite ist grün. Keiner der unten
beschriebenen Fehler wird von ihr abgedeckt; das sind alles Lücken in der
Testabdeckung, keine Regressionen.

Legende: **K** = kritisch (Datenverlust/Absturz), **H** = hoch (Funktion bricht still),
**M** = mittel, **N** = niedrig.

---

## Stand der Umsetzung (Branch `code-review-fixes`)

21 von 23 Findings sind behoben, in 6 atomaren Commits. Suite: **146 passed**
(vorher 108) — 38 neue Tests, alle als Regressionsschutz für die Fixes unten.

| Behoben | Offen (bewusst) |
| --- | --- |
| K-1, K-2, K-3, N-1 — `insert.py` (Commit `43c8eee`) | **N-6 Signierung/Notarisierung** — braucht einen Apple Developer Account, keine Code-Änderung |
| H-1, H-2, N-2 — Hotkey/Monitore (`69e66bf`) | **N-9 Query-Limits** — bei 24 KB DB kein reales Problem; sinnvoll erst mit `LIMIT`+Pagination in der UI |
| H-3, M-1, M-2, M-3, M-4, N-4 — Audio/Pipeline (`1da26fd`) | |
| H-4, H-5, M-5, N-5 — Sicherheit/Privatsphäre (`9298948`) | |
| H-6, H-7, M-6 — Wörterbuch-Lernen (`2b9cd46`) | |
| M-7, N-8, N-10 — Cleanup + Kleinkram (`debb8d8`) | |

Drei Stellen mit bewusst geänderter Semantik — bitte gegenprüfen:

1. **Startton (M-4)** — erst vor `recorder.start()` gezogen, nach Praxistest
   wieder zurückgestellt. Das Öffnen des Eingabegeräts konfiguriert CoreAudio um
   und schneidet einen laufenden Ton ab; da 0,14 s der 0,23 s reiner
   Release-Ausklang sind, war das deutlich hörbar. Endstand: Stream zuerst,
   Chime danach, und der Recorder verwirft die ersten 0,28 s der Aufnahme
   (`Recorder.mute_for`, Länge aus dem WAV-Header). Damit bleibt der Ton
   vollständig, landet trotzdem nicht im Transkript, und die Bedeutung „Aufnahme
   läuft" stimmt wieder — bei fehlgeschlagenem Start schweigt er.
2. **Promotion ins Wörterbuch (H-6/H-7)** ist nicht mehr pauschal `PROMOTE_AT = 1`.
   Begriffe mit markanter Form (`n8n`, `LangGraph`, `API`) kommen weiter beim
   ersten Hören rein; gewöhnlich aussehende Wörter brauchen wieder zwei
   Sichtungen. Zusätzlich beeinflussen automatisch gelernte Begriffe den
   Whisper-`initial_prompt` erst, wenn sie ein zweites Mal aufgetaucht sind —
   manuell angelegte sofort.
3. **`insert.py` schluckt Fehler jetzt leise** statt sie in die Pipeline zu
   werfen: Das Diktat steht dank M-3 vorher schon in der History, und ein
   `set_state("error")` mit verlorenem Text war das schlechtere Ergebnis.

Zwei bestehende Tests kodierten das fehlerhafte Verhalten und wurden umgeschrieben:
`test_press_within_debounce_of_release_is_swallowed` (H-2) und
`test_candidate_promoted_to_dictionary_after_single_sighting` (H-6).

---

## 1. Zwischenablage & Text-Einfügen (`flow/insert.py`)

### K-1 — Zwischenablage wird unwiederbringlich zerstört, wenn sie kein Text war
[insert.py:104-135](../flow/insert.py#L104-L135)

`save_and_set()` liest `pb.stringForType_(NSPasteboardTypeString)`. Enthält die
Zwischenablage ein Bild, eine Datei oder PDF-Daten, liefert das `None`. Direkt danach
läuft `pb.clearContents()` — der Bildinhalt ist weg. In `restore()` steht dann:

```python
if old is not None and pb.changeCount() == our_change:
```

`old` ist `None`, also wird **nie** wiederhergestellt. Ergebnis: Der Screenshot, den
der Nutzer kopiert hatte, ist zerstört, und stattdessen liegt der Diktattext dauerhaft
in der Zwischenablage.

**Fix:** Statt nur des String-Typs die vollständigen Pasteboard-Items sichern
(`pb.pasteboardItems()` mit allen `types()` je Item) und beim Restore zurückschreiben.
Minimallösung: `old is None` als "Zwischenablage danach leeren" behandeln, statt den
Diktattext liegen zu lassen.

### K-2 — Nach Timeout läuft der verworfene Block später doch und überschreibt die Zwischenablage
[insert.py:98-101](../flow/insert.py#L98-L101), [insert.py:112-118](../flow/insert.py#L112-L118)

`_on_main` stellt den Block per `AppHelper.callAfter(run)` in die Main-Thread-Queue und
wartet 1 s. Läuft der Timeout ab, wird `TimeoutError` geworfen — **der Block bleibt
aber in der Queue**. Sobald der Main Thread wieder frei ist (Modal-Alert geschlossen,
WKWebView fertig geladen, Modell-Ladehitch vorbei), führt er `save_and_set` aus:
`clearContents()` + `setString_forType_(text)`. Es wird kein Cmd+V gepostet und
`restore()` läuft nie — die Zwischenablage des Nutzers ist still zerstört und durch
Text ersetzt, der nirgends eingefügt wurde. Derselbe Zombie-Block kann außerdem in die
`changeCount()`-Buchhaltung des *nächsten* Diktats hineinlaufen.

**Fix:** Ein Cancel-Flag (`threading.Event`), das `run()` als Erstes prüft und bei
gesetztem Flag sofort zurückkehrt.

### K-3 — `IndexError` statt der echten Ausnahme; Diktat geht komplett verloren
[insert.py:92-101](../flow/insert.py#L92-L101)

```python
def run():
    try:
        result.append(func())
    finally:
        done.set()
```

Wirft `func()`, wird nichts angehängt, `finally` setzt trotzdem `done`, `done.wait()`
liefert `True` — und `return result[0]` wirft `IndexError: list index out of range`.
`_paste_text` fängt nur `TimeoutError`, also propagiert der `IndexError` über
`insert_text` → `Pipeline._process` → `_worker`. Folge: Text nicht eingefügt, **nicht
in die History geschrieben** (siehe M-3), `set_state("error")`, und die eigentliche
Ursache steckt hinter einem irreführenden Stacktrace. Die Ausnahme aus `run()` wird
nirgends geloggt.

**Fix:** Ausnahme in `run()` einfangen und über eine zweite Liste transportieren, dann
im Aufrufer erneut werfen (`raise exc[0]`), plus `log.exception` in `run()`.

### N-1 — `_type_text` verstümmelt Nicht-BMP-Zeichen
[insert.py:147-155](../flow/insert.py#L147-L155)

`CGEventKeyboardSetUnicodeString(ev, len(chunk), chunk)` erwartet die Länge in
**UTF-16-Einheiten**, `len(chunk)` liefert Python-Codepoints. Emoji und andere
Nicht-BMP-Zeichen zählen 2 Einheiten → der Rest des Chunks wird abgeschnitten. Das
Chunking bei 20 Codepoints kann außerdem Surrogatpaare zerschneiden. Betrifft nur
`insert_mode = "type"`.

**Fix:** `len(chunk.encode("utf-16-le")) // 2` als Länge übergeben.

---

## 2. Hotkey (`flow/hotkey.py`, `flow/menubar.py`)

### H-1 — Fn-Hotkey funktioniert nicht, solange Bomboclat Flow selbst im Vordergrund ist
[hotkey.py:166-168](../flow/hotkey.py#L166-L168), [menubar.py:382-386](../flow/menubar.py#L382-L386)

`NSEvent.addGlobalMonitorForEventsMatchingMask_handler_` liefert per Definition **keine**
Events, die an die eigene App gehen ("this method monitors events sent to other
applications"). Die App läuft aber als `NSApplicationActivationPolicyRegular`, und
`_show_initial_window` ruft beim Start `activateIgnoringOtherApps_(True)` +
`makeKeyAndOrderFront_` — Flow ist nach dem Start also selbst frontmost. Der erste
Fn-Druck nach dem Start macht **gar nichts**, bis der Nutzer in eine andere App klickt.
Der pynput-Pfad ist nicht betroffen (Event-Tap sieht auch eigene Events).

Dasselbe trifft den Notausstieg Cmd+Alt+Shift+Q ([menubar.py:349](../flow/menubar.py#L349)):
Er greift ausgerechnet dann nicht, wenn Flow selbst vorne ist.

**Fix:** Zusätzlich `addLocalMonitorForEventsMatchingMask_handler_` registrieren (Handler
muss das Event zurückgeben) und beide Monitore in `stop()` entfernen.

### H-2 — Debounce verschluckt einen Druck innerhalb von 50 ms nach dem Loslassen
[hotkey.py:184-192](../flow/hotkey.py#L184-L192)

```python
if fn_down and now - self._last_event_time < DEBOUNCE_S:
    return
```

Der Gate prüft nur `fn_down`, nicht ob es sich wirklich um eine *Wiederholung*
handelt. Ablauf: Loslassen bei t → `_last_event_time = t`, `_held = False`. Erneutes
Drücken bei t+20 ms → Gate greift, Return. `_held` bleibt `False`. Da flagsChanged
flankengesteuert ist, kommt **kein weiteres Event**, solange Fn gehalten wird: Der
Nutzer spricht einen ganzen Satz, ohne Overlay, ohne Aufnahme. Das anschließende
Loslassen ist ebenfalls wirkungslos (`_dispatch` verlangt `self._held`).
`_last_event_time` wird im unterdrückten Zweig zudem nicht aktualisiert.

**Fix:** Nur unterdrücken, wenn der Zustand sich gar nicht ändert:
`if fn_down == self._held and now - self._last_event_time < DEBOUNCE_S: return`.

### N-2 — `SerialRunner.stop()` gibt den Thread frei, bevor er beendet ist
[hotkey.py:49-52](../flow/hotkey.py#L49-L52)

`stop()` legt das `None`-Sentinel in die Queue und setzt sofort `self._thread = None`.
Ein danach folgendes `start()` startet einen zweiten Consumer auf derselben Queue,
während der alte noch läuft. Relevant nur bei Stop/Start-Zyklen.

---

## 3. Audio-Pipeline (`flow/pipeline.py`, `flow/audio.py`)

### H-3 — `reset_portaudio()` läuft mit noch offenem Stream
[pipeline.py:150-157](../flow/pipeline.py#L150-L157), [pipeline.py:230-236](../flow/pipeline.py#L230-L236)

`Recorder.start()` ([audio.py:65-71](../flow/audio.py#L65-L71)) setzt erst
`self._stream = sd.InputStream(...)` und ruft danach `self._stream.start()`. Wirft
dieser zweite Aufruf (Gerät von anderer App belegt, HAL-Fehler), bleibt `_stream`
gesetzt und offen. Der `except`-Zweig in `on_hold_start` ruft dann `_recover_audio()`
→ `sd._terminate()` — genau gegen den eigenen Vertrag von `reset_portaudio`
("Only call while no stream is active", [audio.py:137](../flow/audio.py#L137)) und genau
der Absturz, den der Kommentar bei [pipeline.py:203-213](../flow/pipeline.py#L203-L213)
umgeht. Unmittelbar danach ersetzt `self.recorder = Recorder()` die einzige Referenz auf
den offenen Stream; dessen Finalizer ruft später `close()` gegen ein zwischenzeitlich
terminiertes und neu initialisiertes PortAudio.

**Fix:** In `Recorder.start()` `try/except` um `self._stream.start()` legen, dort
`close()` + `self._stream = None`. Zusätzlich im `except`-Pfad von `on_hold_start` den
Recorder verwerfen (`abandon()`), bevor `reset_portaudio()` läuft.

### M-1 — Blockierender PortAudio-Teardown unter gehaltenem Lock auf dem einzigen Hotkey-Thread
[pipeline.py:140-157](../flow/pipeline.py#L140-L157)

`_recover_audio()` wird innerhalb von `with self._lock:` aufgerufen und macht
blockierendes `Pa_Terminate` / `Pa_Initialize`. Der Code läuft auf dem
`hotkey-dispatch`-Thread des `SerialRunner` — dem einzigen Thread, der Hotkey-Callbacks
abarbeitet. Hängt PortAudio dort (genau der Wedged-CoreAudio-Fall, für den die Recovery
gedacht ist), stauen sich alle weiteren Drücke/Loslassen dahinter, und der
`_force_stop`-Timer blockiert zusätzlich am selben Lock. Die App reagiert dann still
gar nicht mehr auf den Hotkey — schlechter als das Verhalten vor der Recovery, das nur
`set_state("error")` gemacht hat.

**Fix:** Recovery außerhalb des Locks bzw. auf einem eigenen Thread mit Timeout
ausführen (analog zu `_stop_recorder_safe`).

### M-2 — Verwaister Recovery-Timer setzt den Zustand auf "idle"
[pipeline.py:238-246](../flow/pipeline.py#L238-L246)

`_enter_error_then_idle` überschreibt `self._recover_timer`, ohne den vorherigen zu
canceln. Zwei Fehler innerhalb von 2 s hinterlassen einen Waisen-Timer, den der
Cancel-Pfad in `on_hold_start` ([pipeline.py:163-165](../flow/pipeline.py#L163-L165))
nicht mehr sieht. Er feuert später und setzt `set_state("idle")` — überschreibt also
u. U. ein laufendes "recording"/"processing". Genau der Bug, gegen den das Tracking
eingebaut wurde.

**Fix:** Vor dem Neusetzen `if self._recover_timer: self._recover_timer.cancel()`.

### M-3 — Diktat wird erst eingefügt und dann gespeichert
[pipeline.py:322-333](../flow/pipeline.py#L322-L333)

`insert_text(...)` läuft vor `store.add_dictation(...)`. Wirft das Einfügen (siehe K-3),
ist der Text weder eingefügt noch in der History — unwiederbringlich weg, obwohl STT und
Cleanup bereits erfolgreich waren.

**Fix:** Reihenfolge tauschen: erst persistieren, dann einfügen.

### M-4 — Startton wird ins eigene Mikrofon aufgenommen
[pipeline.py:151-159](../flow/pipeline.py#L151-L159)

`self.recorder.start()` öffnet den Stream, der Callback sammelt bereits Chunks — erst
*danach* läuft `sound.play_start()`. Der Zweiton-Chime (~0,23 s) landet über das
eingebaute Mikrofon in den ersten Frames der Aufnahme und geht an Whisper. Whisper
neigt bei tonalen Nicht-Sprach-Anläufen zu Halluzinationstokens; zusammen mit dem
aktiven `initial_prompt`-Biasing kann solcher Müll ins Wörterbuch wandern. Der
Stopp-Ton ist korrekt *nach* `_stop_recorder_safe()` platziert
([pipeline.py:194-196](../flow/pipeline.py#L194-L196)) — die Asymmetrie ist der Beleg.

**Fix:** `play_start()` vor `recorder.start()` ziehen (und kurz warten oder die ersten
~250 ms verwerfen).

### N-3 — `sound.play_*` ruft AppKit vom Hintergrund-Thread
`NSSound.play()` wird vom `hotkey-dispatch`-Thread aufgerufen. AppKit ist dort nicht
zugesichert thread-safe. Praktisch bislang unauffällig, sauber wäre `AppHelper.callAfter`.

### N-4 — Sample-Rate hartkodiert
[pipeline.py:330](../flow/pipeline.py#L330): `duration_s=len(captured) / 16000` — der
Recorder ist über `Recorder(samplerate=…)` konfigurierbar. Besser
`self.recorder.samplerate`, sonst kippt die WPM-Statistik still.

---

## 4. Sicherheit & Privatsphäre

### H-4 — API-Key steht als Kommandozeilenargument im Prozesslisting
[menubar.py:247-252](../flow/menubar.py#L247-L252)

```python
subprocess.run(["security", "add-generic-password", "-U",
                "-s", KEYCHAIN_SERVICE, "-a", provider, "-w", key], ...)
```

Argumente sind auf macOS für **jeden lokalen Prozess** über `ps -ax` bzw. `sysctl
KERN_PROCARGS2` sichtbar, solange der Prozess läuft. Der Anthropic-/OpenRouter-Key
liegt damit kurzzeitig offen. Die Kommentare im UI-Dialog versprechen ausdrücklich
"stored in the macOS Keychain, never in a file" — der Transportweg dorthin unterläuft
das.

**Fix:** Key über stdin übergeben (`security add-generic-password … -w` ohne Wert und
`input=key` an `subprocess.run`) oder direkt die Keychain-API via pyobjc
(`SecItemAdd`) nutzen.

### H-5 — Vollständige Transkripte im Klartext-Log, weltweit lesbar
[pipeline.py:314](../flow/pipeline.py#L314), [pipeline.py:321](../flow/pipeline.py#L321)

```python
log.info("STT (%.2fs): %r", t_stt - released_at, raw)
log.info("Cleaned (%.2fs): %r", ..., text)
```

Jedes Diktat landet zweimal im Klartext in `~/Library/Logs/whisper-flow.log`
(Rotating, 3 × 1 MB). Auf diesem Rechner aktuell: 138 KB, Rechte `-rw-r--r--` — für
jeden anderen lokalen Benutzer lesbar. Ein Diktiergerät transkribiert zwangsläufig auch
Passwörter, Gesundheitsdaten, Vertrauliches. Das Versprechen "All data stays on this
Mac" ([index.html:81](../flow/ui/index.html#L81)) ist zwar formal eingehalten, die
Erwartungshaltung aber nicht. `store.db` (0644) hat dieselbe Eigenschaft.

**Fix:** Transkripte auf `DEBUG` herunterstufen (und Level per Config schaltbar machen)
oder auf Länge/Hash kürzen; Log- und DB-Datei mit `0600` anlegen
(`os.chmod` nach `RotatingFileHandler`-Erzeugung, `Store.__init__` analog).

### M-5 — Web Inspector in Release-Builds aktiv
[window.py:142](../flow/window.py#L142): `config.preferences().setValue_forKey_(True,
"developerExtrasEnabled")`. Als Dev-Komfort kommentiert, wird aber mitgebaut. Damit ist
in der ausgelieferten App per Rechtsklick die volle JS-Konsole auf der
Bridge (`window.webkit.messageHandlers.bridge`) verfügbar, inklusive
`deleteEntry`/`deleteWord`/`copyText` auf beliebige IDs.

**Fix:** An `config_mod.IS_FROZEN` koppeln.

### N-5 — `cleanup_base_url` wird nicht auf HTTPS geprüft
[cleanup.py:213-226](../flow/cleanup.py#L213-L226): Der Bearer-Token wird an eine frei
konfigurierbare URL geschickt. Steht in `config.toml` versehentlich `http://`, geht der
Key im Klartext über das Netz. Ein Schema-Check beim Laden der Config ist billig.

### N-6 — Signierung / Distribution
[packaging/build.sh](../packaging/build.sh): selbstsigniert oder ad-hoc, **kein**
Hardened Runtime, keine Entitlements-Datei, keine Notarisierung. Für die lokale
Nutzung ok, für Weitergabe blockiert Gatekeeper. Außerdem ist `codesign --deep` von
Apple offiziell deprecated — für verschachtelte Bundles sollte von innen nach außen
signiert werden.

---

## 5. Wörterbuch-Lernen (`flow/store.py`, `flow/learn.py`, `flow/pipeline.py`)

### H-6 — Selbstverstärkende Fehl-Transkription durch `PROMOTE_AT = 1`
[store.py:23-28](../flow/store.py#L23-L28), [pipeline.py:70-89](../flow/pipeline.py#L70-L89)

Ein Begriff wandert beim **ersten** Sichten dauerhaft ins Wörterbuch und damit ab dem
nächsten Diktat in Whispers `initial_prompt`. Verhört sich Whisper einmal (deutscher
Eigenname → plausibles Pseudowort), liefert das Cleanup-LLM es als `new_term`,
`filter_candidates` lässt es durch (es ist ja gerade *kein* gewöhnliches Wörterbuchwort
— exakt das Filterkriterium), und es wird promotet. Ab dann macht der `initial_prompt`
dasselbe Fehlwort wahrscheinlicher, `bump_uses` zählt den Treffer und schiebt es im
Prompt nach vorne. Das entfernte `PROMOTE_AT = 2` war die einzige Absicherung, dass ein
Begriff zweimal gehört werden muss, bevor er STT beeinflusst; Ersatz gibt es keinen.

**Fix:** Entweder zurück auf 2, oder Promotion bei 1 belassen, aber erst ab der zweiten
Sichtung in den `initial_prompt` aufnehmen (z. B. `uses >= 1` als Filter in
`dictionary_words()`).

### H-7 — Der "gewöhnliches Wort"-Filter kennt kein Deutsch
[learn.py:25-37](../flow/learn.py#L25-L37)

`_dict_words()` liest ausschließlich `/usr/share/dict/words` — eine **englische**
Wortliste. Für einen überwiegend deutschsprachigen Nutzer greift der zentrale
Anti-Pollution-Filter damit praktisch nie: "Besprechung", "Rechnung", "Zusammenfassung"
sind dort nicht enthalten, gelten also als unbekannte Fachbegriffe und werden — mit
`PROMOTE_AT = 1` sofort — ins Wörterbuch aufgenommen und in den STT-Prompt injiziert.
Das ist genau die Verschmutzung, die das Modul laut eigenem Docstring verhindern soll.

**Fix:** Zusätzliche deutsche Wortliste (z. B. `/usr/share/dict/German` falls vorhanden,
sonst mitgeliefert), oder heuristisch: Wörter ohne "starke Form" (`_has_strong_shape`)
grundsätzlich erst ab 2–3 Sichtungen zulassen.

### M-6 — `_terms_in` matcht Teilstrings ohne Wortgrenzen
[pipeline.py:51-63](../flow/pipeline.py#L51-L63)

```python
return [t for t in vocabulary if t.lower() in text_l]
```

Steht "AI", "Go" oder "MLX" im Wörterbuch, matchen "again" (enthält "ai"), "Google"
(enthält "go") usw. Folgen: `bump_uses` bläht die Zähler auf, kurze Begriffe belegen
dauerhaft die Spitze des 30-Term-`initial_prompt` (`ORDER BY uses DESC`), echte
mehrwortige Begriffe bleiben bei `uses = 0` und fliegen bei der Verdrängung
(`ORDER BY uses ASC`) zuerst raus. `_dict_fixes` meldet dieselben Phantomtreffer als
Wörterbuch-Korrekturen im Insights-Panel.

**Fix:** Regex mit Wortgrenzen, `re.search(rf"(?<!\w){re.escape(t)}(?!\w)", text, re.I)`.

---

## 6. LLM-Cleanup (`flow/cleanup.py`)

### M-7 — Abgeschnittene Antworten werden als vollständig behandelt
[cleanup.py:194-211](../flow/cleanup.py#L194-L211)

`_parse_anthropic_response` prüft `resp.stop_reason` nicht. Erreicht ein langes Diktat
(bis `max_duration_s = 120`) das `max_tokens = 2048`, liefert die API einen
abgeschnittenen `tool_use`-Block. `obj.get("cleaned_text")` ergibt das gekürzte Präfix,
und weil es nicht leer ist, greift der Fallback `cleaned or text` nicht — der Schluss
des Diktats wird kommentarlos verworfen und der Rumpf eingefügt. Auf dem
OpenAI-kompatiblen Pfad ist `finish_reason` genauso ungeprüft
([cleanup.py:259-260](../flow/cleanup.py#L259-L260)).

**Fix:** Bei `stop_reason == "max_tokens"` bzw. `finish_reason == "length"` den
Rohtranskript-Fallback nehmen und warnen.

### N-7 — Konfigurierbares Modell wird ungeprüft übernommen
`cleanup_model` und `cleanup_base_url` kommen ungeprüft aus der TOML; ein Tippfehler
äußert sich erst zur Laufzeit als HTTP-Fehler und wird zu "raw transcript" degradiert
(korrekt, aber für den Nutzer stumm — nur im Log sichtbar).

---

## 7. Persistenz & Sonstiges

### N-8 — `purge_old` läuft nur beim Start
[__main__.py:81-88](../flow/__main__.py#L81-L88). Unter dem LaunchAgent
(`KeepAlive`) läuft der Prozess wochenlang; die 14-Tage-Retention greift dann nie.
→ periodischer Timer.

### N-9 — `history()` und `insights()` laden immer die komplette Tabelle
[store.py:96-109](../flow/store.py#L96-L109),
[store.py:245-249](../flow/store.py#L245-L249) — ohne `LIMIT`, und die
`_to_local_date`-Schleife läuft über alle Zeilen. Bei gepinnten Einträgen (nie gelöscht)
wächst das unbegrenzt. Aktuell unkritisch (24 KB DB), aber die WAL-Datei steht bereits
bei 1,1 MB.

### N-10 — `consume_open_request` fängt nur `FileNotFoundError`
[lifecycle.py:63-70](../flow/lifecycle.py#L63-L70) — ein `PermissionError` beim
`unlink()` schlägt bis in den rumps-Timer durch.

### Positiv hervorzuheben

- Die JS-Seite escaped **alle** benutzerkontrollierten Werte konsequent (`esc()` in
  [app.js:221-225](../flow/ui/app.js#L221-L225)) — kein XSS-Vektor gefunden, obwohl
  durchgehend `innerHTML` benutzt wird.
- Alle SQL-Statements sind parametrisiert, auch das dynamisch gebaute `IN (...)` in
  `bump_uses` — keine Injection.
- Der `MainLoopWatchdog` mit Stack-Dump vor `os._exit` ist eine ungewöhnlich gute
  Absicherung gegen den Wedged-Main-Thread.
- `_stop_recorder_safe` mit Timeout + `abandon()` ist die richtige Antwort auf
  CoreAudio-Deadlocks.
- Cleanup degradiert bei jedem Fehler sauber auf das Rohtranskript.

---

## Empfohlene Reihenfolge

1. **K-1, K-2, K-3** — alles in `insert.py`, ein zusammenhängender Fix, verhindert
   Datenverlust bei Nutzer-Zwischenablage und Diktat.
2. **H-1** — Fn-Hotkey nach dem Start tot; hohe Sichtbarkeit, kleiner Fix.
3. **H-4, H-5** — Sicherheit/Privatsphäre, beide je ~5 Zeilen.
4. **H-2, H-3** — still verlorene Diktate bzw. Absturzrisiko im Recovery-Pfad.
5. **H-6, H-7** — Wörterbuch-Qualität; betrifft die Kernfunktion auf Dauer am stärksten.
6. Rest nach Bedarf.
