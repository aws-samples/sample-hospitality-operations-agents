# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""``GET /properties`` -- the property picker's list, scoped by the platform.

This is a **pass-through proxy and nothing else**, and that is the whole point.

The platform already has the endpoint this needs. ``src/pms/reporting/list_properties.py``
exists, in its own words, "to populate the property selector for chain-level users and
to resolve the property name for property-scoped users", and it scopes the answer itself:

* ``Admin`` / ``Manager`` / ``RevenueManager`` -- every active property.
* ``RegionalManager`` -- the properties in ``custom:region`` (all of them if unset).
* ``FrontDesk`` / ``Housekeeping`` -- exactly one, their own, *with its name*.
* anything else -- an empty list.

So this function forwards the operator's **own** ID token and returns what comes back.
It deliberately contains no scoping logic, because any it contained would be a second,
weaker copy of the rule above -- and a copy that drifted would either leak the chain's
property list to a housekeeper or hide properties from a manager. The platform's
authorizer is the only authority here, exactly as it is for the agents' tool Lambdas.

Two things it deliberately does not do
--------------------------------------
**No caching.** The response is per-caller, so a module-scope cache would have to be
keyed on the caller's groups, property and region -- and a cache keyed on identity is
precisely the kind of thing that is one refactor away from serving the wrong operator's
list. The console fetches this once per sign-in; 50 rows is not worth the risk.

**No reshaping.** The platform's envelope goes back verbatim, the same rule the tool
layer follows. If the platform starts returning a new field, the picker can use it
without a change here.
"""

from __future__ import annotations

import os
from typing import Any

from hotel_console.api import ApiError, Caller, handler_for
from hotel_console.platform import get_as_caller

#: Read at import so a missing variable fails the cold start, not the first request.
PMS_API_URL = os.environ["PMS_API_URL"].rstrip("/")


def route(event: dict, caller: Caller) -> dict[str, Any]:
    """Every property this operator may act on, newest platform answer each time."""
    payload = get_as_caller(event, "/properties")

    properties = ((payload or {}).get("data") or {}).get("properties")
    if not isinstance(properties, list):
        raise ApiError(
            502,
            "UNEXPECTED_PLATFORM_SHAPE",
            "The platform's /properties reply had no data.properties list.",
        )

    return {
        "properties": properties,
        # What the console needs to decide whether to render a picker at all, without
        # re-deriving the platform's scoping rule from the caller's groups. One
        # property and a property-scoped claim means a label, not a dropdown.
        "scope": {
            "boundToOneProperty": caller.property_id is not None,
            "region": caller.region,
        },
    }


handler = handler_for(route)
