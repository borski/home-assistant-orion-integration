"""v3 / Orion Intelligence analytics sensors.

The spec's load-bearing rule: these metrics report `unknown` on an empty
period and on a no-subscription account, NEVER `0`. Zero sleep debt (you
met your need) and no data (nothing was measured) are different facts, and
collapsing them to 0 would tell an automation the opposite of the truth.

Built against the real v3 shape probed 2026-07-30: top-level
`has_subscription`, `granularities.{day,week,month}.data.<period>` each
with `overview` and `metrics`, every metric carrying `value` (nullable),
`unit`, `insight`, `comparisons`, `state`, `status`.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

from homeassistant.config_entries import ConfigEntryState

from tests_ha.conftest import ACCOUNT, FakeClient, make_entry


def _day_period(metrics: dict) -> dict:
    return {
        "period_key": "2026-07-30",
        "granularity": "day",
        "start_date": "2026-07-30",
        "end_date": "2026-07-30",
        "days_with_data": 1,
        "overview": {"score": 91, "rating": "Excellent", "award": True},
        "metrics": metrics,
    }


def _v3_payload(day_metrics: dict, *, has_subscription: bool = True) -> dict:
    return {
        "user_id": ACCOUNT,
        "has_subscription": has_subscription,
        "granularities": {
            "day": {
                "range": {"start_date": "2026-07-30", "end_date": "2026-07-30"},
                "data": {"2026-07-30": _day_period(day_metrics)},
            }
        },
    }


class V3Client(FakeClient):
    """A fake whose v3 payload the test sets, primary account only."""

    v3_payload: dict | None = None

    async def get_insights_v3(self, *, expected_user_id=None) -> dict:
        self.calls.append("get_insights_v3")
        return self.v3_payload or {
            "user_id": expected_user_id or ACCOUNT,
            "has_subscription": True,
            "granularities": {},
        }


async def _setup_with_v3(hass, v3_payload: dict):
    client = V3Client()
    client.v3_payload = v3_payload

    def _make(*a: Any, **k: Any) -> V3Client:
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


async def _noop(*_a: Any, **_k: Any) -> None:
    return None


async def test_a_real_sleep_debt_value_reports_the_number(hass):
    # value 0 with status balanced: a REAL zero (you met your need), which
    # must be reported as 0, not unknown.
    await _setup_with_v3(
        hass,
        _v3_payload(
            {
                "sleep_debt": {
                    "value": 0,
                    "need": 408,
                    "unit": "min",
                    "status": "balanced",
                    "state": "balanced",
                    "insight": "You met your sleep need.",
                    "comparisons": {"vs_prior_day": {"delta": -25.6}},
                }
            }
        ),
    )
    state = hass.states.get("sensor.sleepy_sleep_debt")
    assert state is not None, "sleep debt sensor was not created"
    assert state.state == "0", (
        "a real zero sleep debt was not reported as 0. Meeting your sleep "
        "need is a fact, not missing data."
    )
    assert state.attributes.get("need") == 408
    assert state.attributes.get("status") == "balanced"


async def test_an_empty_metric_reports_unknown_not_zero(hass):
    # value null with state calibrating: NO data. Must be unknown, not 0.
    await _setup_with_v3(
        hass,
        _v3_payload(
            {
                "sleep_debt": {
                    "value": None,
                    "unit": "min",
                    "state": "calibrating",
                    "status": "calibrating",
                    "insight": "Calibrating.",
                    "comparisons": {},
                }
            }
        ),
    )
    state = hass.states.get("sensor.sleepy_sleep_debt")
    assert state is not None
    assert state.state == "unknown", (
        "an empty metric reported a number. A null value is no data and "
        f"must be unknown, never 0: got {state.state!r}"
    )


async def test_no_subscription_reports_unknown_not_zero(hass):
    # has_subscription False: the whole metric surface is empty. Even a
    # metric that happens to carry a number must read as unknown, because
    # without a subscription the data is not real.
    await _setup_with_v3(
        hass,
        _v3_payload(
            {"consistency": {"value": 88, "unit": "percent"}},
            has_subscription=False,
        ),
    )
    state = hass.states.get("sensor.sleepy_consistency")
    assert state is not None
    assert state.state == "unknown", (
        "a no-subscription account reported a metric value. Without a "
        f"subscription the surface is empty and must read unknown: {state.state!r}"
    )


async def test_breathing_disturbances_cross_references_the_v2_ahi(hass):
    """The reconciliation the spec requires.

    v3 breathing_disturbances (seconds) and the v2 apnea suite (AHI) are
    different measurements. They stay separate sensors, but the v3 one
    carries the matching v2 AHI as an attribute so they are visibly linked
    rather than silently divergent.
    """
    client = V3Client()
    client.v3_payload = _v3_payload(
        {
            "breathing_disturbances": {
                "value": 210,
                "unit": "sec",
                "state": "data",
                "details": {"low_seconds": 0, "high_seconds": 210},
                "insight": "Up 1m 30s vs yesterday.",
                "comparisons": {"vs_prior_day": {"delta": 90}},
            }
        }
    )
    # A v2 session carrying an apnea block with an AHI, newest and finished.
    client.insights = {
        "user_id": ACCOUNT,
        "data": {
            "2026-07-30": {
                "sessions": [
                    {
                        "session_id": "s1",
                        "is_in_progress": False,
                        "apnea": {"ahi": 4.2},
                    }
                ]
            }
        },
    }

    async def _get_insights(days: int = 7, *, expected_user_id=None) -> dict:
        return client.insights

    client.get_insights = _get_insights  # type: ignore[assignment]

    def _make(*a: Any, **k: Any) -> V3Client:
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

    state = hass.states.get("sensor.sleepy_breathing_disturbances")
    assert state is not None
    assert state.state == "210"
    assert state.attributes.get("low_seconds") == 0
    assert state.attributes.get("high_seconds") == 210
    assert state.attributes.get("ahi") == 4.2, (
        "the v3 breathing sensor did not cross-reference the matching v2 "
        "AHI, so the two breathing measurements are silently divergent"
    )


async def test_weekly_and_monthly_scores_are_one_sensor_each(hass):
    """One score sensor per granularity, metrics as attributes.

    Not eight metric entities per granularity.
    """
    payload = {
        "user_id": ACCOUNT,
        "has_subscription": True,
        "granularities": {
            "week": {
                "range": {},
                "data": {
                    "2026-W30": {
                        "period_key": "2026-W30",
                        "start_date": "2026-07-26",
                        "end_date": "2026-08-01",
                        "days_with_data": 5,
                        "overview": {"score": 83, "rating": "Good"},
                        "metrics": {
                            "hrv": {
                                "value": 55,
                                "unit": "ms",
                                "insight": "Steady.",
                                "comparisons": {"vs_prior_week": {"delta": 2}},
                            }
                        },
                    }
                },
            }
        },
    }
    await _setup_with_v3(hass, payload)
    state = hass.states.get("sensor.sleepy_weekly_sleep_score")
    assert state is not None, "weekly score sensor missing"
    assert state.state == "83"
    assert state.attributes.get("rating") == "Good"
    # The metric breakdown rides as attributes.
    hrv = state.attributes.get("hrv")
    assert isinstance(hrv, dict) and hrv["value"] == 55
    assert hrv["comparison"] == {"delta": 2}
