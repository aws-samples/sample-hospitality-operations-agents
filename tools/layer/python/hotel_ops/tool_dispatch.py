# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""MCP tool-name routing for an AgentCore Gateway Lambda target.

The Gateway invokes the Lambda with the tool *arguments* as the event and the
tool *name* in the client context:

    context.client_context.custom["bedrockAgentCoreToolName"]

and it prefixes that name with the target name and a **three**-underscore
delimiter, e.g. ``housekeeping___assign_task`` for the tool declared as
``assign_task`` on the ``housekeeping`` target. Handlers must strip that prefix
before routing -- forgetting to is the single most likely first-run bug in this
design, and getting the underscore *count* wrong is the second: a two-underscore
split leaves ``_assign_task``, which looks so nearly right that the resulting
``UNKNOWN_TOOL`` reads like a registration mistake. Verified against the deployed
Gateway, which advertised ``regional___list_properties``.

So the delimiter is never assumed. When the target name is known -- and inside
:class:`ToolRouter` it always is -- the prefix is removed by length and any run of
underscores after it is consumed, which is correct for two, three, or any future
count.

The return value goes straight back to the model, so a handler returns the
foundation's envelope unmodified (``PATTERN_EXTENSION_GUIDE.md`` §3.2).
"""

from __future__ import annotations

import json
import logging
import os
import re
import traceback
from typing import Any, Callable

logger = logging.getLogger()
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

Handler = Callable[[dict], dict]

TOOL_NAME_KEY = "bedrockAgentCoreToolName"


#: Fallback for when the target name is not known: an identifier followed by a run
#: of two or more underscores. Consumes the whole run, so it is right whatever
#: delimiter width the Gateway uses.
_UNKNOWN_TARGET_PREFIX = re.compile(r"^[A-Za-z0-9]+_{2,}")


def strip_target_prefix(tool_name: str, target: str | None = None) -> str:
    """``"housekeeping___assign_task"`` -> ``"assign_task"``.

    Args:
        tool_name: The prefixed name the Gateway supplied.
        target: The target this Lambda serves. When given, the prefix is removed by
            length rather than by searching for a delimiter, so a tool whose own
            name contains underscores -- every one of them does -- cannot be
            mis-split. When omitted, falls back to
            :data:`_UNKNOWN_TARGET_PREFIX`.
    """
    if target and tool_name.startswith(target):
        remainder = tool_name[len(target) :]
        stripped = remainder.lstrip("_")
        # Only a *delimited* match counts. Without this, target "billing" would
        # also swallow the prefix of a hypothetical "billingadmin___post_charge".
        if stripped != remainder:
            return stripped
    return _UNKNOWN_TARGET_PREFIX.sub("", tool_name)


def tool_name_from(context: Any, target: str | None = None) -> str:
    """Pull the unprefixed tool name out of the Lambda context."""
    custom = getattr(getattr(context, "client_context", None), "custom", None) or {}
    raw = custom.get(TOOL_NAME_KEY, "")
    if not raw:
        raise KeyError(
            f"No {TOOL_NAME_KEY} in the Lambda client context. This function is "
            "only invocable as an AgentCore Gateway target."
        )
    return strip_target_prefix(raw, target)


def tool_error(code: str, message: str, **details) -> dict:
    """An error shaped like the foundation's own envelope.

    Used only for failures that happen *before* any API call -- unknown tool,
    missing argument, auth setup problem. Anything the foundation itself returns
    is passed through untouched instead.
    """
    error: dict[str, Any] = {"code": code, "message": message}
    if details:
        error["details"] = details
    return {"success": False, "error": error}


class ToolRouter:
    """Registry of the tools one Gateway target exposes."""

    def __init__(self, target_name: str) -> None:
        self.target_name = target_name
        self._handlers: dict[str, Handler] = {}

    def tool(self, name: str) -> Callable[[Handler], Handler]:
        """Register a handler under its unprefixed MCP tool name."""

        def register(fn: Handler) -> Handler:
            if name in self._handlers:
                raise ValueError(f"tool {name!r} is already registered")
            self._handlers[name] = fn
            return fn

        return register

    @property
    def tool_names(self) -> list[str]:
        return sorted(self._handlers)

    def dispatch(self, event: dict, context: Any) -> dict:
        """Route one Gateway invocation. Never raises."""
        try:
            name = tool_name_from(context, self.target_name)
        except KeyError as exc:
            logger.error("tool name missing from client context")
            return tool_error("INVALID_INVOCATION", str(exc))

        handler = self._handlers.get(name)
        if handler is None:
            # A tool the schema declares but this Lambda does not implement, or
            # a tool routed to the wrong target. Either way the agent must not be
            # told it succeeded.
            logger.error("unknown tool %r on target %r", name, self.target_name)
            return tool_error(
                "UNKNOWN_TOOL",
                f"{self.target_name!r} does not implement tool {name!r}",
                available=self.tool_names,
            )

        arguments = event if isinstance(event, dict) else {}
        logger.info(
            "tool_invoke target=%s tool=%s args=%s",
            self.target_name,
            name,
            json.dumps(arguments, default=str)[:1024],
        )

        try:
            result = handler(arguments)
        except KeyError as exc:
            return tool_error(
                "MISSING_ARGUMENT", f"required argument {exc.args[0]!r} was not supplied"
            )
        except (ValueError, TypeError) as exc:
            return tool_error("INVALID_ARGUMENT", str(exc))
        except Exception as exc:  # noqa: BLE001 - the model needs to see this
            logger.error(
                "tool_failed target=%s tool=%s\n%s",
                self.target_name,
                name,
                traceback.format_exc(),
            )
            return tool_error("TOOL_ERROR", f"{type(exc).__name__}: {exc}")

        return result
