import io

from PIL import Image, ImageDraw, ImageFont, ImageOps

from app.media.fonts import find_cjk_font

STYLE_SUFFIX = "2D动漫，竖屏9:16，干净线稿，电影感光影，角色造型稳定，不要写实，不要文字水印"


def load_font(size: int):
    path = find_cjk_font()
    if path is None:
        return ImageFont.load_default()
    try:
        return ImageFont.truetype(str(path), size=size, index=0)
    except TypeError:
        return ImageFont.truetype(str(path), size=size)


def _wrap(draw: ImageDraw.ImageDraw, text: str, font, width: int) -> list[str]:
    lines: list[str] = []
    current = ""
    for char in text.replace("\n", ""):
        trial = current + char
        if draw.textlength(trial, font=font) <= width:
            current = trial
        else:
            if current:
                lines.append(current)
            current = char
    if current:
        lines.append(current)
    return lines[:12]


def _palette(seed: int) -> tuple[tuple[int, int, int], tuple[int, int, int], tuple[int, int, int]]:
    r = 40 + (seed * 17) % 80
    g = 28 + (seed * 29) % 50
    b = 36 + (seed * 13) % 70
    accent = (210, 70 + seed % 80, 58)
    ink = (244, 236, 226)
    return (r, g, b), accent, ink


def render_card(
    *,
    width: int,
    height: int,
    title: str,
    subtitle: str,
    body: str,
    seed: int,
    badge: str,
) -> bytes:
    bg, accent, ink = _palette(seed)
    image = Image.new("RGB", (width, height), bg)
    draw = ImageDraw.Draw(image)
    draw.rectangle((0, 0, width, 18), fill=accent)
    draw.rectangle((28, 48, width - 28, height - 48), outline=accent, width=3)
    title_font = load_font(64)
    sub_font = load_font(28)
    body_font = load_font(32)
    badge_font = load_font(22)
    draw.text((56, 90), title[:12] or "未命名", font=title_font, fill=ink)
    draw.text((56, 180), subtitle[:28], font=sub_font, fill=accent)
    y = 260
    for line in _wrap(draw, body, body_font, width - 140):
        draw.text((56, y), line, font=body_font, fill=ink)
        y += 48
    draw.rectangle((56, height - 160, width - 56, height - 96), fill=accent)
    draw.text((72, height - 148), badge[:32], font=badge_font, fill=(255, 248, 242))
    draw.text((56, height - 80), f"SEED {seed}", font=badge_font, fill=ink)
    buf = io.BytesIO()
    image.save(buf, format="PNG")
    return buf.getvalue()


def fit_png(data: bytes, width: int, height: int) -> bytes:
    image = Image.open(io.BytesIO(data)).convert("RGB")
    fitted = ImageOps.fit(image, (width, height), method=Image.Resampling.LANCZOS)
    buf = io.BytesIO()
    fitted.save(buf, format="PNG")
    return buf.getvalue()
