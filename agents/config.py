# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Environment resolution, in one place, failing loudly.

Every value the agent graph needs from the deploying stack arrives as a runtime
environment variable. Reading them here rather than scattered through the
modules means a misconfigured stack fails on import with a message naming the
missing variable, instead of at the first tool call with a stack trace from
somewhere inside an MCP transport.
"""

from __future__ import annotations

import os

#: Inference profile, not a bare model id. ``us.`` keeps inference in-region,
#: matching the single-region ``us-east-1`` foundation.
DEFAULT_MODEL_ID = "us.anthropic.claude-sonnet-5"

#: The five Gateway targets. The tool names the Gateway advertises are prefixed
#: ``{target}___`` -- three underscores, verified against the deployed Gateway --
#: which is how a sub-agent's tool list is filtered down to exactly its own domain.
TARGETS: tuple[str, ...] = (
    "arrivals",
    "housekeeping",
    "billing",
    "nightaudit",
    "regional",
)


class ConfigError(RuntimeError):
    """A required environment variable is missing or empty."""


def required(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise ConfigError(
            f"{name} is not set. The AgentCore Runtime's environment_variables "
            f"are populated by hotel-ops-agent-agentcore; a missing value means "
            f"the stack and this code disagree about the contract."
        )
    return value


def optional(name: str, default: str = "") -> str:
    return os.environ.get(name, "").strip() or default


#: The MCP endpoint of the single Gateway that fronts all five tool targets.
def gateway_url() -> str:
    return required("GATEWAY_URL")


#: AgentCore Memory id. Empty is legitimate: memory is additive, and an agent
#: that cannot reach it should still answer rather than refuse.
def memory_id() -> str:
    return optional("MEMORY_ID")


def memory_namespaces() -> dict[str, str]:
    """Namespace templates, authored by the stack that created the strategies.

    Passing these in rather than restating them here keeps one source of truth:
    the strategy's namespace and the namespace we retrieve from cannot drift.
    ``{actorId}`` and ``{sessionId}`` are substituted by the session manager.
    """
    return {
        "facts": optional("MEMORY_SEMANTIC_NAMESPACE"),
        "shift": optional("MEMORY_SUMMARY_NAMESPACE"),
    }


#: Identifier of the custom AgentCore Code Interpreter, which the stack creates
#: with ``SANDBOX`` network mode. Empty falls back to the service default
#: ``aws.codeinterpreter.v1``, which has network access -- acceptable for local
#: development, and the reason the stack always sets this in deployment.
def code_interpreter_id() -> str:
    return optional("CODE_INTERPRETER_ID")


def model_id() -> str:
    return optional("MODEL_ID", DEFAULT_MODEL_ID)


def region() -> str:
    return optional("AWS_REGION", optional("AWS_DEFAULT_REGION", "us-east-1"))


def log_level() -> str:
    return optional("LOG_LEVEL", "INFO").upper()


def default_property_id() -> str:
    """A single-property deployment can pin the scope; chain-wide leaves it unset.

    When unset, the invocation payload must carry ``propertyId`` for any request
    that reaches A1, A2, A3, or A4 -- the foundation's housekeeping endpoints
    resolve a per-property Cognito identity from it, so there is nothing sensible
    to guess.
    """
    return optional("PROPERTY_SCOPE")
