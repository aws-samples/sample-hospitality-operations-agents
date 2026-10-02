# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Read-only resolution of the foundation platform's deployed resources.

The hospitality foundation (CloudFormation stack ``anycompany-booking``) is a hard
prerequisite for this project. This module reads its outputs at synth time and
does nothing else -- it never writes, never adds an Export to the foundation
stack, and never reaches the foundation's database.

Resolution order:

1. ``HOTEL_OPS_FOUNDATION_JSON`` -- path to a JSON file with the same field
   names as :class:`FoundationConfig`. Used by unit tests and CI, where no AWS
   credentials are available.
2. ``cloudformation:DescribeStacks`` against the live foundation stack.

If neither succeeds, synth fails loudly. A silent default would produce a stack
that deploys cleanly and then 403s on every call at runtime, which is far worse
than a failed synth.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from typing import Any, Mapping

DEFAULT_FOUNDATION_STACK = "anycompany-booking"
DEFAULT_REGION = "us-east-1"

#: Root-stack outputs this project cannot operate without.
REQUIRED_OUTPUTS: Mapping[str, str] = {
    "ApiUrl": "crs_api_url",
    "PmsApiUrl": "pms_api_url",
    "UserPoolId": "user_pool_id",
    "AdminAuthClientId": "admin_auth_client_id",
    "UserPoolClientId": "user_pool_client_id",
}

#: Useful for verification and cross-linking, but not load-bearing.
OPTIONAL_OUTPUTS: Mapping[str, str] = {
    "PmsCloudFrontUrl": "pms_console_url",
    "CloudFrontUrl": "booking_site_url",
}


class FoundationNotDeployedError(RuntimeError):
    """The foundation stack is absent, incomplete, or unreadable."""


