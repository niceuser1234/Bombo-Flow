"""dmgbuild settings: drag-to-Applications layout."""

application = "dist/Bomboclat Flow.app"

files = [application]
symlinks = {"Applications": "/Applications"}

icon = "packaging/BomboclatFlow.icns"
icon_locations = {
    "Bomboclat Flow.app": (140, 150),
    "Applications": (420, 150),
}

window_rect = ((200, 200), (560, 320))
icon_size = 96
format = "UDZO"  # zlib-compressed
