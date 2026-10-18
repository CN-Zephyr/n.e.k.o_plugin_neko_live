"""Narrow current-viewer visual requests; ordinary replies stay text-only."""

from __future__ import annotations

import re

_QUOTED = re.compile(
    r'''"[^"\r\n]*"|“[^”\r\n]*”|「[^」\r\n]*」|『[^』\r\n]*』|‘[^’\r\n]*’|'''
    r"(?<!\w)'(?:[^'\r\n]|(?<=\w)'(?=\w))*'(?!\w)"
)
_DENIED = re.compile(
    r"(?:别|別|不要|不用|不必|无需|無需|停止|不想|不需要|不愿|不願).{0,16}"
    r"(?:看|评|評|描述|谈|談|聊|分析|讨论|討論|说|說|讲|講|提)"
    r"|不是(?:让|讓|叫|要|请|請).{0,8}(?:看|评|評|描述|谈|談|聊|分析|说|說)"
    r"|\b(?:don['’]t|do\s+not|stop|never|no\s+need\s+to|not\s+asking|not\s+ask)\b.{0,48}"
    r"\b(?:look|describe|review|rate|judge|roast(?:ing)?|analy[sz]e|discuss|talk|mention)\b"
    r"|\bleave\b.{0,48}\b(?:avatar|pfp|face)\b.{0,12}\balone\b",
    re.I,
)
_REPORTED = re.compile(
    r"(?:他|她|他们|她们|他們|她們|有人)(?:说|說|问|問)"
    r"|\b(?:he|she|they|someone)\s+(?:said|says|asked|asks|told)\b", re.I,
)


def _has_own_avatar_reference(text: str) -> bool:
    dense = "".join(char for char in text.casefold() if char.isalnum())
    if any(marker in dense for marker in (
        "我的头像", "我头像", "我的頭像", "我頭像", "本人头像", "本人頭像", "给我看头像", "給我看頭像",
        "我的形象", "我长什么样", "我長什麼樣", "我长啥样", "我的脸", "我的臉",
    )):
        return True
    return re.search(r"\bmy\s+(?:avatar|pfp|profile\s+(?:pic|picture)|face|appearance)\b|\bhow\s+do\s+i\s+look\b", text, re.I) is not None


def _unquoted_clauses(text: str) -> list[str]:
    return re.split(r"[，,。！？!?;；\r\n]+", _QUOTED.sub("", text))


def own_avatar_request_clauses(text: str) -> list[str]:
    # Classify only the unquoted, non-denied current-speaker clauses. This
    # deliberately degrades ambiguous reported speech to an ordinary reply.
    return [
        clause for clause in _unquoted_clauses(text)
        if _has_own_avatar_reference(clause) and not _DENIED.search(clause) and not _REPORTED.search(clause)
    ]


def requests_own_avatar(text: str) -> bool:
    return bool(own_avatar_request_clauses(text))


def declines_own_avatar(text: str) -> bool:
    return not requests_own_avatar(text) and any(
        _has_own_avatar_reference(clause) and _DENIED.search(clause) and not _REPORTED.search(clause)
        for clause in _unquoted_clauses(text)
    )
