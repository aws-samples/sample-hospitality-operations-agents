# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""A single Python-execution tool backed by AgentCore Code Interpreter, for A5.

``strands-agents-tools`` ships a code-interpreter tool that does this and more.
It is not used here: its base install pulls aiohttp, pillow, slack-bolt, sympy,
and dill, none of which any agent in this system touches, and all of which have
to be vendored as arm64 wheels into a Runtime deployment package. Roughly a
hundred megabytes for one tool. ``bedrock_agentcore`` is already a dependency and
exposes the same data plane, so the tool is written directly against it.

Writing it here also means the tool's *description* is ours. That description is
the only thing the model reads before deciding how to use the sandbox, and the
two facts it most needs -- there is no network, and output that is not printed is
lost -- are worth stating in it rather than hoping the system prompt carries.

The sandbox session is keyed on the run's session id (property plus operating
date) and cached at module level, so a warm container reconnects instead of
paying ~800ms to create one, and a second delegation in the same run still has
the dataframes the first one loaded.
"""

from __future__ import annotations

import logging
import threading

from bedrock_agentcore.tools.code_interpreter_client import CodeInterpreter
from strands import tool

from config import code_interpreter_id, region
from run_context import current

logger = logging.getLogger(__name__)

#: Long enough for a multi-step analysis over a 92-day window, short enough that
#: an abandoned sandbox is reaped rather than billed for an hour.
SESSION_TIMEOUT_SECONDS = 1800

#: Sandboxes cached by run session id. Guarded by a lock because Strands runs
#: sync tools in a worker thread and a warm container can have more than one
#: invocation in flight.
_sessions: dict[str, CodeInterpreter] = {}
_lock = threading.Lock()


@tool
def run_analysis(code: str) -> str:
    """Execute Python in a sandbox to compute figures you must not estimate.

    Use this for variance, standard deviation, trendlines, period-over-period
    deltas, RevPAR, ADR, ranking, percentiles, and outlier detection -- anything
    beyond comparing two numbers. Reading a 92-day series and asserting a trend
    produces a confidently wrong number that nobody downstream can distinguish
    from a right one.

    Three things about this sandbox:

    - **No network.** It cannot call the hotel API or install packages. Pass data
      in by embedding the JSON you already fetched directly in the code.
    - **Print your results.** Only stdout comes back. A computed value you did
      not print is a value you do not get.
    - **State persists** across calls within a run, so load data once and then
      run several analyses against it.

    The standard library and pandas are available. If an import fails, rewrite
    using the standard library -- you cannot install anything.

    Args:
        code: Python source to run. Print everything you want returned.

    Returns:
        Whatever the code printed, plus any error output.
    """
    session_name = f"a5-{current().session_id}".replace(":", "-")
    try:
        response = _session(session_name).invoke(
            "executeCode",
            {"code": code, "language": "python", "clearContext": False},
        )
    except Exception as exc:  # noqa: BLE001
        # Most likely the cached session expired past its timeout. Drop it so the
        # next call builds a fresh one, and tell the model plainly -- it can
        # retry, and a retry will start a new sandbox.
        with _lock:
            _sessions.pop(session_name, None)
        logger.warning("Code Interpreter invoke failed for %s", session_name, exc_info=True)
        return (
            f"Code execution failed: {type(exc).__name__} - {exc}. The sandbox was "
            f"discarded; a retry will start a fresh one, but any state your "
            f"earlier calls set up is gone, so re-load the data."
        )

    return _text_of(response)


def _session(session_name: str) -> CodeInterpreter:
    with _lock:
        client = _sessions.get(session_name)
        if client is not None:
            return client

        client = CodeInterpreter(region=region())
        client.start(
            # Falls back to the service default aws.codeinterpreter.v1 when the
            # stack has not published one. That default has network access, which
            # is fine for local development and is exactly why the stack always
            # sets CODE_INTERPRETER_ID to the custom SANDBOX interpreter.
            identifier=code_interpreter_id() or None,
            name=session_name,
            session_timeout_seconds=SESSION_TIMEOUT_SECONDS,
        )
        logger.info(
            "code interpreter session=%s id=%s interpreter=%s",
            session_name,
            client.session_id,
            client.identifier,
        )
        _sessions[session_name] = client
        return client


def _text_of(response) -> str:
    """Flatten the data plane's event stream into the text the model should see.

    ``invoke_code_interpreter`` returns a botocore event stream. Each event may
    carry a ``result`` whose ``content`` is a list of blocks; stdout, stderr and
    tracebacks all arrive as text blocks. Errors are returned as text rather than
    raised: a traceback is something the model can read and fix, and turning it
    into an exception would only cost a turn.
    """
    stream = response.get("stream") if isinstance(response, dict) else None
    if stream is None:
        return str(response)

    chunks: list[str] = []
    is_error = False

    for event in stream:
        result = event.get("result") if isinstance(event, dict) else None
        if not result:
            continue
        is_error = is_error or bool(result.get("isError"))
        for block in result.get("content") or []:
            if not isinstance(block, dict):
                continue
            if (text := block.get("text")) is not None:
                chunks.append(str(text))
            elif (data := block.get("json")) is not None:
                chunks.append(str(data))

    output = "\n".join(chunk for chunk in chunks if chunk.strip())
    if not output:
        return (
            "The code ran and produced no output. Nothing is returned unless you "
            "print it -- add print() around the values you need."
        )
    return f"Execution error:\n{output}" if is_error else output
