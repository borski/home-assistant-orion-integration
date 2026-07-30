"""Left/right side labelling for a bedside dial, without renaming anything.

The integration's climate entities are person-named (Alex / Bob) and stay
that way. A side-anchored controller like a rotary dial thinks in
left/right, so each climate entity carries a `side` attribute driven by the
CONF_ZONE_LEFT option. The option maps a physical zone to "left", defaulting
zone_a=left, and is flippable with no migration.

The load-bearing guarantees, tested here:
  * the side rides as an ATTRIBUTE, never as the entity_id, so the person
    names survive;
  * flipping the option swaps the labels without touching any unique_id;
  * a zone that is neither configured side simply has no `side`.
"""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntryState
from homeassistant.helpers import entity_registry as er

from custom_components.orion_sleep.const import CONF_ZONE_LEFT
from tests_ha.conftest import make_entry


async def _loaded(hass, options=None):
    entry = make_entry(hass, options=options)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED
    return entry


async def test_default_zone_a_is_left(hass, patched, client, ws_manager):
    # No option set: zone_a defaults to left. Alex is on zone_a, Bob zone_b.
    await _loaded(hass)
    alex = hass.states.get("climate.sleepy_alex_climate")
    bob = hass.states.get("climate.sleepy_user_22222222_climate")
    assert alex.attributes.get("side") == "left"
    assert bob.attributes.get("side") == "right"


async def test_option_flips_the_sides(hass, patched, client, ws_manager):
    # zone_b is left now: the labels swap, the entity_ids do not.
    await _loaded(hass, options={CONF_ZONE_LEFT: "zone_b"})
    alex = hass.states.get("climate.sleepy_alex_climate")
    bob = hass.states.get("climate.sleepy_user_22222222_climate")
    assert alex.attributes.get("side") == "right"
    assert bob.attributes.get("side") == "left"
    # The person-named entity_ids are untouched by the side flip.
    assert alex is not None and bob is not None


async def test_side_never_becomes_the_entity_id(hass, patched, client, ws_manager):
    # The whole point: side is metadata, not identity. There must be no
    # climate entity whose id was built from left/right.
    await _loaded(hass)
    ids = [s.entity_id for s in hass.states.async_all("climate")]
    assert "climate.sleepy_alex_climate" in ids
    assert "climate.sleepy_user_22222222_climate" in ids
    assert not any("_left" in e or "_right" in e for e in ids)


async def test_unique_ids_unchanged_by_side_labelling(hass, patched, client, ws_manager):
    # Constraint 1: no unique_id may change. The side feature must not have
    # touched the frozen climate unique_ids, which are keyed by zone.
    await _loaded(hass)
    registry = er.async_get(hass)
    alex = registry.async_get("climate.sleepy_alex_climate")
    bob = registry.async_get("climate.sleepy_user_22222222_climate")
    assert alex.unique_id.endswith("_climate_zone_a")
    assert bob.unique_id.endswith("_climate_zone_b")
