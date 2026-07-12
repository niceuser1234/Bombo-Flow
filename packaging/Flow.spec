# PyInstaller spec for WhisperFlow.app (Apple Silicon, macOS 14+).
# Build: .venv/bin/pyinstaller --noconfirm packaging/Flow.spec
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
    name="WhisperFlow",
    console=False,
    target_arch="arm64",
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    name="WhisperFlow",
)

app = BUNDLE(
    coll,
    name="WhisperFlow.app",
    icon=os.path.join(ROOT, "packaging", "WhisperFlow.icns"),
    bundle_identifier="com.whisperflow.app",
    info_plist={
        # Menu bar agent: no Dock icon, no app switcher entry.
        "LSUIElement": True,
        "LSMinimumSystemVersion": "14.0",
        "NSHighResolutionCapable": True,
        "NSMicrophoneUsageDescription":
            "WhisperFlow records audio while you hold the hotkey and "
            "transcribes it locally on this Mac.",
        "CFBundleShortVersionString": "0.1.0",
        "CFBundleVersion": "1",
    },
)
