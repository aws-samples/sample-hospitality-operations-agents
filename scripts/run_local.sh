#!/usr/bin/env bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
# Run the agent graph on this machine against the *deployed* Gateway, Memory and
# Code Interpreter. This is Layer 2 of the verification plan.
#
# Why not `agentcore dev`
# ----------------------
# The starter toolkit's dev server reads its configuration from a .bedrock_agentcore
# yaml that it also wants to own, and it builds its own container. Every value the
# runtime needs is already a CloudFormation output of hotel-ops-agent-agentcore, so
# reading them here and running the entrypoint directly tests the same code with
# fewer moving parts -- and it tests the *deployed* Gateway, which is the hop most
# worth exercising before the runtime is in the loop.
#
# What this does NOT test: the arm64 dependency bundle, the entrypoint's shebang,
# or the Runtime's own auth. Those only exist once deployed -- that is Layer 3.
#
# Usage:
#   scripts/run_local.sh                 # serve on :8080 until interrupted
#   scripts/run_local.sh --env           # print the resolved environment and exit
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
STACK="${STACK:-hotel-ops-agent-agentcore}"
# No default profile: a baked-in name is one from whichever machine this was written on.
PROFILE="${AWS_PROFILE:?set AWS_PROFILE to a profile for the account the platform is deployed in}"
# Deliberately not `${AWS_REGION:-...}`. The foundation, the Gateway, Memory and
# the Runtime are all in us-east-1, and a developer profile's ambient region often
# is not -- honouring it would look up a stack that does not exist there and fail
# with a ValidationError that says nothing about the real cause. Override explicitly.
REGION="${HOTEL_OPS_REGION:-us-east-1}"
VENV="$REPO_ROOT/.venv"

if [[ ! -x "$VENV/bin/python" ]]; then
  echo "error: $VENV does not exist. Create it with:" >&2
  echo "  uv venv --python 3.12 .venv && VIRTUAL_ENV=.venv uv pip install -r agents/requirements.txt" >&2
  exit 1
fi

# One describe-stacks, then pick the outputs out of it -- five separate --query
# calls would be five API round trips for one answer.
outputs_json="$(aws cloudformation describe-stacks \
  --stack-name "$STACK" --profile "$PROFILE" --region "$REGION" \
  --query 'Stacks[0].Outputs' --output json)"

get() {
  python3 -c '
import json, sys
outputs = json.load(sys.stdin)
key = sys.argv[1]
for output in outputs or []:
    if output["OutputKey"] == key:
        print(output["OutputValue"]); break
else:
    sys.exit(f"error: {key} is not an output of the deployed stack")
' "$1" <<<"$outputs_json"
}

export GATEWAY_URL="$(get GatewayUrl)"
export MEMORY_ID="$(get MemoryId)"
export CODE_INTERPRETER_ID="$(get CodeInterpreterId)"
# Namespace templates are constants in agentcore_stack.py rather than outputs:
# they are part of the code contract, not of the deployment. Kept in sync by
# tests/unit/test_memory.py.
export MEMORY_SEMANTIC_NAMESPACE='/hotel-ops/facts/{actorId}'
export MEMORY_SUMMARY_NAMESPACE='/hotel-ops/shift/{actorId}/{sessionId}'
export MODEL_ID="${MODEL_ID:-us.anthropic.claude-sonnet-5}"
export LOG_LEVEL="${LOG_LEVEL:-INFO}"
export AWS_PROFILE="$PROFILE"
export AWS_REGION="$REGION"
export AWS_DEFAULT_REGION="$REGION"

if [[ "${1:-}" == "--env" ]]; then
  for name in GATEWAY_URL MEMORY_ID CODE_INTERPRETER_ID \
              MEMORY_SEMANTIC_NAMESPACE MEMORY_SUMMARY_NAMESPACE \
              MODEL_ID LOG_LEVEL AWS_PROFILE AWS_REGION; do
    printf '%s=%s\n' "$name" "${!name}"
  done
  exit 0
fi

echo "==> serving the agent graph on http://localhost:8080 (Ctrl-C to stop)"
echo "    gateway: $GATEWAY_URL"
cd "$REPO_ROOT/agents"
exec "$VENV/bin/python" main.py
