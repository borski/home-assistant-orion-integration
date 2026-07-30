"""Orion Intelligence temperature-recommendations sensor.

Built against the real `/v1/sleep-schedules` -> `response.recommendations`
shape probed 2026-07-30. The load-bearing distinction: a recommendations
key that is present with an EMPTY list is a valid "zero recommendations"
state (native_value 0, sensor available); the key being ABSENT is
unavailable. Those are different facts and must not collapse.

Kevin Klaes found this field but only ever saw it empty. The populated item
schema here is measured live: bedtime/phase_1/phase_2/wakeup temps plus
provenance.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

from homeassistant.config_entries import ConfigEntryState

from tests_ha.conftest import ACCOUNT, FakeClient, make_entry

MEASURED_ITEM = {
    "bedtime_temp": 26.5,
    "phase_1_temp": 26,
    "phase_2_temp": 28.5,
    "wakeup_temp": 30,
    "thermal_classification": "neutral",
    "source": "sleep_optimization_test",
    "version": "1.4",
    "created_at": "2026-07-19T06:51:30.498Z",
}


class RecClient(FakeClient):
    """A fake whose recommendations map the test sets."""

    recommendations: dict | None = None


async def _noop(*_a: Any, **_k: Any) -> None:
    return None


async def _setup_with_recs(hass, recs: dict | None):
    client = RecClient()
    client.recommendations = recs

    def _make(*a: Any, **k: Any) -> RecClient:
        return client

    with (
        patch("custom_components.orion_sleep.OrionApiClient", side_effect=_make),
        patch(
            "custom_components.orion_sleep.coordinator.OrionWebSocketManager"
        ) as ws,
    ):
        ws.return_value.async_start = _noop
        ws.return_value.async_stop = _noop
        ws.return_value.is_fresh = lambda *_a, **_k: False
        ws.return_value.state = lambda *_a, **_k: "stopped"
        ws.return_value.last_message_at = lambda *_a, **_k: 0.0
        ws.return_value.sync_to_serials = lambda *_a, **_k: None
        entry = make_entry(hass, data={"_account_id_v3": ACCOUNT})
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        assert entry.state is ConfigEntryState.LOADED
    return entry


def _find(hass) -> str | None:
    for state in hass.states.async_all("sensor"):
        if state.attributes.get("translation_key") == "temperature_recommendations":
            return state.entity_id
    # translation_key isn't surfaced in state attributes; fall back to the
    # unique-id-derived entity by searching friendly names.
    for state in hass.states.async_all("sensor"):
        if "temperature_recommendations" in state.entity_id:
            return state.entity_id
    return None


async def test_populated_recommendation_reports_count_and_items(hass):
    await _setup_with_recs(hass, {ACCOUNT: [MEASURED_ITEM]})
    eid = _find(hass)
    assert eid is not None
    state = hass.states.get(eid)
    assert state.state == "1"
    recs = state.attributes["recommendations"]
    assert recs[0]["bedtime_temp"] == 26.5
    assert recs[0]["thermal_classification"] == "neutral"


async def test_empty_list_is_zero_not_unavailable(hass):
    # key present, list empty: a real "no recommendation yet" state.
    await _setup_with_recs(hass, {ACCOUNT: []})
    eid = _find(hass)
    assert eid is not None
    state = hass.states.get(eid)
    assert state.state == "0"
    assert "recommendations" not in state.attributes


async def test_absent_key_is_unavailable(hass):
    # recommendations key not returned at all: unavailable, never 0.
    await _setup_with_recs(hass, None)
    eid = _find(hass)
    assert eid is not None
    state = hass.states.get(eid)
    assert state.state == "unavailable"
