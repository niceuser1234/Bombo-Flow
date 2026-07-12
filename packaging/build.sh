#!/bin/zsh
# Build "Bomboclat Flow.app" and BomboclatFlow.dmg.
#   ./packaging/build.sh
#
# Signing: uses the self-signed identity "BomboclatFlow Dev" (or the legacy
# "WhisperFlow Dev") if it exists in the Keychain — that keeps TCC permission
# grants stable across rebuilds. Create one via Keychain Access > Certificate
# Assistant > Create a Certificate, type "Code Signing". Falls back to ad-hoc
# signing, where macOS forgets the app's permissions on every rebuild.
set -euo pipefail
cd "$(dirname "$0")/.."

PYTHON=.venv/bin/python
APP="dist/Bomboclat Flow.app"

echo "==> PyInstaller"
.venv/bin/pyinstaller --noconfirm packaging/Flow.spec

echo "==> Codesign"
IDENTITY=""
for candidate in "BomboclatFlow Dev" "WhisperFlow Dev"; do
    if security find-identity -p codesigning -v 2>/dev/null | grep -q "$candidate"; then
        IDENTITY="$candidate"
        break
    fi
done
if [[ -n "$IDENTITY" ]]; then
    echo "    signing with identity: $IDENTITY"
    codesign --force --deep --sign "$IDENTITY" "$APP"
else
    echo "    no dev identity found — ad-hoc signing"
    echo "    (permissions must be re-granted after every rebuild)"
    codesign --force --deep --sign - "$APP"
fi
codesign --verify --deep "$APP" && echo "    signature OK"

echo "==> DMG"
rm -f dist/BomboclatFlow.dmg
$PYTHON -m dmgbuild -s packaging/dmg_settings.py "Bomboclat Flow" dist/BomboclatFlow.dmg

echo "==> Done: $APP + dist/BomboclatFlow.dmg"
du -sh "$APP" dist/BomboclatFlow.dmg