@dataclass(frozen=True)
class FoundationConfig:
    """Everything this project consumes from the foundation. All read-only."""

    # --- required ---
    crs_api_url: str
    pms_api_url: str
    user_pool_id: str
    admin_auth_client_id: str
    user_pool_client_id: str

    # --- derived ---
    region: str = DEFAULT_REGION
    account: str = ""
    environment: str = "dev"

    # --- optional, informational ---
    pms_console_url: str = ""
    booking_site_url: str = ""

    # --- extra outputs, kept verbatim for debugging ---
    raw_outputs: Mapping[str, str] = field(default_factory=dict, repr=False)

    # ------------------------------------------------------------------ #
    # Derived properties
    # ------------------------------------------------------------------ #

    @property
    def event_bus_name(self) -> str:
        """Name of the foundation's custom EventBridge bus.

        The bus is created by the nested ``EventsStack`` as
        ``anycompany-events-${Environment}`` and is *not* a root-stack output.
        Its CloudFormation Export name embeds the nested stack's random suffix,
        so importing it would be brittle; the name is deterministic, so we
        derive it and verify it exists via :meth:`verify_event_bus`.
        """
        return f"anycompany-events-{self.environment}"

    @property
    def event_bus_arn(self) -> str:
        return (
            f"arn:aws:events:{self.region}:{self.account}"
            f":event-bus/{self.event_bus_name}"
        )

    @property
    def user_pool_arn(self) -> str:
        return (
            f"arn:aws:cognito-idp:{self.region}:{self.account}"
            f":userpool/{self.user_pool_id}"
        )

    def api_url(self, surface: str) -> str:
        """Base URL for ``"crs"`` or ``"pms"``."""
        try:
            return {"crs": self.crs_api_url, "pms": self.pms_api_url}[surface]
        except KeyError:
            raise ValueError(f"unknown API surface {surface!r}; use 'crs' or 'pms'")

    # ------------------------------------------------------------------ #
    # Loading
    # ------------------------------------------------------------------ #

    @classmethod
    def resolve(
        cls,
        *,
        stack_name: str = DEFAULT_FOUNDATION_STACK,
        region: str = DEFAULT_REGION,
        environment: str = "dev",
        profile: str | None = None,
    ) -> "FoundationConfig":
        """Load from ``HOTEL_OPS_FOUNDATION_JSON`` if set, else from CloudFormation."""
        override = os.environ.get("HOTEL_OPS_FOUNDATION_JSON")
        if override:
            return cls.from_json_file(override)
        return cls.from_stack(
            stack_name=stack_name,
            region=region,
            environment=environment,
            profile=profile,
        )

    @classmethod
    def from_json_file(cls, path: str) -> "FoundationConfig":
        try:
            with open(path, encoding="utf-8") as handle:
                data: dict[str, Any] = json.load(handle)
        except OSError as exc:
            raise FoundationNotDeployedError(
                f"HOTEL_OPS_FOUNDATION_JSON points at {path!r}, which cannot be read: {exc}"
            ) from exc

        known = {f for f in cls.__dataclass_fields__}
        missing = [f for f in REQUIRED_OUTPUTS.values() if not data.get(f)]
        if missing:
            raise FoundationNotDeployedError(
                f"{path!r} is missing required field(s): {', '.join(missing)}"
            )
        return cls(**{k: v for k, v in data.items() if k in known})

    @classmethod
    def from_stack(
        cls,
        *,
        stack_name: str = DEFAULT_FOUNDATION_STACK,
        region: str = DEFAULT_REGION,
        environment: str = "dev",
        profile: str | None = None,
    ) -> "FoundationConfig":
        """Read the live foundation stack's outputs. Read-only; no mutation."""
        import boto3
        from botocore.exceptions import BotoCoreError, ClientError

        session = boto3.Session(profile_name=profile, region_name=region)
        try:
            response = session.client("cloudformation").describe_stacks(
                StackName=stack_name
            )
        except (ClientError, BotoCoreError) as exc:
            raise FoundationNotDeployedError(
                f"Cannot read the foundation stack {stack_name!r} in {region}.\n"
                f"  {exc}\n"
                "The hospitality foundation is a prerequisite for this project. "
                "Deploy it first, or set HOTEL_OPS_FOUNDATION_JSON to a config file."
            ) from exc

        stack = response["Stacks"][0]
        outputs = {
            o["OutputKey"]: o["OutputValue"] for o in stack.get("Outputs", [])
        }

        missing = [key for key in REQUIRED_OUTPUTS if not outputs.get(key)]
        if missing:
            raise FoundationNotDeployedError(
                f"Foundation stack {stack_name!r} is missing required output(s): "
                f"{', '.join(missing)}.\n"
                f"Present outputs: {', '.join(sorted(outputs)) or '<none>'}\n"
                "This usually means the foundation is only partially deployed."
            )

        account = stack["StackId"].split(":")[4]
        kwargs: dict[str, Any] = {
            field_name: outputs[key] for key, field_name in REQUIRED_OUTPUTS.items()
        }
        kwargs.update(
            {
                field_name: outputs.get(key, "")
                for key, field_name in OPTIONAL_OUTPUTS.items()
            }
        )
        return cls(
            region=region,
            account=account,
            environment=environment,
            raw_outputs=outputs,
            **kwargs,
        )

    # ------------------------------------------------------------------ #
    # Verification helpers (read-only, opt-in)
    # ------------------------------------------------------------------ #

    def verify_event_bus(self, *, profile: str | None = None) -> None:
        """Confirm the derived event bus actually exists. Raises if not."""
        import boto3
        from botocore.exceptions import BotoCoreError, ClientError

        session = boto3.Session(profile_name=profile, region_name=self.region)
        try:
            session.client("events").describe_event_bus(Name=self.event_bus_name)
        except (ClientError, BotoCoreError) as exc:
            raise FoundationNotDeployedError(
                f"Derived event bus {self.event_bus_name!r} does not exist "
                f"in {self.region}: {exc}\n"
                "Check the 'environment' context value in cdk.json -- the bus is "
                "named anycompany-events-${Environment}."
            ) from exc

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data.pop("raw_outputs", None)
        return data


#: Attributes that carry a user's *authorization scope*. The platform's own
#: authorizer reads both -- ``custom:property_id`` decides which hotel's records a
#: property-scoped user may touch, ``custom:region`` which region a RegionalManager
#: sees -- and so does this project's console and Gateway.
SCOPE_ATTRIBUTES = ("custom:property_id", "custom:region")


def writable_scope_attributes(
    config: FoundationConfig, *, region: str, cognito=None
) -> list[str]:
    """Scope attributes a signed-in user can rewrite through the console's app client.

    A security review found that the platform's SPA client -- the one this console
    signs staff in with -- lists both in ``WriteAttributes``. That lets any user,
    including a guest from the booking site, call ``UpdateUserAttributes`` with their
    own access token and choose which property they are scoped to. The scope every
    authorization check here relies on is then whatever the caller says it is.

    Read-only: a ``DescribeUserPoolClient`` and nothing else. It cannot fix the
    platform -- this project never modifies it -- so it reports, and the caller
    decides whether to refuse the synth.
    """
    if cognito is None:
        import boto3

        cognito = boto3.client("cognito-idp", region_name=region)
    client = cognito.describe_user_pool_client(
        UserPoolId=config.user_pool_id, ClientId=config.user_pool_client_id
    )["UserPoolClient"]
    writable = set(client.get("WriteAttributes") or [])
    return [a for a in SCOPE_ATTRIBUTES if a in writable]
