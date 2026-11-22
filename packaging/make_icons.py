"""Write the app icons from the drawn tray mark: build/icons/fetchbox.ico (Windows),
fetchbox.iconset/ (turned into .icns by iconutil on macOS) and fetchbox.png."""

import sys
from pathlib import Path

from fetchbox.tray import icon_image

out = Path(sys.argv[1] if len(sys.argv) > 1 else "build/icons")
out.mkdir(parents=True, exist_ok=True)
big = icon_image(1024)
big.save(out / "fetchbox.png")
big.save(out / "fetchbox.ico", sizes=[(s, s) for s in (16, 24, 32, 48, 64, 128, 256)])
iconset = out / "fetchbox.iconset"
iconset.mkdir(exist_ok=True)
for s in (16, 32, 128, 256, 512):
    icon_image(s).save(iconset / f"icon_{s}x{s}.png")
    icon_image(s * 2).save(iconset / f"icon_{s}x{s}@2x.png")
print(f"icons in {out}")
