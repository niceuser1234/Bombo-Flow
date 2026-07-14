# PyInstaller spec for "Bomboclat Flow.app" (Apple Silicon, macOS 14+).
# Build: .venv/bin/pyinstaller --noconfirm packaging/Flow.spec
import glob
import os

from PyInstaller.utils.hooks import collect_all

ROOT = os.path.abspath(os.path.join(SPECPATH, ".."))

datas, binaries, hiddenimports = [], [], []
# Packages without ready-made PyInstaller hooks. mlx must bring its Metal
# shader library (mlx/lib/mlx.metallib) or GPU inference dies at runtime.
for pkg in ("mlx", "parakeet_mlx", "soxr"):
    d, b, h = collect_all(pkg)
    datas += d
    binaries += b
    hiddenimports += h

# Default config seeded into ~/Library/Application Support on first launch.
datas.append((os.path.join(ROOT, "config.toml"), "."))

# The in-app web UI (loaded by flow/window.py via _MEIPASS/flow/ui).
for f in glob.glob(os.path.join(ROOT, "flow", "ui", "*")):
    if os.path.isfile(f):
        datas.append((f, "flow/ui"))

a = Analysis(
    [os.path.join(ROOT, "packaging", "launcher.py")],
    pathex=[ROOT],
    datas=datas,
    binaries=binaries,
    hiddenimports=hiddenimports,
    excludes=["tkinter", "matplotlib", "PIL", "IPython"],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    exclude_binaries=True,
    name="BomboclatFlow",
    console=False,
    target_arch="arm64",
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    name="BomboclatFlow",
)

app = BUNDLE(
    coll,
    name="Bomboclat Flow.app",
    icon=os.path.join(ROOT, "packaging", "BomboclatFlow.icns"),
    bundle_identifier="com.bomboclat.flow",
    info_plist={
        # Deliberately NOT LSUIElement: an agent app owns no Dock icon, and
        # macOS only offers Force Quit for apps that do. The menu bar icon
        # stays either way.
        "LSMinimumSystemVersion": "14.0",
        "NSHighResolutionCapable": True,
        "NSMicrophoneUsageDescription":
            "Bomboclat Flow records audio while you hold the hotkey and "
            "transcribes it locally on this Mac.",
        "CFBundleName": "Bomboclat Flow",
        "CFBundleDisplayName": "Bomboclat Flow",
        "CFBundleShortVersionString": "0.2.0",
        "CFBundleVersion": "2",
    },
)
