#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Register which property (or region, or the whole chain) each console operator may act on.

Why this exists
---------------
The ops console used to take an operator's scope from their Cognito ID token --
``custom:property_id`` and ``custom:region``. A security review found that the
platform's SPA app client lets any signed-in user rewrite both with their own access
token, so a hotel guest or a front-desk clerk could choose which property they were
scoped to. The platform has no staff table to check those claims against, and this
project does not modify the platform. So scope now lives here, in
``hotel-ops-agent-staff-scope``, which this script writes with **AWS IAM
credentials**. A Cognito user has no path to it.

The console refuses an operator who is not registered (``NOT_REGISTERED``) and one
whose token claims a different scope from their registration (``SCOPE_MISMATCH``).

Usage
-----
    export AWS_PROFILE=<profile for the platform's account>

    # First time: propose a registration for every staff account, from the pool as
    # it is now. Prints what it would write; --apply writes it.
    scripts/register_staff_scope.py seed
    scripts/register_staff_scope.py seed --apply
    scripts/register_staff_scope.py seed --apply --exclude someone@example.com

    # One operator.
    scripts/register_staff_scope.py set frontdesk@anycompany.test --property <uuid>
    scripts/register_staff_scope.py set rm.west@anycompany.test --region WEST
    scripts/register_staff_scope.py set gm@anycompany.test --chain
    scripts/register_staff_scope.py remove someone@anycompany.test

    scripts/register_staff_scope.py list
    scripts/register_staff_scope.py check frontdesk@anycompany.test

**Review the seed before applying it.** Seeding copies each account's attributes as
they stand today. Anyone who has already rewritten their own ``custom:property_id``
would be registered with the value they chose, so compare the proposal against who
actually works where. After seeding, a later rewrite is caught -- that is the point.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import boto3

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "infra"))

from stacks.foundation_config import FoundationConfig  # noqa: E402

TABLE_NAME = "hotel-ops-agent-staff-scope"
REGION = os.environ.get("HOTEL_OPS_REGION", "us-east-1")

#: Kept in step with STAFF_GROUPS in the console layer. A user in none of these is
#: not staff, and is never registered.
STAFF_GROUPS = (
    "Admin", "Manager", "RevenueManager", "RegionalManager", "FrontDesk", "Housekeeping",
)
CHAIN_LEVEL = {"Admin", "Manager", "RevenueManager"}
UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I)


def _clients():
    session = boto3.Session(region_name=REGION)
    stack = os.environ.get("HOTEL_OPS_FOUNDATION_STACK", "anycompany-booking")
    foundation = FoundationConfig.resolve(stack_name=stack, region=REGION)
    return (
        session.client("cognito-idp"),
        session.resource("dynamodb").Table(TABLE_NAME),
        foundation.user_pool_id,
        session.client("sts").get_caller_identity()["Arn"],
    )


def _attributes(user: dict) -> dict:
    return {a["Name"]: a["Value"] for a in user.get("Attributes") or user.get("UserAttributes") or []}


def _staff(cognito, pool_id: str) -> dict[str, dict]:
    """Every user in a staff group: sub -> email, groups, and their current claims."""
    people: dict[str, dict] = {}
    for group in STAFF_GROUPS:
        for page in cognito.get_paginator("list_users_in_group").paginate(
            UserPoolId=pool_id, GroupName=group
        ):
            for user in page["Users"]:
                attrs = _attributes(user)
                person = people.setdefault(
                    attrs["sub"],
                    {
                        "email": attrs.get("email") or user["Username"],
                        "groups": set(),
                        "propertyId": attrs.get("custom:property_id") or None,
                        "region": attrs.get("custom:region") or None,
                    },
                )
                person["groups"].add(group)
    return people


def _find(cognito, pool_id: str, email: str) -> dict:
    users = cognito.list_users(UserPoolId=pool_id, Filter=f'email = "{email}"')["Users"]
    if len(users) != 1:
        raise SystemExit(f"expected one user with email {email!r}, found {len(users)}")
    return _attributes(users[0])


def _label(propertyId: str | None, region: str | None) -> str:
    if propertyId:
        return f"property {propertyId}"
    if region:
        return f"region {region}"
    return "chain"


def _item(sub: str, email: str, propertyId, region, by: str, source: str) -> dict:
    item = {
        "sub": sub,
        "email": email,
        "registeredBy": by,
        "registeredAt": datetime.now(timezone.utc).isoformat(),
        "source": source,
    }
    if propertyId:
        item["propertyId"] = propertyId
    if region:
        item["region"] = region
    return item


# --------------------------------------------------------------------------- #
# Commands
# --------------------------------------------------------------------------- #


