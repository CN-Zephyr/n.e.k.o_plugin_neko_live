"""Small text classifiers owned by the danmaku response path."""

from __future__ import annotations

from . import active_topic_mentions
from .active_topic_filters import is_reaction_only

__all__ = ["is_reaction_only", "is_viewer_to_viewer_mention_text"]


def is_viewer_to_viewer_mention_text(text: str) -> bool:
    return active_topic_mentions.is_viewer_to_viewer_mention_text(text)
