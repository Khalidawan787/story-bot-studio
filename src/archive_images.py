"""The real photo of the person or case a fact-only video is about.

On a fact channel (verified_facts.py) the opening scene, and every scene that
names the subject, shows the subject's own free photo from Wikipedia/Commons.
The other scenes use video (stock_video.py). Only files free to reuse are
taken, each with its credit line for the video description.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from .models import Scene

_STATE_FILE = "archive_photos.json"
# Pictures that say nothing on screen: flags, logos, icons, signatures.
_SKIP_NAMES = re.compile(r"flag|logo|icon|signature|coat[ _]of[ _]arms|seal[ _.]|ribbon|commons-|"
                         r"symbol|emblem|question_book|edit-clear|disambig", re.I)
_MIN_SIDE = 320
_MAX_PHOTOS = 4


def _subject(run_dir: Path) -> str:
    from .stock_video import SUBJECT_FILE

    path = run_dir / SUBJECT_FILE
    return path.read_text(encoding="utf-8").strip() if path.exists() else ""


def _photos(title: str) -> list[tuple[str, dict]]:
    """[(url, credit row)] of the article's free photos, its lead photo first."""
    from .page_posts import _PHOTO_INFO, _usable
    from .verified_facts import _api

    lead = (_api({"action": "query", "prop": "pageimages", "piprop": "name", "redirects": "1",
                  "titles": title}).get("query", {}).get("pages") or [{}])[0].get("pageimage") or ""
    pages = _api({"action": "query", "generator": "images", "gimlimit": "50", "redirects": "1",
                  "titles": title, **_PHOTO_INFO}).get("query", {}).get("pages") or []
    lead_name = lead.replace("_", " ")
    pages.sort(key=lambda page: 0 if lead_name and str(page.get("title", "")).endswith(lead_name) else 1)
    found: list[tuple[str, dict]] = []
    for page in pages:
        name = str(page.get("title", ""))
        if _SKIP_NAMES.search(name):
            continue
        usable = _usable(page, own_repository=page.get("imagerepository") == "shared")
        if not usable or usable[2] < _MIN_SIDE:
            continue
        info = (page.get("imageinfo") or [{}])[0]
        meta = info.get("extmetadata") or {}
        found.append((usable[0], {
            "title": name.replace("File:", "").rsplit(".", 1)[0][:90],
            "creator": re.sub(r"^Photo: | \(.*$", "", usable[1]) or "Wikimedia Commons",
            "license": str((meta.get("LicenseShortName") or {}).get("value") or "CC"),
            "source_page": str(info.get("descriptionurl") or ""),
        }))
        if len(found) >= _MAX_PHOTOS:
            break
    return found


def _state(run_dir: Path) -> dict:
    """The run's photos, downloaded once."""
    path = run_dir / _STATE_FILE
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    state: dict = {"files": [], "next": 0}
    title = _subject(run_dir)
    if title:
        from .verified_facts import fetch

        folder = run_dir / "archive"
        folder.mkdir(exist_ok=True)
        try:
            photos = _photos(title)
        except Exception as exc:
            print(f"[archive] {title}: no photo ({exc})")
            photos = []
        for number, (url, credit) in enumerate(photos, 1):
            target = folder / f"photo_{number:02}.jpg"
            try:
                target.write_bytes(fetch(url))
            except Exception:
                continue
            state["files"].append({"path": str(target), "credit": credit})
        print(f"[archive] {title}: {len(state['files'])} free photo(s)")
    path.write_text(json.dumps(state, indent=1), encoding="utf-8")
    return state


def _names_subject(scene: Scene, title: str) -> bool:
    """The scene's line names the subject (any word of the title of 4+ letters)."""
    words = {w.lower() for w in re.findall(r"[A-Za-z]{4,}", re.sub(r"\(.*?\)", "", title))}
    line = {w.lower() for w in re.findall(r"[A-Za-z]{4,}", f"{scene.line} {scene.speech or ''}")}
    return bool(words & line)


def photo_for_scene(scene_number: int, scene: Scene, run_dir: Path) -> Path | None:
    """The subject's real photo for scene 1 and scenes naming the subject; else None.

    Each such scene gets the next photo, so one picture is not shown twice in a
    row; when the photos run out the remaining scenes use video.
    """
    title = _subject(run_dir)
    if not title or not (scene_number == 1 or _names_subject(scene, title)):
        return None
    state = _state(run_dir)
    if state["next"] >= len(state["files"]):
        return None
    picture = state["files"][state["next"]]
    state["next"] += 1
    (run_dir / _STATE_FILE).write_text(json.dumps(state, indent=1), encoding="utf-8")
    path = Path(picture["path"])
    gen = run_dir / "gen"
    gen.mkdir(exist_ok=True)
    # Credit line for the video description (pipeline._append_image_credits).
    (gen / f"{path.stem}.wikimedia.json").write_text(json.dumps(picture["credit"]), encoding="utf-8")
    return path
