"""Generate packaging/BomboclatFlow.icns: 🎤 on a rounded-rect background.

Run once (committed .icns is used by the build):
    .venv/bin/python packaging/make_icon.py
"""

import subprocess
import tempfile
from pathlib import Path

import AppKit
import Foundation

HERE = Path(__file__).resolve().parent
SIZE = 1024


def render_master(path: Path):
    img = AppKit.NSImage.alloc().initWithSize_((SIZE, SIZE))
    img.lockFocus()

    # Rounded-rect background, macOS-icon style margins (~10%).
    inset = SIZE * 0.10
    rect = Foundation.NSMakeRect(inset, inset, SIZE - 2 * inset, SIZE - 2 * inset)
    radius = SIZE * 0.18
    AppKit.NSColor.colorWithCalibratedRed_green_blue_alpha_(
        0.13, 0.13, 0.15, 1.0
    ).setFill()
    AppKit.NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
        rect, radius, radius
    ).fill()

    # Centered emoji.
    attrs = {AppKit.NSFontAttributeName: AppKit.NSFont.systemFontOfSize_(560)}
    text = Foundation.NSAttributedString.alloc().initWithString_attributes_(
        "🎤", attrs
    )
    bounds = text.size()
    text.drawAtPoint_(
        ((SIZE - bounds.width) / 2, (SIZE - bounds.height) / 2 + SIZE * 0.01)
    )

    img.unlockFocus()
    rep = AppKit.NSBitmapImageRep.imageRepWithData_(img.TIFFRepresentation())
    png = rep.representationUsingType_properties_(AppKit.NSBitmapImageFileTypePNG, None)
    png.writeToFile_atomically_(str(path), True)


def main():
    with tempfile.TemporaryDirectory() as tmp:
        master = Path(tmp) / "master.png"
        render_master(master)
        iconset = Path(tmp) / "BomboclatFlow.iconset"
        iconset.mkdir()
        for pts in (16, 32, 128, 256, 512):
            for scale in (1, 2):
                px = pts * scale
                suffix = "" if scale == 1 else "@2x"
                out = iconset / f"icon_{pts}x{pts}{suffix}.png"
                subprocess.run(
                    ["sips", "-z", str(px), str(px), str(master),
                     "--out", str(out)],
                    check=True, capture_output=True,
                )
        subprocess.run(
            ["iconutil", "-c", "icns", str(iconset),
             "-o", str(HERE / "BomboclatFlow.icns")],
            check=True,
        )
    print(f"Wrote {HERE / 'BomboclatFlow.icns'}")


if __name__ == "__main__":
    main()