def seed(args) -> int:
    cognito, table, pool_id, by = _clients()
    people = _staff(cognito, pool_id)
    existing: set[str] = set()
    try:
        kwargs: dict = {"ProjectionExpression": "#s", "ExpressionAttributeNames": {"#s": "sub"}}
        while True:
            page = table.scan(**kwargs)
            existing |= {i["sub"] for i in page["Items"]}
            if "LastEvaluatedKey" not in page:
                break
            kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]
    except table.meta.client.exceptions.ResourceNotFoundException:
        # Before the api stack that creates the table is deployed. Fine for a dry
        # run, which only reads the pool; --apply needs the table.
        if args.apply:
            raise SystemExit(f"{TABLE_NAME} does not exist yet; deploy hotel-ops-agent-api first")

    proposals, warnings = [], []
    excluded = {e.lower() for e in args.exclude}
    for sub, person in sorted(people.items(), key=lambda kv: kv[1]["email"]):
        if sub in existing and not args.overwrite:
            continue
        if person["email"].lower() in excluded:
            warnings.append(f"  SKIP {person['email']}: excluded with --exclude")
            continue
        groups = person["groups"]
        # The same rules the platform's verify_property_access applies, so a
        # registration never grants what the platform itself would refuse.
        if person["propertyId"] is None and not (groups & (CHAIN_LEVEL | {"RegionalManager"})):
            warnings.append(f"  SKIP {person['email']}: {sorted(groups)} with no property "
                            "has no access on the platform either")
            continue
        proposals.append((sub, person))

    print(f"{len(people)} staff accounts; {len(existing)} already registered; "
          f"{len(proposals)} to register\n")
    for sub, p in proposals:
        print(f"  {p['email']:58} {','.join(sorted(p['groups'])):28} "
              f"{_label(p['propertyId'], p['region'])}")
    for w in warnings:
        print(w)

    if not args.apply:
        print("\nDry run. Review the list -- each scope is copied from the account's "
              "current attributes -- then re-run with --apply.")
        return 0
    with table.batch_writer() as batch:
        for sub, p in proposals:
            batch.put_item(Item=_item(sub, p["email"], p["propertyId"], p["region"], by, "seed"))
    print(f"\nRegistered {len(proposals)}.")
    return 0


def set_(args) -> int:
    cognito, table, pool_id, by = _clients()
    attrs = _find(cognito, pool_id, args.email)
    if args.property and not UUID.fullmatch(args.property):
        raise SystemExit(f"--property must be a property uuid, got {args.property!r}")
    table.put_item(Item=_item(attrs["sub"], args.email, args.property, args.region, by, "manual"))
    print(f"{args.email}: {_label(args.property, args.region)}")
    claimed = (attrs.get("custom:property_id") or None, attrs.get("custom:region") or None)
    if claimed != (args.property, args.region):
        print(f"  NOTE: the account's own attributes currently say "
              f"{_label(*claimed)}, so the console will refuse it as SCOPE_MISMATCH until "
              "an administrator aligns them (AdminUpdateUserAttributes).")
    return 0


def remove(args) -> int:
    cognito, table, pool_id, _ = _clients()
    table.delete_item(Key={"sub": _find(cognito, pool_id, args.email)["sub"]})
    print(f"{args.email}: removed; the console will refuse it as NOT_REGISTERED")
    return 0


def list_(args) -> int:
    _, table, _, _ = _clients()
    items = []
    kwargs: dict = {}
    while True:
        page = table.scan(**kwargs)
        items += page["Items"]
        if "LastEvaluatedKey" not in page:
            break
        kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]
    for i in sorted(items, key=lambda i: i.get("email", "")):
        print(f"  {i.get('email', i['sub']):58} {_label(i.get('propertyId'), i.get('region')):48} "
              f"{i.get('source', '')}")
    print(f"{len(items)} registered")
    return 0


def check(args) -> int:
    cognito, table, pool_id, _ = _clients()
    attrs = _find(cognito, pool_id, args.email)
    item = table.get_item(Key={"sub": attrs["sub"]}).get("Item")
    claimed = (attrs.get("custom:property_id") or None, attrs.get("custom:region") or None)
    print(f"  token claims: {_label(*claimed)}")
    if not item:
        print("  registered:   (not registered)  -> console refuses: NOT_REGISTERED")
        return 1
    registered = (item.get("propertyId") or None, item.get("region") or None)
    print(f"  registered:   {_label(*registered)}")
    if claimed != registered:
        print("  -> console refuses: SCOPE_MISMATCH")
        return 1
    print("  -> console accepts")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("seed", help="propose (and with --apply, write) a registration per staff account")
    p.add_argument("--apply", action="store_true", help="write the proposals")
    p.add_argument("--overwrite", action="store_true", help="also re-seed accounts already registered")
    p.add_argument(
        "--exclude", action="append", default=[], metavar="EMAIL",
        help="a staff account that must not get console access; repeatable",
    )
    p.set_defaults(func=seed)

    p = sub.add_parser("set", help="register one operator")
    p.add_argument("email")
    scope = p.add_mutually_exclusive_group(required=True)
    scope.add_argument("--property", metavar="UUID")
    scope.add_argument("--region")
    scope.add_argument("--chain", action="store_true")
    p.set_defaults(func=set_)

    p = sub.add_parser("remove", help="unregister one operator")
    p.add_argument("email")
    p.set_defaults(func=remove)

    sub.add_parser("list", help="show every registration").set_defaults(func=list_)

    p = sub.add_parser("check", help="compare one operator's registration with their token claims")
    p.add_argument("email")
    p.set_defaults(func=check)

    args = parser.parse_args()
    if getattr(args, "command", None) == "set":
        args.property = args.property or None
        args.region = args.region or None
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
