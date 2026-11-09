"""Song controls must be callable through the host's UI authorization boundary."""

from __future__ import annotations

import pytest
from plugin.plugins.neko_live import NekoLivePlugin
from plugin.plugins.neko_live.core.runtime_dashboard_actions import dashboard_actions
from plugin.sdk.plugin.ui import UI_ACTION_META_ATTR
from plugin.sdk.shared.constants import EVENT_META_ATTR
from plugin.server.application.plugins.ui_query_service import (
    _resolve_authorized_action_entry_id,
)


@pytest.mark.parametrize("action_id", ["song_request_status", "song_request_jump"])
def test_song_controls_are_authorized_by_hosted_ui(action_id: str) -> None:
    method = getattr(NekoLivePlugin, action_id)
    entry_meta = getattr(method, EVENT_META_ATTR)
    ui_meta = getattr(method, UI_ACTION_META_ATTR, None)
    # The host derives its action whitelist from decorators, not dashboard state.
    actions = [] if ui_meta is None else [{**ui_meta, "entry_id": entry_meta.id}]

    assert _resolve_authorized_action_entry_id(
        action_id,
        actions=actions,
        entry_ids={entry_meta.id},
    ) == entry_meta.id
    assert _resolve_authorized_action_entry_id(
        action_id,
        actions=dashboard_actions(),
        entry_ids={entry_meta.id},
    ) == entry_meta.id
