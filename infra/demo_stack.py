"""SPEC-21 §21.4: hosted demo infrastructure (Lightsail container, budget alarm, log retention).

Deployed and reached ``CREATE_COMPLETE`` — see the SPEC-21 as-built note in
docs/specs/20261001-quality-hardening-specs.md for the full history (circular-dependency fix,
an orphaned log group, an ARM64/Lightsail architecture mismatch, and the deployment-ordering
fix below), and ``app.py``'s docstring for how to run ``cdk synth``/``cdk deploy``.

Built on the AWS CDK L1 ``CfnContainer`` resource (``AWS::Lightsail::Container``): Lightsail
has no L2 container-service construct in ``aws-cdk-lib`` as of this writing (its own module
README says so explicitly), so every property name below is the CloudFormation resource's
own schema name, confirmed against the CDK API reference and, for the two trickiest pieces,
confirmed empirically (an actual `cdk synth`, not just a docs read):

- ``PrivateRegistryAccess``/``EcrImagePullerRole`` and the container/health-check shapes were
  confirmed by running `cdk synth` and reading the rendered template.
- The image is NOT taken straight from ``DockerImageAsset`` (CDK's own bootstrap-managed
  ECR repo): a real `cdk synth` showed that neither `grant_pull` nor `add_to_resource_policy`
  on `DockerImageAsset.repository` produces any policy resource at all — that repository is
  imported/shared-bootstrap and CDK won't let user code mutate its policy (matches the CDK
  Developer Guide's assets page: for a principal that isn't a role CDK itself owns, the
  guide's own recommended fix — calling `add_to_resource_policy` directly — is what was
  tried, and it *also* produced nothing, confirmed, not assumed). So this stack owns its
  own ``ecr.Repository`` instead (fully policy-mutable) and uses the third-party
  ``cdk-ecr-deployment`` construct to copy the locally-built asset image into it — the
  documented pattern for exactly this situation.
- The pull-access grant is NOT `repository.grant_pull(...)` (which sets the policy as an
  inline property *on the `AWS::ECR::Repository` resource itself*): a real `cdk deploy`
  failed with "Circular dependency between resources: [DemoContainerService,
  PushDemoImageCustomResource..., DemoRepositoryEDDAABE5]", because the policy needs
  Lightsail's auto-created role ARN (only known once `DemoContainerService` exists), while
  `DemoContainerService` needs the image already pushed into `DemoRepository` (so it depends
  on `DemoRepository` transitively through the push step) — repo→container→...→repo. ECR
  has no separate `AWS::ECR::RepositoryPolicy` resource type to split this onto (confirmed:
  no such class exists in `aws_cdk.aws_ecr`), so the fix is a `custom_resources
  .AwsCustomResource` calling `ECR.SetRepositoryPolicy` directly — a genuinely new resource
  node depending on both `repository` and `container_service`, with no edge back into
  either, so the graph stays acyclic.
- `container_service` does NOT carry an inline `container_service_deployment`, even though
  that's the obvious way to express "run this image": a real `cdk deploy`, past the fix
  above, reached `CREATE_COMPLETE` but the Lightsail deployment itself still failed
  (3 restart attempts, then cancelled, zero application-level log output). CloudFormation
  events showed why — `DemoContainerService` stayed `CREATE_IN_PROGRESS` for the entire
  ~6 minutes Lightsail spent on that first deployment attempt, and `GrantEcrPullToLightsail`
  only started once `DemoContainerService` reported complete. An embedded
  `container_service_deployment` makes a `CfnContainer` resource's own completion wait on
  that deployment settling, so Lightsail's first pull attempt always happens with no ECR
  permission granted yet — unavoidable no matter how dependencies are drawn, since the
  permission genuinely cannot exist until after the role it names exists. This matches AWS's
  own documented order for Lightsail+private-ECR access (activate the puller role, grant the
  ECR policy, *then* create the deployment), so the deployment was moved out into a separate
  `CreateDemoDeployment` custom resource calling `Lightsail.CreateContainerServiceDeployment`
  directly, depending on `grant_pull` (not yet re-verified by a real deploy — see the as-built
  note).
"""

import json
from pathlib import Path
from typing import Optional

import cdk_ecr_deployment as ecrdeploy
from aws_cdk import RemovalPolicy, Stack
from aws_cdk import aws_budgets as budgets
from aws_cdk import aws_ecr as ecr
from aws_cdk import aws_ecr_assets as ecr_assets
from aws_cdk import aws_lightsail as lightsail
from aws_cdk import aws_logs as logs
from aws_cdk import custom_resources as cr
from constructs import Construct

