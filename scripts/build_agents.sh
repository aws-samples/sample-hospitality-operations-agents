#!/usr/bin/env bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
#
# Assemble the AgentCore Runtime deployment directory for the agent graph.
#
# AgentCore Runtime "direct code deploy" takes a zip, unpacks it to the working
# directory, and execs `entryPoint` there -- there is no Dockerfile and no ECR
# step. So this script has to produce a single directory that is simultaneously
# the source root and the site-packages root:
#
#     build/agents/
#       main.py  orchestrator.py  ...      <- our code, at the top level
#       prompts/  subagents/               <- our packages
#       strands/  bedrock_agentcore/  ...  <- dependencies, flat, arm64
#       bin/opentelemetry-instrument       <- the tracing wrapper, executable
#
# Dependencies go FLAT rather than under a subdirectory because the unpacked
# directory is what lands on sys.path: `strands` under `deps/` would only be
# importable as `deps.strands`.
#
# This mirrors what bedrock_agentcore_starter_toolkit's CodeZipPackager does
# (utils/runtime/package.py) on purpose, including its two non-obvious fixups:
#
#   1. `uv pip install --target` writes console scripts whose shebang points at
#      the *host* interpreter that resolved them (e.g. a mise-managed macOS
#      python). That path does not exist on the Runtime, so every bin/ script is
#      rewritten to `#!/usr/bin/env python3`.
#   2. Those scripts must stay executable. `entryPoint: ["opentelemetry-instrument",
#      "main.py"]` is an argv, so argv[0] is exec'd from bin/ -- CDK's asset
#      zipper preserves st_mode, so a lost +x here is a lost +x in production.
#
# Wheels are pulled for linux/arm64 regardless of this machine's architecture:
# the Runtime is always aarch64, and a native macOS wheel for pydantic-core or
# jiter would import-error on the first invocation rather than at build time.
#
# Usage:
#   scripts/build_agents.sh          # incremental: reuses the dependency layer
#   scripts/build_agents.sh --clean  # rebuild dependencies from scratch
#
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SOURCE_DIR="$REPO_ROOT/agents"
BUILD_DIR="$REPO_ROOT/build/agents"
REQUIREMENTS="$SOURCE_DIR/requirements.txt"
STAMP="$REPO_ROOT/build/.deps-stamp"

PYTHON_VERSION="3.12"
# Tried in order. manylinux2014 is the widest-compatibility tag; the newer ones
# are fallbacks for packages that only ship against a newer glibc.
PLATFORMS=("aarch64-manylinux2014" "aarch64-manylinux_2_17" "aarch64-manylinux_2_28")

CLEAN=0
for arg in "$@"; do
  case "$arg" in
    --clean) CLEAN=1 ;;
    *) echo "unknown argument: $arg" >&2; exit 2 ;;
  esac
done

command -v uv >/dev/null 2>&1 || {
  echo "error: uv is required (https://docs.astral.sh/uv/getting-started/installation/)" >&2
  exit 1
}

# ---------------------------------------------------------------------------- #
# 1. Dependency layer
# ---------------------------------------------------------------------------- #
# Resolving and downloading ~40 arm64 wheels takes the better part of a minute,
# and the agent code changes far more often than requirements.txt does. The
# hash of requirements.txt gates the reinstall so an ordinary edit-deploy cycle
# only pays for the copy in step 2.

requirements_hash() {
  shasum -a 256 "$REQUIREMENTS" | cut -d' ' -f1
}

install_dependencies() {
  echo "==> installing dependencies for linux/arm64 (python $PYTHON_VERSION)"
  local platform status
  for platform in "${PLATFORMS[@]}"; do
    set +e
    uv pip install \
      --target "$BUILD_DIR" \
      --python-version "$PYTHON_VERSION" \
      --python-platform "$platform" \
      --only-binary :all: \
      --upgrade \
      --quiet \
      -r "$REQUIREMENTS"
    status=$?
    set -e
    if [ $status -eq 0 ]; then
      echo "    resolved against $platform"
      return 0
    fi
    echo "    $platform failed, trying next" >&2
  done
  echo "error: could not install dependencies for any aarch64 platform tag" >&2
  return 1
}

