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
DEV_IDENTITY="BomboclatFlow Dev"
LEGACY_IDENTITY="WhisperFlow Dev"

warn_adhoc_signing() {
    echo ""
    echo "  ⚠️  WARNING: No dev signing identity found"
    echo "  ─────────────────────────────────────────────────"
    echo "  Falling back to ad-hoc signing. macOS will forget"
    echo "  Input Monitoring, Microphone, and Accessibility"
    echo "  grants after every rebuild (new code signature each time)."
    echo ""
    echo "  One-time fix — create a Code Signing certificate:"
    echo "    1. Open Keychain Access (Schlüsselbundverwaltung)"
    echo "    2. Keychain Access menu → Certificate Assistant → Create a Certificate…"
    echo "    3. Name: ${DEV_IDENTITY}"
    echo "    4. Identity type: Code Signing"
    echo "    5. Create, then re-run: ./packaging/build.sh"
    echo ""
    echo "  Verify after setup:"
    echo "    security find-identity -p codesigning -v | grep \"${DEV_IDENTITY}\""
    echo ""
    echo "  See packaging/README.md for details."
    echo ""
}

warn_install_from_applications() {
    echo ""
    echo "  📦 Install reminder"
    echo "  ─────────────────────────────────────────────────"
    echo "  1. Open dist/BomboclatFlow.dmg"
    echo "  2. Drag Bomboclat Flow.app to Applications"
    echo "  3. Eject the DMG before launching"
    echo "  4. Run only from /Applications — not from dist/ or the mounted DMG"
    echo ""
}

echo "==> PyInstaller"
.venv/bin/pyinstaller --noconfirm packaging/Flow.spec

echo "==> Codesign"
IDENTITY=""
for candidate in "$DEV_IDENTITY" "$LEGACY_IDENTITY"; do
    if security find-identity -p codesigning -v 2>/dev/null | grep -q "$candidate"; then
        IDENTITY="$candidate"
        break
    fi
done
if [[ -n "$IDENTITY" ]]; then
    echo "    signing with identity: $IDENTITY"
    echo "    (TCC permissions should persist across rebuilds)"
    codesign --force --deep --sign "$IDENTITY" "$APP"
    SIGNED_WITH_DEV_IDENTITY=1
else
    warn_adhoc_signing
    codesign --force --deep --sign - "$APP"
    SIGNED_WITH_DEV_IDENTITY=0
fi
codesign --verify --deep "$APP" && echo "    signature OK"

echo "==> DMG"
rm -f dist/BomboclatFlow.dmg
$PYTHON -m dmgbuild -s packaging/dmg_settings.py "Bomboclat Flow" dist/BomboclatFlow.dmg

echo "==> Done: $APP + dist/BomboclatFlow.dmg"
du -sh "$APP" dist/BomboclatFlow.dmg

if [[ "$SIGNED_WITH_DEV_IDENTITY" -eq 0 ]]; then
    echo ""
    echo "  Build succeeded with ad-hoc signing — permissions will reset on the next rebuild."
fi
warn_install_from_applications
