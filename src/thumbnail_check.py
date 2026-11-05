"""Check a thumbnail before its video may go to YouTube.

With REQUIRE_THUMBNAIL=true (the default) no Short or long video is uploaded
until its thumbnail passes these checks (taken from the Chronovanta bot,
2026-11-05). A thumbnail that fails is remade once; if it still fails the
video waits as pending.

    check_thumbnail(path)            -> ThumbCheck (ok, issues, size)
    ensure_ready(path, regenerate)   -> path, or raises ThumbnailNotReady
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

# YouTube's own limits for a custom thumbnail.
MAX_BYTES = 2 * 1024 * 1024
MIN_WIDTH, MIN_HEIGHT = 1280, 720
ASPECT = 16 / 9


class ThumbnailNotReady(RuntimeError):
    """The video must not be uploaded yet: its thumbnail is missing or not good enough."""


@dataclass
class ThumbCheck:
    ok: bool
    issues: list[str] = field(default_factory=list)
    width: int = 0
    height: int = 0
    kb: int = 0

    @property
    def summary(self) -> str:
        if self.ok:
            return f"Verified {self.width}x{self.height}, {self.kb} KB"
        return "; ".join(self.issues)


def thumbnail_required() -> bool:
    return os.getenv("REQUIRE_THUMBNAIL", "true").strip().lower() not in {"0", "false", "no", "off"}


_CACHE: dict[tuple[str, int, int], ThumbCheck] = {}


def check_thumbnail(path: str | Path | None) -> ThumbCheck:
    if not path:
        return ThumbCheck(False, ["no thumbnail file"])
    path = Path(path)
    if not path.is_file():
        return ThumbCheck(False, ["thumbnail file is missing"])
    stat = path.stat()
    key = (str(path.resolve()), stat.st_mtime_ns, stat.st_size)
    if key not in _CACHE:
        if len(_CACHE) > 2000:
            _CACHE.clear()
        _CACHE[key] = _inspect(path, stat.st_size)
    return _CACHE[key]


def _inspect(path: Path, size: int) -> ThumbCheck:
    import cv2
    import numpy as np

    kb = round(size / 1024)
    if size == 0:
        return ThumbCheck(False, ["thumbnail file is empty"], kb=kb)
    image = cv2.imdecode(np.fromfile(str(path), dtype=np.uint8), cv2.IMREAD_COLOR)
    if image is None:
        return ThumbCheck(False, ["thumbnail is not a readable image"], kb=kb)
    height, width = image.shape[:2]
    issues: list[str] = []
    if size > MAX_BYTES:
        issues.append(f"file is {kb} KB; YouTube allows at most 2048 KB")
    # 16:9 for every video; a Short may also carry a tall 9:16 card.
    tall = height > width
    need_w, need_h, ratio = (MIN_HEIGHT, MIN_WIDTH, 1 / ASPECT) if tall else (MIN_WIDTH, MIN_HEIGHT, ASPECT)
    if width < need_w or height < need_h:
        issues.append(f"too small ({width}x{height}); needs at least {need_w}x{need_h}")
    if abs(width / height - ratio) > ratio * 0.02:
        issues.append(f"not {'9:16' if tall else '16:9'} ({width}x{height})")
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    if float(gray.std()) < 10:
        issues.append("looks blank (almost one flat colour)")
    mean = float(gray.mean())
    if mean < 25 or float((gray < 16).mean()) > 0.85:
        issues.append("almost completely black")
    elif mean > 240:
        issues.append("almost completely white")
    return ThumbCheck(not issues, issues, width, height, kb)


def _shrink_to_limit(path: Path) -> None:
    """Re-save an over-size JPEG at lower quality; size and picture stay the same."""
    import cv2
    import numpy as np

    image = cv2.imdecode(np.fromfile(str(path), dtype=np.uint8), cv2.IMREAD_COLOR)
    if image is None:
        return
    for quality in (90, 82, 74, 66):
        ok, data = cv2.imencode(".jpg", image, [cv2.IMWRITE_JPEG_QUALITY, quality])
        if ok and len(data) <= MAX_BYTES:
            path.write_bytes(data.tobytes())
            return


def ensure_ready(path: str | Path | None,
                 regenerate: Callable[[], Path | str | None] | None = None) -> Path:
    """Return a thumbnail that passes the checks, fixing it once if possible."""
    result = check_thumbnail(path)
    if not result.ok and path and Path(path).is_file() and result.kb * 1024 > MAX_BYTES:
        _shrink_to_limit(Path(path))
        result = check_thumbnail(path)
    if result.ok:
        return Path(path)
    first_problem = result.summary
    if regenerate is not None:
        try:
            fresh = regenerate()
        except Exception as exc:
            raise ThumbnailNotReady(f"{first_problem}; remaking it failed: {exc}") from exc
        if fresh:
            again = check_thumbnail(fresh)
            if again.ok:
                print(f"[thumbnail] remade after a failed check ({first_problem})")
                return Path(fresh)
            raise ThumbnailNotReady(f"{first_problem}; the remade one also failed: {again.summary}")
    raise ThumbnailNotReady(first_problem)
