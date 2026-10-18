#!/usr/bin/env python3
"""CDK app entrypoint for the hosted read-only demo (SPEC-21 §21.4).

Not deployed by this change — see the SPEC-21 as-built note in
docs/specs/20261001-quality-hardening-specs.md. Run via `make demo-synth`/`make demo-deploy`
when ready (those targets set `AWS_PROFILE=bk_personal` by default — the profile this
project's AWS deployments use; override with `make demo-synth AWS_PROFILE=other-profile`),
or manually (needs Node.js: the ``aws-cdk-lib`` Python package itself shells out to a
bundled Node process, and the ``cdk`` CLI is npm-distributed, `npm install -g aws-cdk`):

    cd infra && python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
    AWS_PROFILE=bk_personal cdk synth   # renders the template only, deploys nothing
    AWS_PROFILE=bk_personal cdk deploy  # the actual deploy step

(No PATH tricks needed: cdk.json's "app" command points straight at ".venv/bin/python3".)
"""

import os

import aws_cdk as cdk
from demo_stack import DemoStack

app = cdk.App()

# `DemoStack` builds and pushes the image itself (a `DockerImageAsset` pointed at the
# repo-root Dockerfile) — nothing to pass in here for that. `cdk synth` still builds the
# image locally (needed to compute its content hash for the asset's name), so
# infra/demo-snapshot/ must exist first (`make demo-snapshot`) even just to synth.
alert_email = os.environ.get("DEMO_BUDGET_ALERT_EMAIL")

DemoStack(
    app,
    "ReinventConciergeDemo",
    budget_alert_email=alert_email,
    env=cdk.Environment(
        account=os.environ.get("CDK_DEFAULT_ACCOUNT"),
        region=os.environ.get("CDK_DEFAULT_REGION", "us-east-1"),
    ),
)

app.synth()
