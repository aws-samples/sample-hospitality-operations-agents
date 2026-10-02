# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Loads the system prompts from ``agents/prompts/*.md``.

The prompts are markdown files rather than Python string literals for one
practical reason: they are the part of this system most likely to need editing
by someone who is not editing code, and a `.md` file next to the others is
reviewable in a diff without reading around it.

Loaded once and cached. In a warm Runtime container this is read on the first
delegation and never again, and a cached prompt keeps the Bedrock prompt cache
prefix byte-identical across invocations -- which is the whole reason the cache
hits at all.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

PROMPT_DIR = Path(__file__).parent / "prompts"

#: Every prompt this system expects to exist. Named explicitly rather than
#: globbed so a prompt that failed to make it into the deployment package fails
#: on the first delegation with a message that says which file is missing,
#: instead of producing an agent with no instructions.
NAMES = (
    "orchestrator",
    "arrivals",
    "housekeeping",
    "billing",
    "night_audit",
    "regional",
)


@lru_cache(maxsize=None)
def load(name: str) -> str:
    if name not in NAMES:
        raise ValueError(f"Unknown prompt {name!r}; expected one of {list(NAMES)}")

    path = PROMPT_DIR / f"{name}.md"
    try:
        text = path.read_text(encoding="utf-8").strip()
    except FileNotFoundError as exc:
        raise RuntimeError(
            f"System prompt {path} is missing from the deployment package. "
            f"scripts/build_agents.sh must copy agents/prompts/ alongside the code."
        ) from exc

    if not text:
        raise RuntimeError(f"System prompt {path} is empty.")
    return text
