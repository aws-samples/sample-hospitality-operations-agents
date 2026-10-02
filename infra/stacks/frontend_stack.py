# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The ops console: its own bucket, its own distribution, and the API behind it.

Deliberately separate from the foundation's two frontends
(``anycompany-dev-frontend-*`` and ``anycompany-booking-pmsfrontendbucket-*``). This
project adds a console; it does not edit theirs.

One distribution, two origins
-----------------------------
``/api/*`` goes to the console API and everything else to the S3 bucket. That is not
a packaging convenience -- it is what makes the whole thing same-origin, so there is
no CORS configuration in ``api_stack``, no preflight on any request, and no API
hostname baked into the JavaScript bundle. A redeploy that changes the API's id needs
no frontend rebuild.

The trick that makes it free: the API's stage is named ``api``, so API Gateway already
serves ``/api/runs``. CloudFront forwards the path unchanged -- no origin path, no
CloudFront Function, no rewrite to get wrong.

What the bucket is not
----------------------
Not public, and not a website endpoint. Origin Access Control, so the bucket policy
grants exactly one distribution's ``s3:GetObject`` and nothing else can read it. SPA
routing is handled by mapping 403 and 404 to ``/index.html`` with a 200 -- 403 as well
as 404 because with OAC a missing key returns AccessDenied, not NotFound, and a
console that 403s on a page refresh is a console people stop refreshing.
"""

from __future__ import annotations

from pathlib import Path

from aws_cdk import CfnOutput, Duration, RemovalPolicy, Stack
from aws_cdk import aws_cloudfront as cloudfront
from aws_cdk import aws_cloudfront_origins as origins
from aws_cdk import aws_s3 as s3
from aws_cdk import aws_s3_deployment as s3deploy
from constructs import Construct

from stacks.api_stack import STAGE_NAME, ApiStack

REPO_ROOT = Path(__file__).resolve().parents[2]
FRONTEND_DIST = REPO_ROOT / "frontend" / "dist"

#: Hashed filenames from Vite, so these can be cached hard. `index.html` is excluded
#: below and gets no-cache, which is what makes a deploy take effect immediately.
ASSET_MAX_AGE = Duration.days(365)


class FrontendStack(Stack):
    """S3 + CloudFront + OAC for the console, with the API as a second origin."""

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        api: ApiStack,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        if not (FRONTEND_DIST / "index.html").is_file():
            raise FileNotFoundError(
                f"{FRONTEND_DIST}/index.html is missing. Run scripts/build_frontend.sh "
                "first -- it reads the Cognito ids from the deployed api stack and "
                "then runs the Vite build. Failing the synth here rather than "
                "deploying an empty bucket that serves 404 to every operator."
            )

        self.bucket = s3.Bucket(
            self,
            "ConsoleBucket",
            # Named by CloudFormation rather than by us: a fixed name would collide
            # on a second deployment into another account or region, and nothing
            # needs to guess this name -- OAC wires it to the distribution.
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            encryption=s3.BucketEncryption.S3_MANAGED,
            enforce_ssl=True,
            versioned=False,
            removal_policy=RemovalPolicy.DESTROY,
            # The bucket holds a build artifact and nothing else. Emptying it on
            # destroy is safe in a way that emptying the decision log would not be.
            auto_delete_objects=True,
        )

        self.distribution = cloudfront.Distribution(
            self,
            "ConsoleDistribution",
            comment="Hotel Operations Agent ops console",
            default_root_object="index.html",
            default_behavior=cloudfront.BehaviorOptions(
                origin=origins.S3BucketOrigin.with_origin_access_control(self.bucket),
                viewer_protocol_policy=cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
                allowed_methods=cloudfront.AllowedMethods.ALLOW_GET_HEAD_OPTIONS,
                cached_methods=cloudfront.CachedMethods.CACHE_GET_HEAD_OPTIONS,
                cache_policy=cloudfront.CachePolicy.CACHING_OPTIMIZED,
                compress=True,
            ),
            additional_behaviors={
                "/api/*": cloudfront.BehaviorOptions(
                    origin=origins.HttpOrigin(
                        api.api_domain,
                        protocol_policy=cloudfront.OriginProtocolPolicy.HTTPS_ONLY,
                        # Long enough for a chat POST and the run polls behind it. The
                        # API's own functions time out in 10s, so this only has to
                        # outlast them.
                        read_timeout=Duration.seconds(30),
                    ),
                    viewer_protocol_policy=cloudfront.ViewerProtocolPolicy.HTTPS_ONLY,
                    allowed_methods=cloudfront.AllowedMethods.ALLOW_ALL,
                    # Nothing here is cacheable: every response is scoped to the
                    # caller's own groups and property, and a cached /api/runs would
                    # serve one operator another's hotel.
                    cache_policy=cloudfront.CachePolicy.CACHING_DISABLED,
                    # Authorization must reach API Gateway or the Cognito authorizer
                    # sees an unauthenticated request. ALL_VIEWER_EXCEPT_HOST_HEADER
                    # rather than ALL_VIEWER because API Gateway rejects a Host header
                    # that is not its own.
                    origin_request_policy=cloudfront.OriginRequestPolicy.ALL_VIEWER_EXCEPT_HOST_HEADER,
                )
            },
            error_responses=[
                # Client-side routing. 403 as well as 404 because OAC answers a
                # missing key with AccessDenied.
                cloudfront.ErrorResponse(
                    http_status=403,
                    response_http_status=200,
                    response_page_path="/index.html",
                    ttl=Duration.minutes(5),
                ),
                cloudfront.ErrorResponse(
                    http_status=404,
                    response_http_status=200,
                    response_page_path="/index.html",
                    ttl=Duration.minutes(5),
                ),
            ],
            # North America and Europe. The chain is US-only today and the foundation
            # is single-region; PRICE_CLASS_ALL would pay for edges nobody uses.
            price_class=cloudfront.PriceClass.PRICE_CLASS_100,
            minimum_protocol_version=cloudfront.SecurityPolicyProtocol.TLS_V1_2_2021,
            enable_logging=False,
        )

        # Two deployments, because index.html and the hashed assets want opposite
        # cache headers. Assets are immutable and cached for a year; index.html must
        # never be cached, or an operator keeps loading a bundle that references
        # assets the last deploy replaced.
        s3deploy.BucketDeployment(
            self,
            "ConsoleAssets",
            sources=[s3deploy.Source.asset(str(FRONTEND_DIST))],
            destination_bucket=self.bucket,
            distribution=self.distribution,
            distribution_paths=["/*"],
            exclude=["index.html"],
            cache_control=[
                s3deploy.CacheControl.set_public(),
                s3deploy.CacheControl.max_age(ASSET_MAX_AGE),
                s3deploy.CacheControl.immutable(),
            ],
            prune=True,
        )
        s3deploy.BucketDeployment(
            self,
            "ConsoleIndex",
            sources=[s3deploy.Source.asset(str(FRONTEND_DIST), exclude=["assets/*"])],
            destination_bucket=self.bucket,
            distribution=self.distribution,
            distribution_paths=["/index.html"],
            cache_control=[
                s3deploy.CacheControl.no_cache(),
                s3deploy.CacheControl.must_revalidate(),
            ],
            # Must not prune: this deployment's source excludes the assets the other
            # one uploaded, so pruning would delete them.
            prune=False,
        )

        CfnOutput(
            self,
            "ConsoleUrl",
            value=f"https://{self.distribution.distribution_domain_name}",
            description="The ops console. Staff sign in with their AnyCompany accounts.",
        )
        CfnOutput(
            self,
            "ConsoleApiPath",
            value=f"https://{self.distribution.distribution_domain_name}/{STAGE_NAME}/",
            description=(
                "The API, same-origin behind the console. This is why the stack has "
                "no CORS configuration."
            ),
        )
        CfnOutput(self, "ConsoleBucketName", value=self.bucket.bucket_name)
        CfnOutput(
            self,
            "ConsoleDistributionId",
            value=self.distribution.distribution_id,
            description="For manual invalidation if a deploy needs forcing",
        )
