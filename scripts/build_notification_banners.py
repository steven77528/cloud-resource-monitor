"""Rebuild packaged notification PNG banners (Pillow is a build-time dependency)."""

from __future__ import annotations

import argparse
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont


ROOT = Path(__file__).resolve().parents[1]
OUTPUT_DIR = ROOT / "cloud_monitor" / "assets" / "notification-banners"
TITLE = "\u6167\u6a59\u4e91\u80fd\u76d1\u63a7\u544a\u8b66"
WIDTH, HEIGHT = 1200, 220
SCALE = 2
STATES = {
    "critical": ("\u4e25\u91cd", "#c62828", "#fff0f0", "#ffffff"),
    "warning": ("\u8b66\u544a", "#f2c200", "#fff9de", "#403400"),
    "info": ("\u901a\u77e5", "#1677d2", "#edf6ff", "#ffffff"),
    "recovery": ("\u6062\u590d", "#218739", "#eaf8ee", "#ffffff"),
}


def find_font(explicit: Path | None) -> Path:
    candidates = [explicit] if explicit else [
        Path("C:/Windows/Fonts/msyhbd.ttc"),
        Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc"),
        Path("/System/Library/Fonts/PingFang.ttc"),
    ]
    for candidate in candidates:
        if candidate and candidate.is_file():
            return candidate
    raise SystemExit("A CJK bold font is required. Pass its path with --font.")


def render_banner(font_path: Path, label: str, color: str, background: str,
                  badge_text: str) -> Image.Image:
    image = Image.new("RGB", (WIDTH * SCALE, HEIGHT * SCALE), background)
    draw = ImageDraw.Draw(image)
    title_font = ImageFont.truetype(str(font_path), 70 * SCALE)
    badge_font = ImageFont.truetype(str(font_path), 58 * SCALE)

    draw.rectangle((0, 0, WIDTH * SCALE, 12 * SCALE - 1), fill=color)
    draw.rectangle((0, 214 * SCALE, WIDTH * SCALE, HEIGHT * SCALE), fill=color)
    draw.text((64 * SCALE, 113 * SCALE), TITLE, font=title_font,
              anchor="lm", fill="#252b30")

    badge = (956 * SCALE, 68 * SCALE, 1136 * SCALE, 156 * SCALE)
    draw.rounded_rectangle(badge, radius=8 * SCALE, fill=color)
    draw.text((1046 * SCALE, 112 * SCALE), label, font=badge_font,
              anchor="mm", fill=badge_text)

    return image.resize((WIDTH, HEIGHT), resample=Image.Resampling.LANCZOS)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--font", type=Path, help="Path to a CJK bold TTF/TTC font")
    args = parser.parse_args()
    font_path = find_font(args.font)
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    for name, style in STATES.items():
        output = OUTPUT_DIR / f"{name}.png"
        render_banner(font_path, *style).save(output, format="PNG", optimize=True)
        print(output.relative_to(ROOT).as_posix())


if __name__ == "__main__":
    main()