# Rewrite host-specific shebangs and keep the scripts executable. Matches
# anything that is not already `#!/usr/bin/env ...`.
fix_bin_scripts() {
  [ -d "$BUILD_DIR/bin" ] || return 0
  local script fixed=0
  while IFS= read -r script; do
    if head -c 2 "$script" 2>/dev/null | grep -q '#!' &&
       ! head -n 1 "$script" | grep -q '^#!/usr/bin/env '; then
      # In-place with a temp file: sed -i is not portable between BSD and GNU.
      { printf '#!/usr/bin/env python3\n'; tail -n +2 "$script"; } > "$script.tmp"
      mv "$script.tmp" "$script"
      fixed=$((fixed + 1))
    fi
    chmod +x "$script"
  done < <(find "$BUILD_DIR/bin" -maxdepth 1 -type f)
  echo "    rewrote $fixed shebang(s) in bin/"
}

if [ "$CLEAN" = 1 ] || [ ! -d "$BUILD_DIR" ] || [ ! -f "$STAMP" ] ||
   [ "$(cat "$STAMP")" != "$(requirements_hash)" ]; then
  rm -rf "$BUILD_DIR"
  mkdir -p "$BUILD_DIR"
  install_dependencies
  fix_bin_scripts
  requirements_hash > "$STAMP"
else
  echo "==> dependencies unchanged, reusing $BUILD_DIR"
  # Remove only our own code, so a deleted module or prompt does not linger in
  # the build from a previous run and get deployed.
  while IFS= read -r entry; do
    rm -rf "$BUILD_DIR/$entry"
  done < <(cd "$SOURCE_DIR" && find . -maxdepth 1 -mindepth 1 \
             ! -name '__pycache__' -exec basename {} \;)
fi

# ---------------------------------------------------------------------------- #
# 2. Agent source
# ---------------------------------------------------------------------------- #
echo "==> copying agent source"
# --exclude rather than a copy-then-delete so no .pyc from this machine's
# CPython ever reaches an arm64 runtime that would ignore it anyway.
rsync -a \
  --exclude '__pycache__/' \
  --exclude '*.py[co]' \
  --exclude '.pytest_cache/' \
  "$SOURCE_DIR"/ "$BUILD_DIR"/

# ---------------------------------------------------------------------------- #
# 3. Verify what we produced
# ---------------------------------------------------------------------------- #
# The Runtime's failure mode for a missing file is a container that starts,
# fails /ping, and reports CREATE_FAILED minutes later with nothing useful in
# the log. Cheaper to assert here. agentcore_stack.py asserts the same set at
# synth time, because a stale build directory is just as deployable as a fresh
# one.
echo "==> verifying build"
missing=()
for required in \
  main.py orchestrator.py config.py run_context.py gateway.py sigv4.py \
  memory.py model.py prompt_loader.py code_execution.py \
  subagents/__init__.py subagents/base.py subagents/arrivals.py \
  subagents/housekeeping.py subagents/billing.py subagents/night_audit.py \
  subagents/regional.py \
  prompts/orchestrator.md prompts/arrivals.md prompts/housekeeping.md \
  prompts/billing.md prompts/night_audit.md prompts/regional.md \
  strands/__init__.py bedrock_agentcore/__init__.py boto3/__init__.py \
  httpx/__init__.py \
  bin/opentelemetry-instrument
do
  [ -e "$BUILD_DIR/$required" ] || missing+=("$required")
done

if [ ${#missing[@]} -gt 0 ]; then
  echo "error: build is incomplete, missing:" >&2
  printf '  %s\n' "${missing[@]}" >&2
  exit 1
fi

[ -x "$BUILD_DIR/bin/opentelemetry-instrument" ] || {
  echo "error: bin/opentelemetry-instrument is not executable. The Runtime" >&2
  echo "       entrypoint execs it by name, so this would fail at startup." >&2
  exit 1
}

size_mb=$(du -sm "$BUILD_DIR" | cut -f1)
echo "    $BUILD_DIR is ${size_mb} MB unpacked"
# The hard service limit is 250 MB on the *zipped* artifact; unpacked is
# strictly larger, so this only fires on a genuine problem.
if [ "$size_mb" -gt 400 ]; then
  echo "warning: unpacked build is large; check the zipped asset stays under 250 MB" >&2
fi

echo "==> ok"
