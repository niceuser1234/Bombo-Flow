"""dmgbuild settings: drag-to-Applications layout."""

application = "dist/WhisperFlow.app"

files = [application]
symlinks = {"Applications": "/Applications"}

icon = "packaging/WhisperFlow.icns"
icon_locations = {
    "WhisperFlow.app": (140, 150),
    "Applications": (420, 150),
}

window_rect = ((200, 200), (560, 320))
icon_size = 96
format = "UDZO"  # zlib-compressed
