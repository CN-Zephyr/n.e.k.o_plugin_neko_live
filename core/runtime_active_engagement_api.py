"""Runtime compatibility API for active engagement actions."""

from __future__ import annotations


from . import runtime_active_engagement
from .contracts import InteractionResult
from .runtime_active_topic_api import RuntimeActiveTopicApiMixin
from .runtime_active_topic_rules_api import RuntimeActiveTopicRulesApiMixin


class RuntimeActiveEngagementApiMixin(
    RuntimeActiveTopicApiMixin,
    RuntimeActiveTopicRulesApiMixin,
):
    async def trigger_active_engagement(self) -> InteractionResult:
        return await runtime_active_engagement.trigger_active_engagement(self)

    async def maybe_trigger_active_engagement(self) -> InteractionResult | None:
        return await runtime_active_engagement.maybe_trigger_active_engagement(self)
