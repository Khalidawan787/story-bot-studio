"""Per-channel video language: English (default) or Urdu.

Urdu videos are written in Roman Urdu (Urdu in English letters, the way most
Pakistani viewers type and search), because that is what goes on screen and
into the title. The Urdu voice cannot read Roman Urdu, so the script also
carries every line in Urdu script as `speech`; only the voice hears that.
Scene labels and picture prompts stay in English, since the image models and
stock-footage search understand English.
"""
from __future__ import annotations

LANGUAGES = {"en": "English", "ur": "Urdu (Roman Urdu on screen)"}

# Microsoft's Pakistani Urdu voices.
_URDU_MALE, _URDU_FEMALE = "ur-PK-AsadNeural", "ur-PK-UzmaNeural"
_FEMALE_HINTS = ("Aria", "Jenny", "Ava", "Emma", "Sonia", "Michelle", "Ana", "Uzma")


def channel_language(channel) -> str:
    value = str(getattr(channel, "language", "") or "en").lower()
    return value if value in LANGUAGES else "en"


def is_urdu(channel) -> bool:
    return channel_language(channel) == "ur"


def voice_for(channel, default: str | None = None) -> str | None:
    """The narrator voice: the channel's own, or the matching Urdu voice."""
    voice = getattr(channel, "voice", None) if channel is not None else None
    voice = voice or default
    if not is_urdu(channel):
        return voice
    if voice and voice.startswith("ur-"):
        return voice
    return _URDU_FEMALE if voice and any(h in voice for h in _FEMALE_HINTS) else _URDU_MALE


# Asked of every Urdu script and translation. Left alone, the writer mixes
# English into every sentence ("volunteer network ko fund kiya jata hai").
PURE_URDU = (
    "Write PURE Urdu: use the Urdu word wherever an everyday one exists (madad, not help; "
    "atiyaat, not donations; idara, not organisation; razakar, not volunteer; sarbarah, not head; "
    "aizaz, not award). It is Pakistani Urdu, so no Hindi words either (waba, not mahamari; "
    "atiya, not daan; sarparast for a guardian; janaza for a funeral). "
    "Do not mix English words into the sentences. Keep only names of people, "
    "places and organisations, and things Urdu itself calls by the English word (ambulance, "
    "hospital, doctor, film).\n"
)


def script_rules(channel) -> str:
    """Extra instructions for the script writer; empty for English channels."""
    if not is_urdu(channel):
        return ""
    return (
        "\nLANGUAGE: this channel publishes in URDU for a Pakistani audience.\n"
        "- 'line' = the narrated sentence(s) in ROMAN URDU: Urdu written in English letters, "
        "the way people type on WhatsApp (e.g. \"Yeh kahani ek purani haveli se shuru hoti hai.\"). "
        "Natural everyday Urdu, not word-by-word translated English.\n"
        "- 'speech' = exactly the same text as 'line', written in Urdu script (اردو), for the narrator's voice.\n"
        "- 'title', 'intro' and 'outro' in Roman Urdu; the title may add one or two English search words.\n"
        "- Keep 'label' and 'image_prompt' in ENGLISH.\n"
        "- Word counts asked for above apply to 'line'.\n"
        "- " + PURE_URDU
    )


def display_marks(marks, display_text: str):
    """Karaoke timings for the on-screen words when the voice read other words.

    The voice speaks Urdu script but the caption shows Roman Urdu, so the word
    lists differ. The spoken span is kept and the shown words are spread over
    it by length, which tracks the voice closely enough for a highlight.
    """
    from .tts import _split_boundary_into_words

    if not marks:
        return marks
    start = marks[0][1]
    end = max(s + d for _w, s, d in marks)
    return _split_boundary_into_words(display_text, start, max(0.1, end - start))
