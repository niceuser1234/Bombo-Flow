"""Generate packaging/BomboclatFlow.icns from the Palm-Mic logo SVG.

Rasterises packaging/bomboclat_flow_icon.svg (the green Palm-Mic on a cream
rounded-rect, already sized with macOS icon margins) into an .iconset and packs
it with iconutil. Reproducible — no hand-assembled assets.

Run when the logo changes (the committed .icns is what the build uses):
    .venv/bin/python packaging/make_icon.py
"""

import subprocess
import tempfile
from pathlib import Path

import AppKit
import Foundation

HERE = Path(__file__).resolve().parent
SIZE = 1024
SVG_SOURCE = HERE / "bomboclat_flow_icon.svg"


def render_master(path: Path):
    src = AppKit.NSImage.alloc().initWithContentsOfFile_(str(SVG_SOURCE))
    if src is None:
        raise SystemExit(f"Could not load SVG: {SVG_SOURCE}")

    img = AppKit.NSImage.alloc().initWithSize_((SIZE, SIZE))
    img.lockFocus()
    src.drawInRect_fromRect_operation_fraction_(
        Foundation.NSMakeRect(0, 0, SIZE, SIZE),
        Foundation.NSZeroRect,
        AppKit.NSCompositingOperationSourceOver,
        1.0,
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
