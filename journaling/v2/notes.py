from __future__ import annotations

import re


_FENCED_CODE_RE = re.compile(r"```[\s\S]*?```")
_INLINE_CODE_RE = re.compile(r"`([^`]*)`")
_IMAGE_RE = re.compile(r"!\[([^\]]*)\]\([^)]*\)")
_LINK_RE = re.compile(r"\[([^\]]+)\]\([^)]*\)")
_REF_LINK_RE = re.compile(r"\[([^\]]+)\]\[[^\]]*\]")
_AUTOLINK_RE = re.compile(r"<https?://[^>]+>")
_HEADER_RE = re.compile(r"(?m)^\s{0,3}#{1,6}\s*")
_BLOCKQUOTE_RE = re.compile(r"(?m)^\s{0,3}>\s?")
_ULIST_RE = re.compile(r"(?m)^\s{0,3}[-+*]\s+")
_OLIST_RE = re.compile(r"(?m)^\s{0,3}\d+\.\s+")
_WHITESPACE_RE = re.compile(r"\s+")


def markdown_to_search_text(markdown: str) -> str:
    text = str(markdown or "")
    if not text:
        return ""

    text = _FENCED_CODE_RE.sub(" ", text)
    text = _IMAGE_RE.sub(r"\1", text)
    text = _LINK_RE.sub(r"\1", text)
    text = _REF_LINK_RE.sub(r"\1", text)
    text = _AUTOLINK_RE.sub(" ", text)
    text = _INLINE_CODE_RE.sub(r"\1", text)

    text = _HEADER_RE.sub("", text)
    text = _BLOCKQUOTE_RE.sub("", text)
    text = _ULIST_RE.sub("", text)
    text = _OLIST_RE.sub("", text)

    text = text.replace("**", " ").replace("__", " ")
    text = text.replace("*", " ").replace("_", " ")
    text = text.replace("~~", " ")

    text = _WHITESPACE_RE.sub(" ", text)
    return text.strip()


NOTE_TEMPLATE_THESIS = """## Thesis\n- Setup:\n- Edge:\n- Invalidation:"""

NOTE_TEMPLATE_RISK_PLAN = """## Risk Plan\n- Max loss:\n- Exit trigger:\n- Size check: [ ]"""

NOTE_TEMPLATE_ADJUSTMENT = """## Adjustment\n- What changed:\n- Why now:\n- New risk:"""

NOTE_TEMPLATE_EXIT_REVIEW = """## Exit Review\n- Exit quality:\n- Rule followed: [ ]\n- Improve next time:"""

NOTE_TEMPLATE_LESSON = """## Lesson\n- Pattern observed:\n- Repeat/avoid:\n- Action item:"""

NOTE_TEMPLATE_PSYCHOLOGY = """## Psychology\n- State:\n- Trigger:\n- Reset step: [ ]"""

NOTE_TEMPLATE_EXPERIMENT = """## Experiment\n- Hypothesis:\n- Change:\n- Result:"""
