#!/usr/bin/env bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
#
# Build the ops console for deployment.
#
# The two Cognito ids the app needs are read from the *deployed* api stack rather
# than hardcoded or committed. That matters more than it looks: the console signs in
# against the foundation's existing user pool and its existing SPA client, and if
# either id drifted from what the API's authorizer validates, sign-in would succeed
# and every request would then 401 -- a failure that looks like a permissions bug and
# is really a build-time typo.
#
# No API URL is injected. The app calls /api/* relative, because CloudFront serves it
# and the API from one origin. So there is nothing here that has to change when the
# API is redeployed.
#
# Usage:  scripts/build_frontend.sh
#   AWS_PROFILE must be set; HOTEL_OPS_REGION defaults to us-east-1.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
FRONTEND="$REPO_ROOT/frontend"
REGION="${HOTEL_OPS_REGION:-us-east-1}"
API_STACK="hotel-ops-agent-api"

cd "$FRONTEND"

echo "==> reading Cognito configuration from $API_STACK"
outputs=$(aws cloudformation describe-stacks \
  --stack-name "$API_STACK" \
  --region "$REGION" \
  --query 'Stacks[0].Outputs' \
  --output json)

read_output() {
  echo "$outputs" | python3 -c "
import json, sys
key = sys.argv[1]
for o in json.load(sys.stdin):
    if o['OutputKey'] == key:
        print(o['OutputValue']); break
else:
    sys.exit(f'{key} is not an output of $API_STACK. Deploy it first.')
" "$1"
}

USER_POOL_ID=$(read_output ConsoleUserPoolId)
CLIENT_ID=$(read_output ConsoleUserPoolClientId)

echo "    user pool: $USER_POOL_ID"
echo "    client:    $CLIENT_ID"

# .local so it is gitignored: these are not secrets -- a Cognito pool id and a public
# SPA client id are both safe in a bundle -- but they are environment-specific, and a
# committed copy is a copy that goes stale.
cat > .env.production.local <<ENV
VITE_USER_POOL_ID=$USER_POOL_ID
VITE_USER_POOL_CLIENT_ID=$CLIENT_ID
ENV

if [[ ! -d node_modules ]]; then
  echo "==> installing dependencies"
  if [[ -f package-lock.json ]]; then npm ci; else npm install; fi
fi

echo "==> type-checking and building"
npm run build

if [[ ! -f dist/index.html ]]; then
  echo "!! dist/index.html was not produced; the CDK synth will refuse to deploy" >&2
  exit 1
fi

echo "==> ok: $(du -sh dist | cut -f1) in $FRONTEND/dist"
echo "    now: cdk deploy hotel-ops-agent-frontend"
