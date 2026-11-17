"""Hook A/B tests: two ways to open a Short, taken in turns, so the weekly
scorecard can show which one holds viewers better (the owner's choice,
2026-11-17, from the growth plan V2).

Each Short of a tested channel gets the next style in turn; the style is
written into the script prompt and kept in the table hook_tests (channel,
topic key, style), which the scorecard joins with the videos. Only Shorts:
the long videos are too few to compare. Facts rules stay the same: the hook
only decides WHICH true fact opens the video.
"""
from __future__ import annotations

from datetime import datetime, timezone

STYLES: dict[str, dict[str, str]] = {
    "world_valor_chronicles": {
        "consequence": (
            "HOOK STYLE for this video: CONSEQUENCE FIRST. Scene 1 shows the decisive deed or its "
            "result, the moment everything turned, in plain words. Birth, dates and background come "
            "only after the viewer is hooked."
        ),
        "context": (
            "HOOK STYLE for this video: CONTEXT FIRST. Scene 1 sets the situation and what was at "
            "stake (the danger, the odds, what could be lost), as a striking fact from the SOURCE. "
            "Hold the decisive deed back until the middle of the story."
        ),
    },
    "crime": {
        "clue": (
            "HOOK STYLE for this video: CLUE FIRST. Scene 1 states one concrete piece of evidence or "
            "a mistake from the SOURCE (an object, a record, a slip) that mattered to the case. Then "
            "tell the case in order."
        ),
        "timeline": (
            "HOOK STYLE for this video: TIMELINE. Scene 1 opens at the first tense moment of the case "
            "as the SOURCE records it, and the story runs forward in time; the key evidence appears "
            "where it happened."
        ),
    },
    "ain_news_alert": {
        "headline": (
            "HOOK STYLE for this video: HEADLINE FIRST. Scene 1 states the news itself in one plain "
            "sentence: who did what, where."
        ),
        "impact": (
            "HOOK STYLE for this video: IMPACT FIRST. Scene 1 says why this news matters to an "
            "ordinary viewer, using only what the headlines support; the headline facts follow in "
            "scene 2."
        ),
    },
}


def _table(conn) -> None:
    conn.execute("""CREATE TABLE IF NOT EXISTS hook_tests (
        channel TEXT NOT NULL, topic TEXT NOT NULL, style TEXT NOT NULL, created_at TEXT NOT NULL,
        PRIMARY KEY (channel, topic))""")


def next_style(channel_id: str) -> tuple[str, str] | None:
    """(style, prompt words) for the next Short of this channel, or None when it is not tested."""
    styles = STYLES.get(channel_id)
    if not styles:
        return None
    from .db import connect

    names = sorted(styles)
    conn = connect()
    try:
        _table(conn)
        done = conn.execute("SELECT COUNT(*) FROM hook_tests WHERE channel = ?", (channel_id,)).fetchone()[0]
    finally:
        conn.close()
    style = names[int(done) % len(names)]
    return style, styles[style]


def record(channel_id: str, topic: str, style: str) -> None:
    """Remember which hook this topic's script was written with. Never fails the caller."""
    try:
        from .db import connect

        conn = connect()
        try:
            _table(conn)
            conn.execute(
                "INSERT OR REPLACE INTO hook_tests (channel, topic, style, created_at) VALUES (?, ?, ?, ?)",
                (channel_id, topic, style, datetime.now(timezone.utc).isoformat()))
            conn.commit()
        finally:
            conn.close()
        print(f"[hook test] {channel_id}: '{topic}' written with the {style} hook")
    except Exception as exc:
        print(f"[hook test] not recorded ({exc})")


def style_of(conn, channel_id: str, topic: str) -> str:
    """The hook style a video's topic was written with ('' when untested). The bank
    may have added a suffix to the key ('_2'), so a topic starting with it counts."""
    _table(conn)
    for key, style in conn.execute("SELECT topic, style FROM hook_tests WHERE channel = ?", (channel_id,)):
        if topic == key or topic.startswith(f"{key}_"):
            return style
    return ""
