# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""A3 -- Billing & Folio Integrity. Tier 2, propose-and-confirm."""

from __future__ import annotations

from strands import tool

from subagents.base import delegate

KEY = "billing"
TARGET = "billing"


@tool
async def billing_agent(request: str) -> str:
    """Folio integrity, charges, failed payments, and loyalty points.

    Delegate here for: a folio that looks wrong, a missing or duplicated or
    mis-posted charge, an unsettled balance before checkout, a failed payment,
    and anything involving a guest's loyalty points ledger.

    It reads folios line by line -- folio totals hide exactly the errors worth
    finding -- and checks the loyalty ledger before proposing any goodwill
    gesture, so the same incident is not compensated twice.

    **It cannot move money on its own.** Posting a charge, voiding a folio, and
    adjusting loyalty points each require an approval token a human issues in the
    ops console; without one, the write is rejected by a gate outside the agent.
    So a request with no token comes back as a *proposal*: the folio, the amount,
    the instrument, and the reason. If you were given an approval token, pass it
    through in your request text and say what it authorizes.

    Note there is no endpoint that voids a single charge -- reversing one line
    means posting a negative adjustment, and voiding a folio voids all of it.

    Args:
        request: The folio, guest, or symptom to investigate, naming the property
            and any folio or guest IDs you already know. Include an approval
            token verbatim if a human issued one.
    """
    return await delegate(key=KEY, target=TARGET, request=request)