SERVICE_NAME = "reinvent-concierge-demo"
CONTAINER_NAME = "demo"
CONTAINER_PORT = 8080
REPO_ROOT = Path(__file__).resolve().parent.parent
# Written by `make demo-snapshot` (DEMO_EVENT_ID): the event the baked catalog came from.
SNAPSHOT_EVENT_ID_FILE = REPO_ROOT / "infra" / "demo-snapshot" / "event_id"


def snapshot_event_id() -> str:
    """Event ID the baked catalog snapshot was synced from; the image is built for it."""
    try:
        event_id = SNAPSHOT_EVENT_ID_FILE.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        event_id = ""
    if not event_id:
        raise RuntimeError(
            f"{SNAPSHOT_EVENT_ID_FILE} is missing or empty; run `make demo-snapshot` "
            "(optionally DEMO_EVENT_ID=<public-event-id>) first"
        )
    return event_id


class DemoStack(Stack):
    """One Lightsail container service running this repo's ``Dockerfile``."""

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        budget_alert_email: Optional[str] = None,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # Built locally and published to CDK's own bootstrap-managed ECR repo, which this
        # stack cannot grant a third party (Lightsail's puller role) pull access on — see
        # the module docstring. Copied below into a repo this stack owns instead.
        built_image = ecr_assets.DockerImageAsset(
            self,
            "DemoImage",
            directory=str(REPO_ROOT),
            file="Dockerfile",
            build_args={"AWS_EVENTS_EVENT_ID": snapshot_event_id()},
            # Lightsail's container runtime is x86_64-only (no architecture selector on
            # AWS::Lightsail::Container); building for arm64 produced an instant
            # "exec format error" crash-loop with zero app-level logs on deploy.
            platform=ecr_assets.Platform.LINUX_AMD64,
        )

        repository = ecr.Repository(
            self,
            "DemoRepository",
            repository_name=SERVICE_NAME,
            removal_policy=RemovalPolicy.DESTROY,
            empty_on_delete=True,
            lifecycle_rules=[ecr.LifecycleRule(max_image_count=5)],
        )
        image_uri = f"{repository.repository_uri}:latest"
        push_image = ecrdeploy.ECRDeployment(
            self,
            "PushDemoImage",
            src=ecrdeploy.DockerImageName(built_image.image_uri),
            dest=ecrdeploy.DockerImageName(image_uri),
        )

        logs.LogGroup(
            self,
            "DemoLogGroup",
            log_group_name=f"/reinvent-concierge/{SERVICE_NAME}",
            retention=logs.RetentionDays.TWO_WEEKS,
            removal_policy=RemovalPolicy.DESTROY,
        )

        # No `container_service_deployment` here (deliberately): CloudFormation waits for an
        # embedded deployment to settle before marking THIS resource CREATE_COMPLETE, so the
        # container's first pull attempt would always happen before `GrantEcrPullToLightsail`
        # below has run (confirmed: a real deploy showed `DemoContainerService` still
        # CREATE_IN_PROGRESS for the full ~6 minutes Lightsail spent retrying and finally
        # cancelling deployment #1, with `GrantEcrPullToLightsail` only starting afterwards —
        # i.e. the pull policy didn't exist yet for any attempt Lightsail made). This matches
        # AWS's own documented order for Lightsail+private-ECR: activate the puller role,
        # grant the ECR policy, *then* create the deployment — so the deployment is moved to
        # a separate `CreateDemoDeployment` custom resource below, ordered after the grant.
        container_service = lightsail.CfnContainer(
            self,
            "DemoContainerService",
            service_name=SERVICE_NAME,
            power="micro",  # 0.25 vCPU / 1 GB, per SPEC-21 §21.3's recommended hosting option
            scale=1,
            # Lets this service pull the image above; Lightsail creates and owns the IAM
            # role, whose ARN is read back below to grant it pull access on `repository`.
            private_registry_access=lightsail.CfnContainer.PrivateRegistryAccessProperty(
                ecr_image_puller_role=lightsail.CfnContainer.EcrImagePullerRoleProperty(
                    is_active=True
                )
            ),
        )

        # A custom resource (not repository.grant_pull — see the module docstring) so this
        # grant depends on both `repository` and `container_service` without putting an edge
        # from `repository` back into `container_service`'s own dependency chain.
        pull_policy = json.dumps(
            {
                "Version": "2012-10-17",
                "Statement": [
                    {
                        "Effect": "Allow",
                        "Principal": {
                            "AWS": (
                                container_service.attr_private_registry_access_ecr_image_puller_role_principal_arn
                            )
                        },
                        "Action": [
                            "ecr:BatchCheckLayerAvailability",
                            "ecr:GetDownloadUrlForLayer",
                            "ecr:BatchGetImage",
                        ],
                    }
                ],
            }
        )
        sdk_call_params = {
            "repositoryName": repository.repository_name,
            "policyText": pull_policy,
        }
        physical_resource_id = cr.PhysicalResourceId.of(f"{SERVICE_NAME}-ecr-pull-policy")
        grant_pull = cr.AwsCustomResource(
            self,
            "GrantEcrPullToLightsail",
            on_create=cr.AwsSdkCall(
                service="ECR",
                action="setRepositoryPolicy",
                parameters=sdk_call_params,
                physical_resource_id=physical_resource_id,
            ),
            on_update=cr.AwsSdkCall(
                service="ECR",
                action="setRepositoryPolicy",
                parameters=sdk_call_params,
                physical_resource_id=physical_resource_id,
            ),
            policy=cr.AwsCustomResourcePolicy.from_sdk_calls(resources=[repository.repository_arn]),
        )
        grant_pull.node.add_dependency(repository)
        grant_pull.node.add_dependency(container_service)

        # The actual deployment (what used to be `container_service`'s inline
        # `container_service_deployment`), issued only now that `grant_pull` has run — so
        # Lightsail's first pull attempt has the ECR permission in place. `containers` and
        # `publicEndpoint` here use the CreateContainerServiceDeployment API's own
        # (camelCase) field names, not the CFN property names used above.
        deploy_params = {
            "serviceName": SERVICE_NAME,
            "containers": {
                CONTAINER_NAME: {
                    "image": image_uri,
                    "ports": {str(CONTAINER_PORT): "HTTP"},
                }
            },
            "publicEndpoint": {
                "containerName": CONTAINER_NAME,
                "containerPort": CONTAINER_PORT,
                "healthCheck": {
                    "healthyThreshold": 2,
                    "unhealthyThreshold": 3,
                    "path": "/_stcore/health",
                    "successCodes": "200",
                    "timeoutSeconds": 5,
                    "intervalSeconds": 30,
                },
            },
        }
        deploy_physical_id = cr.PhysicalResourceId.of(f"{SERVICE_NAME}-deployment")
        create_deployment = cr.AwsCustomResource(
            self,
            "CreateDemoDeployment",
            on_create=cr.AwsSdkCall(
                service="Lightsail",
                action="createContainerServiceDeployment",
                parameters=deploy_params,
                physical_resource_id=deploy_physical_id,
            ),
            on_update=cr.AwsSdkCall(
                service="Lightsail",
                action="createContainerServiceDeployment",
                parameters=deploy_params,
                physical_resource_id=deploy_physical_id,
            ),
            # Lightsail's control-plane actions don't support resource-level ARNs; AWS's own
            # managed policies for Lightsail use Resource: "*" for the same reason.
            policy=cr.AwsCustomResourcePolicy.from_sdk_calls(
                resources=cr.AwsCustomResourcePolicy.ANY_RESOURCE
            ),
        )
        create_deployment.node.add_dependency(push_image)
        create_deployment.node.add_dependency(grant_pull)

        # NotificationProperty/SubscriberProperty field names below are not independently
        # verified against the CDK API reference the way the rest of this file's property
        # names are (see the module docstring) — only exercised when DEMO_BUDGET_ALERT_EMAIL
        # is set, so `cdk synth` with it unset (the default) never reaches this branch.
        subscribers = (
            [
                budgets.CfnBudget.SubscriberProperty(
                    address=budget_alert_email, subscription_type="EMAIL"
                )
            ]
            if budget_alert_email
            else []
        )
        budgets.CfnBudget(
            self,
            "DemoBudget",
            budget=budgets.CfnBudget.BudgetDataProperty(
                budget_name=f"{SERVICE_NAME}-monthly",
                budget_type="COST",
                time_unit="MONTHLY",
                budget_limit=budgets.CfnBudget.SpendProperty(amount=20, unit="USD"),
            ),
            notifications_with_subscribers=(
                [
                    budgets.CfnBudget.NotificationWithSubscribersProperty(
                        notification=budgets.CfnBudget.NotificationProperty(
                            notification_type="ACTUAL",
                            comparison_operator="GREATER_THAN",
                            threshold=100,
                            threshold_type="PERCENTAGE",
                        ),
                        subscribers=subscribers,
                    )
                ]
                if budget_alert_email
                else []
            ),
        )
