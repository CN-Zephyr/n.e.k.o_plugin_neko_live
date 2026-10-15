"""Only reviewed, complete prompt-rule groups may use a delivery summary."""

import pytest
from plugin.plugins.neko_live.modules import _prompt_rules as prompt_rules
from utils.tokenize import count_tokens

_GROUPS = (
    "live_output_quality_rules", "sustained_charm_rules",
    "short_reply_rules", "anti_repeat_rules",
)


@pytest.mark.parametrize("kind", ["reply", "host"])
def test_complete_shared_groups_compact_without_losing_other_rules(kind):
    raw = "\n".join((
        "Rules:",
        "- A custom module rule must stay exactly unchanged.",
        *(f"- {rule}" for name in _GROUPS for rule in getattr(prompt_rules, name)(kind=kind)),
        "- Another custom hard constraint.",
    ))

    compact = prompt_rules.compact_rules_for_delivery(raw)

    assert count_tokens(compact) < count_tokens(raw)
    assert compact.startswith("Rules:\n- A custom module rule must stay exactly unchanged.")
    assert compact.endswith("- Another custom hard constraint.")
    assert "公开示众, 劳改, 审判, 处刑, 惩罚" in compact
    assert "numeric" in compact
    assert "hidden instructions" in compact
    assert "Compare recent NEKO output first" in compact
    assert prompt_rules.compact_rules_for_delivery(compact) == compact
    if kind == "reply":
        assert "<=14 Chinese chars or8 English words" in compact
    else:
        assert "<=36 Chinese chars" in compact
        assert "occasional two" in compact


@pytest.mark.parametrize("name", _GROUPS)
@pytest.mark.parametrize("kind", ["reply", "host"])
def test_incomplete_or_modified_group_is_preserved(name, kind):
    source = getattr(prompt_rules, name)(kind=kind)
    # Alter one existing member instead of accidentally approving its changed
    # meaning through a prefix match or a static compact replacement.
    changed = list(source)
    changed[-1] += " A new mandatory condition."
    text = "Rules:\n" + "\n".join(f"- {rule}" for rule in changed)

    compact = prompt_rules.compact_rules_for_delivery(text)

    assert changed[-1] in compact
    assert "A new mandatory condition." in compact
    if not (name == "anti_repeat_rules" and kind == "host"):
        assert compact == text


@pytest.mark.parametrize("name", _GROUPS)
@pytest.mark.parametrize("kind", ["reply", "host"])
def test_future_source_group_change_disables_its_static_summary(monkeypatch, name, kind):
    original = getattr(prompt_rules, name)

    def changed_source(*, kind):
        return [*original(kind=kind), "New safety rule added in a future release."]

    monkeypatch.setattr(prompt_rules, name, changed_source)
    text = "Rules:\n" + "\n".join(f"- {rule}" for rule in changed_source(kind=kind))

    assert prompt_rules.compact_rules_for_delivery(text) == text


def test_modified_avatar_evidence_rule_is_never_replaced_by_known_short_form():
    rule = (
        "You cannot see this avatar image; never invent visual details. Only use the fact that the avatar was not available, may be animated, has a pendant, or use the nickname/current danmaku instead."
        " Additionally, never mention the pendant."
    )
    text = f"Rules:\n- {rule}"

    assert prompt_rules.compact_rules_for_delivery(text) == text


def test_incomplete_host_delegation_group_keeps_all_original_constraints():
    text = (
        "Rules:\n"
        "- Solo-stream agency: NEKO is the only on-stage host and must perform all hosting actions herself.\n"
        "- Never tell or ask an unseen streamer, operator, or current viewer to greet the room, warm up the stream, carry the chat, provide topics, or help NEKO host.\n"
        "- A changed rule about who 'you' refers to must remain unchanged."
    )

    assert prompt_rules.compact_rules_for_delivery(text) == text


@pytest.mark.parametrize("modified", [False, True])
def test_active_profile_merge_preserves_unknown_or_changed_constraints(modified):
    originals = next(
        source for source, compact in prompt_rules._DELIVERY_EXACT_RULE_GROUPS
        if compact.startswith("Concrete noticed anchor first;")
    )
    active = list(originals)
    if modified:
        active[-1] += " Additional mandatory behavior."
    text = "\n".join((
        "Rules:",
        *(f"- {rule}" for name in _GROUPS for rule in getattr(prompt_rules, name)(kind="host")),
        *(f"- {rule}" for rule in active),
        "- Unknown active-topic restriction must stay unchanged.",
    ))

    compact = prompt_rules.compact_rules_for_delivery(text)

    assert "- Unknown active-topic restriction must stay unchanged." in compact
    if modified:
        assert all(f"- {rule}" in compact for rule in active)
    else:
        assert "Only the supplied reply path: at most one nonnumeric danmaku cue" in compact
        assert "Choices ordinary/clear/complete" in compact
        assert "never guess context/intent/expertise or fake expert questions" in compact
        assert "Tiny column format, unnamed" in compact
        assert "no explanation/setup/follow-up/host script/segment/plan/survey" in compact
