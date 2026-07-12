#!/bin/zsh
# Build WhisperFlow.app and WhisperFlow.dmg.
#   ./packaging/build.sh
#
# Signing: uses the self-signed identity "WhisperFlow Dev" if it exists in
# the Keychain (keeps TCC permission grants stable across rebuilds — create
# one via Keychain Access > Certificate Assistant > Create a Certificate,
# type "Code Signing"). Falls back to ad-hoc signing, where macOS forgets
# the app's permissions on every rebuild.
set -euo pipefail
cd "$(dirname "$0")/.."

PYTHON=.venv/bin/python
IDENTITY="WhisperFlow Dev"

echo "==> PyInstaller"
.venv/bin/pyinstaller --noconfirm packaging/Flow.spec

echo "==> Codesign"
if security find-identity -p codesigning -v 2>/dev/null | grep -q "$IDENTITY"; then
    echo "    signing with identity: $IDENTITY"
    codesign --force --deep --sign "$IDENTITY" dist/WhisperFlow.app
else
    echo "    no '$IDENTITY' identity found — ad-hoc signing"
    echo "    (permissions must be re-granted after every rebuild)"
    codesign --force --deep --sign - dist/WhisperFlow.app
fi
codesign --verify --deep dist/WhisperFlow.app && echo "    signature OK"

echo "==> DMG"
rm -f dist/WhisperFlow.dmg
$PYTHON -m dmgbuild -s packaging/dmg_settings.py "WhisperFlow" dist/WhisperFlow.dmg

echo "==> Done: dist/WhisperFlow.app + dist/WhisperFlow.dmg"
du -sh dist/WhisperFlow.app dist/WhisperFlow.dmg
