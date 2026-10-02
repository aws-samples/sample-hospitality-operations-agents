# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The one model configuration every agent in the graph shares.

Claude Sonnet 5 via the ``us.`` inference profile. Adaptive thinking is on: the
work here is genuinely multi-step reasoning over partially-conflicting operational
data (which of two rooms fits this guest better, whether two reporting endpoints
disagreeing is a bug or the known ADR denominator difference), and that is what
adaptive thinking is for.

Note ``thinking: {"type": "adaptive"}`` and *no* ``budget_tokens``. Sonnet 5
rejects ``budget_tokens`` with a 400 -- it is the pre-4.6 shape.
"""

from __future__ import annotations

from strands.models import BedrockModel
from strands.models.bedrock import CacheConfig

from config import model_id, region

#: Room-assignment and audit reasoning is long-form; the default ceiling is too
#: low for A4's readiness reports, which enumerate every unresolved folio.
MAX_TOKENS = 8192


def build_model(*, max_tokens: int = MAX_TOKENS) -> BedrockModel:
    return BedrockModel(
        region_name=region(),
        model_id=model_id(),
        streaming=True,
        max_tokens=max_tokens,
        additional_request_fields={"thinking": {"type": "adaptive"}},
        # Deliberately no `temperature`. Bedrock rejects any temperature other
        # than 1 when thinking is enabled, so setting the low value that
        # operational determinism would otherwise argue for is a 400, not a
        # tradeoff. Determinism comes from the prompts and the tool layer here.
        #
        # The system prompts are long and stable, and one orchestrator turn can
        # fan out to several sub-agents that each re-send theirs plus their tool
        # definitions. Caching both sections is the largest cost lever available.
        cache_config=CacheConfig(strategy="auto", tools_ttl=True),
    )
