"""Does a thumbnail look like the channel's own examples (data/brand/<channel>/examples)?

Adapted from the Chronovanta bot (2026-11-05). That one measured one design
(an orange bar); this bot has a different layout per channel, so the card is
measured on what every layout has:

    layout      where the light, dark and coloured parts sit (an 8x4 grid in
                Lab colour; a 9:16 card on a 4x8 grid)
    colours     the spread of hues, weighted by how strong they are
    brightness  how light or dark the card is
    saturation  how vivid it is
    headline    how much of the card is bright, near-white lettering

and compared with the average of the examples. A Short (9:16) compared with
wide examples uses the colours and brightness only. The score is 0-100; under
PASS the card fails, with the reasons in plain words, and the pipeline draws
it again with a new picture and keeps the best one (pipeline.style_matched).
"""
from __future__ import annotations

import json
from pathlib import Path

PASS = 50


def _load(path: Path):
    import cv2
    import numpy as np

    image = cv2.imdecode(np.fromfile(str(path), dtype=np.uint8), cv2.IMREAD_COLOR)
    if image is None:
        raise ValueError(f"not an image: {path.name}")
    tall = image.shape[0] > image.shape[1]
    return cv2.resize(image, (180, 320) if tall else (320, 180), interpolation=cv2.INTER_AREA), tall


def measure(path: str | Path) -> dict:
    import cv2
    import numpy as np

    image, tall = _load(Path(path))
    hsv = cv2.cvtColor(image, cv2.COLOR_BGR2HSV).astype(float)
    hue, sat, val = hsv[:, :, 0], hsv[:, :, 1] / 255.0, hsv[:, :, 2] / 255.0
    weight = (sat * val).ravel()
    hist, _ = np.histogram(hue.ravel(), bins=36, range=(0, 180), weights=weight)
    grid = cv2.cvtColor(cv2.resize(image, (4, 8) if tall else (8, 4), interpolation=cv2.INTER_AREA),
                        cv2.COLOR_BGR2LAB).astype(float)
    total = float(hist.sum()) or 1.0
    white = (val > 0.82) & (sat < 0.18)
    return {
        "tall": bool(tall),
        "hues": [float(x) / total for x in hist],
        "grid": grid.tolist(),
        "brightness": float(val.mean()),
        "saturation": float(sat.mean()),
        "headline": float(white.mean()),
    }


def reference(channel_id: str, tall: bool) -> dict | None:
    """The average of the channel's examples; the same shape first, else all of them."""
    from .brand import examples

    measured = []
    for path in examples(channel_id):
        try:
            measured.append(measure(path))
        except Exception:
            continue
    if not measured:
        return None
    same = [m for m in measured if m["tall"] == tall]
    pool = same or measured
    avg = {key: sum(m[key] for m in pool) / len(pool) for key in ("brightness", "saturation", "headline")}
    avg["hues"] = [sum(m["hues"][i] for m in pool) / len(pool) for i in range(36)]
    if same:
        import numpy as np

        avg["grid"] = np.mean([m["grid"] for m in same], axis=0).tolist()
    avg["same_shape"] = bool(same)
    avg["count"] = len(pool)
    return avg


def compare(path: str | Path, channel_id: str) -> dict | None:
    """{score, ok, issues} against the examples; None when the channel has none."""
    card = measure(path)
    ref = reference(channel_id, card["tall"])
    if ref is None:
        return None
    colours = sum(min(a, b) for a, b in zip(card["hues"], ref["hues"]))  # 0..1
    parts = [(0.45, colours)]
    issues = []
    if colours < 0.4:
        issues.append("the colours are not the ones in your examples")
    if ref["same_shape"]:
        import numpy as np

        distance = float(np.sqrt(((np.array(card["grid"]) - np.array(ref["grid"])) ** 2).sum(axis=2)).mean())
        parts.append((0.35, max(0.0, 1 - distance / 90)))
        if distance > 60:
            issues.append("the light and dark parts sit in other places than in your examples")
    for key, weight, words in (("brightness", 0.1, ("darker", "lighter")),
                               ("saturation", 0.1, ("duller", "more vivid"))):
        diff = card[key] - ref[key]
        parts.append((weight, max(0.0, 1 - abs(diff) / max(ref[key], 0.15))))
        if abs(diff) > max(ref[key], 0.15) * 0.35:
            issues.append(f"{words[0] if diff < 0 else words[1]} than your examples")
    if ref["same_shape"]:
        diff = card["headline"] - ref["headline"]
        parts.append((0.1, max(0.0, 1 - abs(diff) / max(ref["headline"], 0.03))))
        if abs(diff) > max(ref["headline"], 0.03) * 0.5:
            issues.append("a smaller headline than your examples" if diff < 0 else "a bigger headline than your examples")
    score = round(100 * sum(w * v for w, v in parts) / sum(w for w, _ in parts))
    return {"score": score, "ok": score >= PASS, "issues": issues, "examples": ref["count"]}


def save_check(path: str | Path, result: dict | None) -> None:
    if result is not None:
        Path(path).with_suffix(".style.json").write_text(json.dumps(result, indent=2), encoding="utf-8")


def load_check(path: str | Path) -> dict | None:
    try:
        return json.loads(Path(path).with_suffix(".style.json").read_text(encoding="utf-8"))
    except Exception:
        return None
