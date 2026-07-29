"""`openapi.yaml` has to describe the routes the client actually calls.

The file calls itself the reference document and the client library
points consumers at it, so a route the client uses and the spec omits is
worse than an undocumented route: it is a document that looks complete
and is not.

It had drifted to roughly two thirds coverage, and the missing third was
not a random sample. It was the sleep-session mutations, the account
configuration writes, the zone assignment and the whole invitation
group, which is to say the destructive and cross-account surface. A
consumer reading the spec was blind to precisely the parts worth
reading it for.

Compared by SHAPE rather than by literal path, because the two sides
name their parameters differently and always will: the client writes
`{_seg(device_serial)}` and the spec writes `{serial_number}`. Both
normalise to `/v1/devices/{}/live`, which is the thing that has to
agree.
"""

from __future__ import annotations

import inspect
import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SPEC = REPO / "openapi.yaml"

# Every f-string or plain string in the client that looks like a route.
_ROUTE = re.compile(r'"(/v[12]/[^"]*)"')
# Any interpolated segment, however it is spelled on either side.
_PARAM = re.compile(r"\{[^}]*\}")


def shape(path: str) -> str:
    return _PARAM.sub("{}", path)


def client_routes() -> set[str]:
    api = pytest.importorskip("orion_sleep_api.api")
    source = inspect.getsource(api)
    return {shape(match) for match in _ROUTE.findall(source)}


def spec_routes() -> set[str]:
    yaml = pytest.importorskip("yaml")
    spec = yaml.safe_load(SPEC.read_text())
    return {shape(path) for path in spec["paths"]}


def test_every_route_the_client_calls_is_in_the_spec():
    """The direction that matters.

    A consumer building against this document must not discover a route
    by reading the client instead.
    """
    missing = sorted(client_routes() - spec_routes())
    assert missing == [], (
        "the client calls routes the reference document does not "
        f"describe: {missing}"
    )


def test_the_spec_is_not_describing_routes_nobody_calls():
    """The other direction, as a warning rather than a rule.

    The spec legitimately documents more than this client uses: routes
    read out of the vendor app that were never wired up, and onboarding
    and survey endpoints no consumer here touches. So this asserts a
    bound rather than equality, and exists to catch the spec growing
    fiction faster than it grows coverage.
    """
    extra = spec_routes() - client_routes()
    assert len(extra) < len(spec_routes()), (
        "the spec and the client have no routes in common at all, which "
        "means the comparison above is not comparing anything"
    )
