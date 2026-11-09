"""Observed host examples remain optional inside the existing prompt budget."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from plugin.plugins.neko_live.adapters.neko_dispatcher import (
    ReplyPreparation,
    _append_plugin_output_contract,
)
from plugin.plugins.neko_live.core.contracts import (
    InteractionRequest,
    ViewerEvent,
    ViewerIdentity,
    ViewerProfile,
)
from plugin.plugins.neko_live.core.live_output_contract_prompt import render_contract_instruction
from plugin.plugins.neko_live.core.live_output_memory import compact_recent_reply_values
from plugin.plugins.neko_live.core.live_reply_contract import build_reply_metadata
from plugin.plugins.neko_live.core.prompt_delivery import (
    HOST_CALLBACK_TOKEN_BUDGET,
    fit_live_callback_prompt,
)
from utils.tokenize import count_tokens

_HOST_REPLIES = tuple(f"host example {index}: " + "distinct wording " * 4 for index in range(6))
_REPEAT_RULE = "- Do not copy NEKO's previous wording."
_HARD_RULE = "- Preserve this independent hard constraint exactly."


def _metadata(*, expanded=False):
    metadata = build_reply_metadata(
        uid="viewer", live_mode="solo_stream", response_module_hint="danmaku_response",
    )
    if expanded:
        metadata.update(danmaku_profile="content_request", reply_length_mode="expanded", max_reply_chars=56)
    else:
        metadata["danmaku_profile"] = "emoji_or_reaction"
    return metadata


def _plugin(local_replies=()):
    return SimpleNamespace(runtime=SimpleNamespace(
        recent_results=[{"status": "pushed", "output": reply} for reply in local_replies],
    ))


@pytest.mark.parametrize("expanded", [False, True])
def test_host_examples_fit_the_baseline_budget_without_removing_hard_rules(expanded):
    metadata = _metadata(expanded=expanded)
    prompt = "\n\n".join((
        "NEKO Live delivery boundary:\n- live room",
        "[NEKO Live danmaku response]\ndanmaku: hello",
        "Rules:\n" + _REPEAT_RULE + "\n" + _HARD_RULE,
    ))
    baseline = _append_plugin_output_contract(prompt, metadata=metadata, plugin=_plugin())
    with_host = _append_plugin_output_contract(
        prompt, metadata=metadata, plugin=_plugin(), recent_host_replies=_HOST_REPLIES,
    )
    baseline_contract = render_contract_instruction([{"metadata": metadata}]).strip()
    budget = count_tokens(baseline)

    assert count_tokens(with_host) > budget
    assert "Avoid recent:" not in with_host
    assert with_host.count("  - Avoid repeating: ") == 6
    fitted = fit_live_callback_prompt(with_host, budget=budget)

    assert count_tokens(fitted) <= budget
    assert baseline_contract in fitted
    assert _HARD_RULE in fitted
    assert _REPEAT_RULE in fitted
    assert "host example" not in fitted


@pytest.mark.parametrize("expanded", [False, True])
def test_local_and_host_examples_share_six_optional_lines(expanded):
    local = ("old local zero", "old local one", "shared wording", "local retained")
    host = ("shared wording", "host two", "host three", "host four " + "x" * 40)
    text = _append_plugin_output_contract(
        "reply", metadata=_metadata(expanded=expanded), plugin=_plugin(local),
        recent_host_replies=host,
    )

    expected = compact_recent_reply_values([*local, *host])
    lines = [line.removeprefix("  - Avoid repeating: ") for line in text.splitlines()
             if line.startswith("  - Avoid repeating: ")]
    assert lines == expected
    assert len(lines) == 6
    assert text.count("shared wording") == 1
    assert text.count("Recent NEKO Live outputs below") == 1
    assert "Avoid recent:" not in text
    assert "x" * 25 not in text


def test_no_host_examples_keep_the_existing_contract_and_marker_behavior():
    local = ("old local line", "another local line")
    metadata = _metadata()
    expected = "reply\n\n" + render_contract_instruction(
        [{"metadata": metadata}], recent_live_replies=list(local),
    ).strip()
    assert _append_plugin_output_contract("reply", metadata=metadata, plugin=_plugin(local)) == expected
    existing = "reply\nNEKO Live short output contract: already present"
    assert _append_plugin_output_contract(
        existing, metadata=metadata, plugin=_plugin(local), recent_host_replies=_HOST_REPLIES,
    ) == existing


def _request(runtime, padding, *, trusted_rules=True, current_input="hello"):
    required = _HARD_RULE + "\n- Required context: " + "anchor " * padding
    prompt = f"[NEKO Live danmaku response]\ndanmaku: {current_input}"
    if trusted_rules:
        prompt += "\n\nRules:\n" + _REPEAT_RULE + "\n" + required
    else:
        # Legacy/minimal builders have no trusted Rules section. The fitter
        # must not reinterpret unknown required text merely to admit examples.
        prompt += "\n" + required
    return InteractionRequest(
        event=ViewerEvent(
            uid="viewer", nickname="viewer", source="live_danmaku", danmaku_text=current_input,
            raw={"_live_session_generation": runtime._live_session_generation},
        ),
        identity=ViewerIdentity(uid="viewer", nickname="viewer"),
        profile=ViewerProfile(uid="viewer", nickname="viewer"),
        prompt_text=prompt,
        live_mode="solo_stream", strength="normal",
        metadata={"danmaku_profile": "emoji_or_reaction"},
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("trusted_rules", [True, False])
async def test_real_dispatch_near_thousand_tokens_stays_deliverable_with_host_examples(runtime, trusted_rules):
    runtime._accepting_live_events = True
    runtime._live_session_generation = 1
    runtime._live_listener_operation_id = 1
    runtime.live_target_lanlan = "Cat"
    runtime.live_connection_state = "connected"
    runtime.config.dry_run = False
    runtime.plugin.runtime = runtime

    # Find a nearly full required prompt using the actual dispatcher and token
    # counter; no boundary/contract implementation is duplicated in the test.
    low, high = 0, HOST_CALLBACK_TOKEN_BUDGET * 2
    while low < high:
        padding = (low + high + 1) // 2
        result = await runtime.dispatcher.push_roast(_request(runtime, padding, trusted_rules=trusted_rules))
        if result.startswith("queued_to_neko("):
            low = padding
        else:
            high = padding - 1
    baseline_request = _request(runtime, low, trusted_rules=trusted_rules)
    baseline_result = await runtime.dispatcher.push_roast(baseline_request)
    assert baseline_result.startswith("queued_to_neko(")
    baseline = runtime.plugin.pushed_messages[-1]["parts"][0]["text"]
    assert HOST_CALLBACK_TOKEN_BUDGET - 20 <= count_tokens(baseline) <= HOST_CALLBACK_TOKEN_BUDGET

    request = _request(runtime, low, trusted_rules=trusted_rules)
    prepared = ReplyPreparation(
        runtime.dispatcher._reply_scope(request), runtime.dispatcher._reply_reader_epoch, _HOST_REPLIES,
    )
    result = await runtime.dispatcher.push_roast(request, prepared_replies=prepared)

    assert result.startswith("queued_to_neko(")
    delivered = runtime.plugin.pushed_messages[-1]["parts"][0]["text"]
    assert count_tokens(delivered) <= HOST_CALLBACK_TOKEN_BUDGET
    assert _HARD_RULE in delivered
    if trusted_rules:
        assert _REPEAT_RULE in delivered
    else:
        assert "Rules:" not in delivered
    assert "- Required context: " + ("anchor " * low).rstrip() in delivered
    assert "target<=14 zh; hard<=28 zh" in delivered
    assert "first character must be spoken dialogue" in delivered
    assert "  - Avoid repeating: " not in delivered


@pytest.mark.asyncio
@pytest.mark.parametrize("input_words,expect_excerpt", [(150, False), (260, True)])
async def test_host_examples_do_not_take_budget_from_current_input(runtime, input_words, expect_excerpt):
    runtime._accepting_live_events = True
    runtime.live_target_lanlan = "Cat"
    runtime.config.dry_run = False
    current_input = "alpha " * input_words + "END_OF_QUESTION"
    baseline_request = _request(runtime, 531, trusted_rules=False, current_input=current_input)
    assert (await runtime.dispatcher.push_roast(baseline_request)).startswith("queued_to_neko(")
    baseline = runtime.plugin.pushed_messages[-1]["parts"][0]["text"]
    assert ("[truncated]" in baseline) is expect_excerpt
    assert ("END_OF_QUESTION" not in baseline) is expect_excerpt

    request = _request(runtime, 531, trusted_rules=False, current_input=current_input)
    prepared = ReplyPreparation(
        runtime.dispatcher._reply_scope(request), runtime.dispatcher._reply_reader_epoch, _HOST_REPLIES,
    )
    assert (await runtime.dispatcher.push_roast(request, prepared_replies=prepared)).startswith("queued_to_neko(")
    assert runtime.plugin.pushed_messages[-1]["parts"][0]["text"] == baseline
