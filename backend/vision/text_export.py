"""Order raw OCR blocks for the host's execution-bound text export."""
import re


def format_snapshot(blocks):
    ordered = []
    for index, block in enumerate(blocks):
        text = block["text"]
        if not text:
            continue
        polygon = block["polygon"]
        left = min(p["x"] for p in polygon)
        top, bottom = min(p["y"] for p in polygon), max(p["y"] for p in polygon)
        ordered.append((top, left, index, bottom, text))
    rows = []
    for block in sorted(ordered):
        top, _, _, bottom, _ = block
        best, best_ratio = None, 0
        for row in rows:
            start, _, _, end, _ = row[0]
            overlap = min(end, bottom) - max(start, top)
            height = min(end - start, bottom - top)
            if height > 0 and 2 * overlap >= height and overlap / height > best_ratio:
                best, best_ratio = row, overlap / height
        if best is None:
            rows.append([block])
        else:
            best.append(block)
    lines = []
    for row in rows:
        parts = []
        for _, _, _, _, text in sorted(row, key=lambda b: (b[1], b[0], b[2])):
            if parts and not parts[-1][-1].isspace() and not text[0].isspace():
                parts.append(" ")
            parts.append(text)
        line = re.sub(r"\r\n|\r|\n", "\r\n", "".join(parts))
        lines.append(line if line.endswith("\r\n") else line + "\r\n")
    return "".join(lines)
