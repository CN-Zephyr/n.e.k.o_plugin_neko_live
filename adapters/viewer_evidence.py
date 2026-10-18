"""Keep public speaker attribution adjacent to the delivered utterance."""

from __future__ import annotations

import hashlib
import re

_INPUT_LINE = re.compile(
    r"^(?P<label>(?:- )?(?:danmaku|current_danmaku|current_danmaku_claim|current danmaku):[ \t]*)"
    r"(?P<value>[^\r\n]+)$", re.MULTILINE,
)


def current_evidence_rules(text: str, *, has_avatar_image: bool, avatar_requested: bool = False) -> list[str]:
    """Keep relevant factual boundaries mandatory without taxing every reply."""
    rules = []
    if avatar_requested or has_avatar_image or re.search(r"头像|頭像|形象|长啥样|长什么样|脸|臉|\bavatar\b|\bpfp\b|\bface\b|\bappearance\b", text, re.I):
        state = "attached in this request" if has_avatar_image else "unavailable in this request"
        rules.append(f"- Current viewer avatar: {state}. Never reuse historical images or another viewer's avatar.")
    if re.search(r"什么时候|什麼時候|何时|何時|多久|第一次|首次|认识|認識|相见|相見|记得|記得|\bwhen\b|\bfirst\b|\bmet\b|\bremember\b", text, re.I):
        rules.append("- Never invent first visits or meeting dates; absent explicit records, admit unknown. Past model guesses are not evidence.")
    return rules


def viewer_identity_token(uid: str) -> str:
    """Bounded stable disambiguation, not a promise of UID anonymization."""
    return "v_" + hashlib.sha256(uid.encode("utf-8")).hexdigest()[:12]


def attribute_current_input(text: str, viewer: str, identity_token: str) -> str:
    # Delimiters belong to the plugin, not to a nickname that could close them.
    name = viewer.replace("【", "").replace("】", "") or "viewer"
    prefix = f"【{name}·{identity_token}】"

    def attributed(match: re.Match[str]) -> str:
        value = match["value"]
        if value == "(empty)" or value.startswith(prefix):
            return match.group(0)
        return f"{match['label']}{prefix}{value}"

    return _INPUT_LINE.sub(attributed, text)
