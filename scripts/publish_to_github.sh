#!/usr/bin/env bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
#
# Copy this repository's publishable files into a clone of the public aws-samples
# repository, scan them, and leave the commit to you.
#
# Why a script: the file list and the checks were a shell one-liner retyped by hand,
# which is exactly the kind of step that eventually publishes something it should not.
# The exclusions below are the whole reason it exists -- each one is a file that must
# stay internal, and the list is reviewable here rather than in someone's history.
#
# What it deliberately does NOT do: commit, push, or make anything public. It prints
# what changed and stops.
#
#   scripts/publish_to_github.sh ~/scratch/sample-hospitality-operations-agents
set -euo pipefail

DEST="${1:?usage: publish_to_github.sh <path-to-public-repo-clone>}"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

# Internal-only files. Anything added here needs a reason in this comment block.
#   use-case-broad.md  - brainstorming notes; superseded for readers by
#                        PATTERN_EXTENSION_GUIDE.md, and never published.
#   .gitleaksignore    - fingerprints naming commits that exist only in this
#                        repository's history; meaningless and misleading in a repo
#                        whose history starts at the public initial commit.
#   CLAUDE.md          - this team's working notes for Claude Code sessions. Kept in
#                        the internal repository only, by the owner's decision; the
#                        public copy also lists it in .gitignore (below) so a
#                        contributor's own CLAUDE.md is never committed by accident.
EXCLUDE='^(use-case-broad\.md|\.gitleaksignore|CLAUDE\.md)$'

[[ -d "$DEST/.git" ]] || { echo "not a git clone: $DEST" >&2; exit 1; }

echo "==> copying"
git ls-files | grep -vxE "$EXCLUDE" > /tmp/publish-files.txt
rsync -a --files-from=/tmp/publish-files.txt ./ "$DEST"/
echo "    $(wc -l < /tmp/publish-files.txt) files"

# The public repository ignores CLAUDE.md; the internal one tracks it. So the public
# .gitignore is this one plus that line, and an excluded file that is already tracked
# in the public repo is removed from it rather than left stale.
printf '\n# Claude Code working notes are kept out of the public repository.\nCLAUDE.md\n' \
    >> "$DEST/.gitignore"
for excluded in use-case-broad.md .gitleaksignore CLAUDE.md; do
    git -C "$DEST" rm -q --cached --ignore-unmatch "$excluded"
    rm -f "$DEST/$excluded"
done

cd "$DEST"
# Hooks are per-clone, so a fresh clone has none until this runs.
git secrets --install -f >/dev/null 2>&1 || true
git secrets --register-aws >/dev/null 2>&1 || true

echo "==> scanning"
git secrets --scan -r .
# Not piped to tail: a pipeline reports the *last* command's status, so `| tail` would
# turn a scanner failure into a pass. Same reason the tests below use a log file.
gitleaks detect --no-banner --redact --no-color

echo "==> tests"
# The public clone has no virtualenv, so build a throwaway one rather than trusting
# whatever `python3` happens to be -- the ambient interpreter has no pytest, and a
# missing-module error must not read as "tests passed".
if command -v uv >/dev/null; then
    uv venv -q -p 3.12 /tmp/publish-check-venv
    uv pip install -q --python /tmp/publish-check-venv/bin/python -r requirements-dev.txt
    # Judged on pytest's exit status, never on its output. `-o addopts=` drops
    # pyproject's own -q: stacked with this one it goes silent and prints no summary,
    # which an earlier version of this script read as failure after the tests passed.
    if /tmp/publish-check-venv/bin/python -m pytest -q -o addopts= \
            > /tmp/publish-tests.log 2>&1; then
        tail -1 /tmp/publish-tests.log
    else
        tail -20 /tmp/publish-tests.log >&2
        echo "    TESTS FAILED -- nothing staged is fit to push." >&2
        exit 1
    fi
else
    echo "    SKIPPED: uv not installed, so the suite did not run here." >&2
    echo "    Run it in the source repository before pushing." >&2
fi

echo "==> changed"
git add -A
git status --short
cat <<'NEXT'

Nothing has been committed. Review the diff, then:
    git commit && git push origin main && gh run watch
NEXT
