# Packaging & Code Signing

`./packaging/build.sh` erzeugt `dist/Bomboclat Flow.app` und `dist/BomboclatFlow.dmg`.

## Code-Signing-Zertifikat (empfohlen)

Für stabile macOS-Berechtigungen (TCC) brauchst du ein **lokales, selbstsigniertes Code-Signing-Zertifikat** mit dem exakten Namen **`BomboclatFlow Dev`** in deinem Login-Keychain.

`build.sh` erkennt das Zertifikat automatisch und signiert damit. Ohne Zertifikat fällt der Build auf **Ad-hoc-Signing** zurück (`codesign --sign -`) — der Build läuft weiter, aber Berechtigungen gehen nach jedem Rebuild verloren.

### Einmalig anlegen (Keychain Access)

1. **Schlüsselbundverwaltung** öffnen (Keychain Access).
2. Menü **Schlüsselbundverwaltung** → **Zertifikatsassistent** → **Zertifikat erstellen…**
3. **Name:** `BomboclatFlow Dev` (exakt so — `build.sh` sucht diesen String).
4. **Identitätstyp:** Code Signing.
5. **Erstellen** — bei Aufforderung das Zertifikat als vertrauenswürdig einstufen.
6. `./packaging/build.sh` erneut ausführen. Die Ausgabe sollte `signing with identity: BomboclatFlow Dev` zeigen.

**Legacy:** Falls du noch ein älteres Zertifikat `WhisperFlow Dev` hast, wird das ebenfalls akzeptiert.

### Prüfen, ob das Zertifikat vorhanden ist

```sh
security find-identity -p codesigning -v | grep "BomboclatFlow Dev"
```

## Warum Ad-hoc-Signing TCC-Berechtigungen verliert

macOS speichert Datenschutz-Berechtigungen (TCC — Transparency, Consent, and Control) an die **Code-Signatur** der App gebunden:

| Berechtigung | Zweck |
|---|---|
| Input Monitoring | Hotkey-Erkennung |
| Mikrofon | Aufnahme |
| Bedienungshilfen | programmatisches Einfügen (Cmd+V) |

Bei **Ad-hoc-Signing** ist die Signatur bei **jedem Rebuild neu** — für macOS ist das faktisch eine andere App. Bereits erteilte Einträge in *Systemeinstellungen → Datenschutz & Sicherheit* gelten dann nicht mehr; du musst sie nach jedem `./packaging/build.sh` neu vergeben (App beenden, neu öffnen, ggf. Häkchen erneut setzen).

Mit **`BomboclatFlow Dev`** bleibt die Signatur über Rebuilds hinweg **stabil**. Dieselbe Binär-Identität → dieselben TCC-Einträge bleiben gültig.

> **Hinweis:** Das Zertifikat ist nur für lokale Entwicklung auf **deinem** Mac gedacht. Die App ist nicht notarisiert; auf einem anderen Mac ist einmalig **Trotzdem öffnen** in den Systemeinstellungen nötig.

## Installation über die DMG

1. `dist/BomboclatFlow.dmg` öffnen (Doppelklick).
2. **Bomboclat Flow.app** per Drag & Drop nach **Programme** (`/Applications`) kopieren.
3. **DMG auswerfen** — Rechtsklick auf das DMG-Laufwerk → **Auswerfen** (oder im Finder auswerfen). Nicht von der gemounteten DMG starten.
4. App **ausschließlich aus `/Applications`** starten (Spotlight, Programme-Ordner oder Launchpad).

**Nicht** die Kopie unter `dist/` oder auf der gemounteten DMG für den Alltag nutzen:

- `dist/Bomboclat Flow.app` ist nur das Build-Artefakt auf deinem Rechner.
- Eine App von der DMG oder aus `dist/` hat einen anderen Pfad und ggf. eine andere Signatur-Kontext-Wahrnehmung → doppelte oder wirkungslose TCC-Einträge in den Systemeinstellungen.

Nach der Installation: App einmal öffnen, Berechtigungen erteilen (siehe Haupt-README), STT-Modell laden lassen.

## Build ausführen

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
./packaging/build.sh
```

Die Codesign-Ausgabe am Ende des Builds zeigt, ob mit `BomboclatFlow Dev` oder Ad-hoc signiert wurde. Bei Ad-hoc erscheint eine ausführliche Warnung mit den Schritten oben.
