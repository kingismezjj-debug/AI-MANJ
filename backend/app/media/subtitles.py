from app.media.fonts import find_cjk_font


def ass_time(seconds: float) -> str:
    seconds = max(0.0, float(seconds))
    centis = int(round(seconds * 100))
    secs, cs = divmod(centis, 100)
    minutes, secs = divmod(secs, 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}.{cs:02d}"


def _escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace("{", "\\{").replace("}", "\\}").replace("\n", "\\N")


def build_ass(cues: list[tuple[float, float, str]], total: float, show_label: bool, width: int, height: int) -> str:
    font = "WenQuanYi Micro Hei"
    if find_cjk_font() is None:
        font = "DejaVu Sans"
    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {width}
PlayResY: {height}
WrapStyle: 0

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Dialogue,{font},52,&H00FFFFFF,&H000000FF,&H00000000,&H64000000,0,0,0,0,100,100,0,0,1,3,0,2,40,40,80,1
Style: Label,{font},36,&H00FFFFFF,&H000000FF,&H00101010,&H96000000,1,0,0,0,100,100,0,0,3,0,0,9,24,36,36,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    lines = [header.rstrip("\n")]
    if show_label:
        lines.append(f"Dialogue: 0,{ass_time(0)},{ass_time(max(total, 0.5))},Label,,0,0,0,,AI生成")
    for start, end, text in cues:
        cleaned = _escape((text or "").strip())
        if not cleaned:
            continue
        lines.append(f"Dialogue: 0,{ass_time(start)},{ass_time(end)},Dialogue,,0,0,0,,{cleaned}")
    return "\n".join(lines) + "\n"
