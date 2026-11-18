"""The lean Kairos production stack (ADR-017).

    browser ──HTTPS──▶ CloudFront ──VPC origin──▶ internal ALB ──▶ one Fargate task (ARM64)
                        │                                      ├ /api/*  ▶ api + worker :8000
                        └ default cert (*.cloudfront.net)      └ else    ▶ web (Next.js) :3000
                                                                              │
                                                                RDS Postgres (isolated)

Choices, all for one user at the lowest sensible cost:

- **No NAT gateway.** Tasks run in public subnets with public IPs (needed to reach the
  Anthropic API, ECR and Secrets Manager), but their security group only admits the
  load balancer. The database sits in isolated subnets with no route out at all.
- **The ALB is internal.** CloudFront reaches it through a VPC origin, so it has no
  public address and nothing on the internet can call it directly. (No domain means no
  certificate, so CloudFront terminates HTTPS with its default one.)
- **One task, two containers**: the API with its embedded worker
  (``KAIROS_EMBEDDED_WORKER``) and the web UI, on Graviton (ARM64), which is cheaper.
  Leases make it safe to split or scale out later.
- **Sign-in is Cognito** (MFA required, no self sign-up), checked by the API itself.
"""

from dataclasses import dataclass
from pathlib import Path
from typing import cast

from aws_cdk import (
    CfnOutput,
    Duration,
    Environment,
    IgnoreMode,
    RemovalPolicy,
    SecretValue,
    Stack,
)
from aws_cdk import aws_budgets as budgets
from aws_cdk import aws_cloudfront as cloudfront
from aws_cdk import aws_cloudfront_origins as origins
from aws_cdk import aws_cloudwatch as cloudwatch
from aws_cdk import aws_cloudwatch_actions as cw_actions
from aws_cdk import aws_cognito as cognito
from aws_cdk import aws_ec2 as ec2
from aws_cdk import aws_ecs as ecs
from aws_cdk import aws_elasticloadbalancingv2 as elbv2
from aws_cdk import aws_iam as iam
from aws_cdk import aws_logs as logs
from aws_cdk import aws_rds as rds
from aws_cdk import aws_secretsmanager as secretsmanager
from aws_cdk import aws_sns as sns
from aws_cdk import aws_sns_subscriptions as subscriptions
from aws_cdk.aws_ecr_assets import Platform
from constructs import Construct

APP_SECRET_KEYS = (
    "ANTHROPIC_API_KEY",
    "TAVILY_API_KEY",
    "SERPAPI_API_KEY",
    "KAIROS_VAPID_PUBLIC_KEY",
    "KAIROS_VAPID_PRIVATE_KEY",
)
"""Filled in after the first deploy (runbook); empty values leave a feature off."""
REPO_ROOT = Path(__file__).resolve().parents[4]
"""Docker build context for both images (``.dockerignore`` applies)."""


@dataclass(frozen=True)
class Config:
    owner_email: str
    """Sign-in user, alarm and budget recipient, and the only email recipient."""
    cognito_domain_prefix: str
    """Globally unique per region, e.g. ``kairos-<something>``."""
    monthly_budget_usd: float = 80.0
    postgres_version: str = "17.9"
    with_browser: bool = False
    timezone: str = "America/Toronto"
    daily_spend_cap_usd: float = 5.0
    availability_zones: tuple[str, ...] = ()
    """Two AZ names to use. Empty: the first two of the account's. CloudFront VPC origins
    do not support the AZ with ID ``cac1-az3``; set this if your first two include it."""


class KairosStack(Stack):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        config: Config,
        env: Environment | None = None,
        description: str | None = None,
    ) -> None:
        super().__init__(scope, construct_id, env=env, description=description)
        self.config = config

        # --- network: public subnets for tasks, isolated ones for the database ------
        vpc = ec2.Vpc(
            self,
            "Vpc",
            max_azs=None if config.availability_zones else 2,
            availability_zones=list(config.availability_zones) or None,
            nat_gateways=0,
            subnet_configuration=[
                ec2.SubnetConfiguration(name="public", subnet_type=ec2.SubnetType.PUBLIC),
                ec2.SubnetConfiguration(name="edge", subnet_type=ec2.SubnetType.PRIVATE_ISOLATED),
                ec2.SubnetConfiguration(name="data", subnet_type=ec2.SubnetType.PRIVATE_ISOLATED),
            ],
        )
        alb_sg = ec2.SecurityGroup(self, "AlbSg", vpc=vpc, description="CloudFront to ALB")
        alb_sg.add_ingress_rule(
            ec2.Peer.prefix_list(self._cloudfront_prefix_list()),
            ec2.Port.tcp(80),
            "CloudFront origin-facing addresses only",
        )
        task_sg = ec2.SecurityGroup(self, "TaskSg", vpc=vpc, description="ALB to tasks")
        task_sg.add_ingress_rule(alb_sg, ec2.Port.tcp(8000), "API from the ALB")
        task_sg.add_ingress_rule(alb_sg, ec2.Port.tcp(3000), "web from the ALB")
        db_sg = ec2.SecurityGroup(
            self, "DbSg", vpc=vpc, description="tasks to Postgres", allow_all_outbound=False
        )
        db_sg.add_ingress_rule(task_sg, ec2.Port.tcp(5432), "Postgres from tasks")

        # --- database -------------------------------------------------------------
        db = rds.DatabaseInstance(
            self,
            "Postgres",
            engine=rds.DatabaseInstanceEngine.postgres(
                version=rds.PostgresEngineVersion.of(
                    config.postgres_version, config.postgres_version.split(".")[0]
                )
            ),
            instance_type=ec2.InstanceType.of(ec2.InstanceClass.T4G, ec2.InstanceSize.MICRO),
            vpc=vpc,
            vpc_subnets=ec2.SubnetSelection(subnet_group_name="data"),
            security_groups=[db_sg],
            database_name="kairos",
            credentials=rds.Credentials.from_generated_secret(
                "kairos", exclude_characters=" %+~`#$&*()|[]{}:;<>?!'/@\"\\,=^"
            ),
            allocated_storage=20,
            max_allocated_storage=50,
            storage_type=rds.StorageType.GP3,
            storage_encrypted=True,
            backup_retention=Duration.days(7),
            deletion_protection=True,
            removal_policy=RemovalPolicy.SNAPSHOT,
            multi_az=False,
            publicly_accessible=False,
            cloudwatch_logs_exports=["postgresql"],
        )
        assert db.secret is not None  # noqa: S101  # from_generated_secret always sets it
        db_secret = db.secret

        # --- secrets the owner fills in (API keys) ---------------------------------
        app_secret = secretsmanager.Secret(
            self,
            "AppSecret",
            secret_name="kairos/app",  # noqa: S106  # a name, not a value
            description="Kairos API keys; set the values after the first deploy (runbook).",
            secret_object_value={k: SecretValue.unsafe_plain_text("") for k in APP_SECRET_KEYS},
        )

        # jsii classes are not structurally their own interfaces to pyright; cast once.
        app_secret_ref = cast(secretsmanager.ISecret, app_secret)

        # --- sign-in ----------------------------------------------------------------
        pool = cognito.UserPool(
            self,
            "Users",
            self_sign_up_enabled=False,
            sign_in_aliases=cognito.SignInAliases(email=True),
            mfa=cognito.Mfa.REQUIRED,
            mfa_second_factor=cognito.MfaSecondFactor(sms=False, otp=True),
            password_policy=cognito.PasswordPolicy(min_length=12),
            account_recovery=cognito.AccountRecovery.EMAIL_ONLY,
            removal_policy=RemovalPolicy.RETAIN,
        )
        domain = pool.add_domain(
            "HostedUi",
            cognito_domain=cognito.CognitoDomainOptions(domain_prefix=config.cognito_domain_prefix),
        )
        cognito.CfnUserPoolUser(
            self,
            "Owner",
            user_pool_id=pool.user_pool_id,
            username=config.owner_email,
            user_attributes=[
                cognito.CfnUserPoolUser.AttributeTypeProperty(
                    name="email", value=config.owner_email
                ),
                cognito.CfnUserPoolUser.AttributeTypeProperty(name="email_verified", value="true"),
            ],
            desired_delivery_mediums=["EMAIL"],
        )

        # --- containers -------------------------------------------------------------
        cluster = ecs.Cluster(self, "Cluster", vpc=vpc)
        logs_group = logs.LogGroup(
            self,
            "Logs",
            retention=logs.RetentionDays.ONE_MONTH,
            removal_policy=RemovalPolicy.DESTROY,
        )
        alb = elbv2.ApplicationLoadBalancer(
            self,
            "Alb",
            vpc=vpc,
            internet_facing=False,
            security_group=alb_sg,
            vpc_subnets=ec2.SubnetSelection(subnet_group_name="edge"),
        )

        task = ecs.FargateTaskDefinition(
            self,
            "Task",
            cpu=512,
            memory_limit_mib=2560 if config.with_browser else 1536,
            runtime_platform=ecs.RuntimePlatform(
                cpu_architecture=ecs.CpuArchitecture.ARM64,
                operating_system_family=ecs.OperatingSystemFamily.LINUX,
            ),
        )
        task.add_to_task_role_policy(self._ses_send_only(config.owner_email))
        api_container = task.add_container(
            "api",
            image=ecs.ContainerImage.from_asset(
                str(REPO_ROOT),
                file="infra/docker/api.Dockerfile",
                build_args={"WITH_BROWSER": "true" if config.with_browser else "false"},
                ignore_mode=IgnoreMode.DOCKER,
                platform=Platform.LINUX_ARM64,
            ),
            logging=ecs.LogDrivers.aws_logs(stream_prefix="api", log_group=logs_group),
            environment={
                "KAIROS_EMBEDDED_WORKER": "1",
                "KAIROS_PATH_PREFIX": "/api",
                "KAIROS_MODEL": "claude-sonnet-5-5",
                "KAIROS_TIMEZONE": config.timezone,
                "KAIROS_EMAIL_PROVIDER": "ses",
                "KAIROS_EMAIL_TO": config.owner_email,
                "KAIROS_SES_REGION": self.region,
                "KAIROS_BACKUPS": "false",  # RDS automated backups instead
                "KAIROS_BROWSER": "1" if config.with_browser else "0",
                "KAIROS_DAILY_SPEND_CAP_USD": str(config.daily_spend_cap_usd),
                "KAIROS_AUTH_ISSUER": pool.user_pool_provider_url,
                "KAIROS_AUTH_DOMAIN": domain.base_url(),
                "DB_HOST": db.db_instance_endpoint_address,
                "DB_NAME": "kairos",
            },
            secrets={
                "DB_USER": ecs.Secret.from_secrets_manager(db_secret, "username"),
                "DB_PASSWORD": ecs.Secret.from_secrets_manager(db_secret, "password"),
                **{
                    key: ecs.Secret.from_secrets_manager(app_secret_ref, key)
                    for key in APP_SECRET_KEYS
                },
            },
            command=[
                "sh",
                "-c",
                'export KAIROS_DATABASE_URL="postgresql+asyncpg://$DB_USER:$DB_PASSWORD@$DB_HOST:5432/$DB_NAME"'
                " && kairos migrate && exec uvicorn --factory kairos_api.app:create_app"
                " --host 0.0.0.0 --port 8000 --proxy-headers --forwarded-allow-ips '*'",
            ],
            port_mappings=[ecs.PortMapping(container_port=8000)],
        )
        task.add_container(
            "web",
            image=ecs.ContainerImage.from_asset(
                str(REPO_ROOT),
                file="infra/docker/web.Dockerfile",
                ignore_mode=IgnoreMode.DOCKER,
                platform=Platform.LINUX_ARM64,
            ),
            logging=ecs.LogDrivers.aws_logs(stream_prefix="web", log_group=logs_group),
            port_mappings=[ecs.PortMapping(container_port=3000)],
        )

        service = ecs.FargateService(
            self,
            "Service",
            cluster=cluster,
            task_definition=task,
            desired_count=1,
            assign_public_ip=True,  # outbound to Anthropic, ECR, Secrets Manager; no NAT
            vpc_subnets=ec2.SubnetSelection(subnet_group_name="public"),
            security_groups=[task_sg],
            circuit_breaker=ecs.DeploymentCircuitBreaker(rollback=True),
            min_healthy_percent=0,  # one task: replace rather than run two workers
            max_healthy_percent=100,
        )
        db.connections.allow_default_port_from(task_sg)

        # --- load balancer: /api/* to the API container, the rest to the web one -----
        listener = alb.add_listener("Http", port=80, open=False)
        api_targets = elbv2.ApplicationTargetGroup(
            self,
            "ApiTargets",
            vpc=vpc,
            port=8000,
            protocol=elbv2.ApplicationProtocol.HTTP,
            targets=[service.load_balancer_target(container_name="api", container_port=8000)],
            health_check=elbv2.HealthCheck(path="/api/health", healthy_http_codes="200"),
            deregistration_delay=Duration.seconds(10),
        )
        web_targets = elbv2.ApplicationTargetGroup(
            self,
            "WebTargets",
            vpc=vpc,
            port=3000,
            protocol=elbv2.ApplicationProtocol.HTTP,
            targets=[service.load_balancer_target(container_name="web", container_port=3000)],
            health_check=elbv2.HealthCheck(path="/", healthy_http_codes="200"),
            deregistration_delay=Duration.seconds(10),
        )
        listener.add_target_groups("Web", target_groups=[web_targets])
        listener.add_target_groups(
            "Api",
            priority=10,
            conditions=[elbv2.ListenerCondition.path_patterns(["/api/*"])],
            target_groups=[api_targets],
        )
        # Streams (SSE) stay open; FastAPI pings every 15 s, inside the 60 s idle timeout.
        alb.set_attribute("idle_timeout.timeout_seconds", "60")

        # --- CloudFront: HTTPS for the browser, one origin for web and API -------------
        alb_origin = origins.VpcOrigin.with_application_load_balancer(
            alb,
            http_port=80,
            protocol_policy=cloudfront.OriginProtocolPolicy.HTTP_ONLY,
            read_timeout=Duration.seconds(60),
        )
        no_cache = cloudfront.BehaviorOptions(
            origin=alb_origin,
            viewer_protocol_policy=cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
            allowed_methods=cloudfront.AllowedMethods.ALLOW_ALL,
            cache_policy=cloudfront.CachePolicy.CACHING_DISABLED,
            origin_request_policy=cloudfront.OriginRequestPolicy.ALL_VIEWER_EXCEPT_HOST_HEADER,
        )
        distribution = cloudfront.Distribution(
            self,
            "Cdn",
            default_behavior=no_cache,
            additional_behaviors={
                "/api/*": no_cache,
                "/_next/static/*": cloudfront.BehaviorOptions(
                    origin=alb_origin,
                    viewer_protocol_policy=cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
                    cache_policy=cloudfront.CachePolicy.CACHING_OPTIMIZED,
                ),
            },
            price_class=cloudfront.PriceClass.PRICE_CLASS_100,
            comment="Kairos",
        )
        app_url = f"https://{distribution.distribution_domain_name}"

        client = pool.add_client(
            "Web",
            generate_secret=False,
            o_auth=cognito.OAuthSettings(
                flows=cognito.OAuthFlows(authorization_code_grant=True),
                scopes=[cognito.OAuthScope.OPENID, cognito.OAuthScope.EMAIL],
                callback_urls=[f"{app_url}/auth/callback"],
                logout_urls=[f"{app_url}/"],
            ),
            access_token_validity=Duration.hours(1),
            refresh_token_validity=Duration.days(30),
            prevent_user_existence_errors=True,
        )
        api_container.add_environment("KAIROS_AUTH_CLIENT_ID", client.user_pool_client_id)
        api_container.add_environment("KAIROS_WEB_BASE_URL", app_url)

        # --- alarms, by email ---------------------------------------------------------
        topic = sns.Topic(self, "Alarms", display_name="Kairos alarms")
        topic.add_subscription(
            cast(sns.ITopicSubscription, subscriptions.EmailSubscription(config.owner_email))
        )
        notify = cast(cloudwatch.IAlarmAction, cw_actions.SnsAction(cast(sns.ITopic, topic)))
        alarms = [
            cloudwatch.Alarm(
                self,
                "Api5xx",
                metric=api_targets.metrics.http_code_target(
                    elbv2.HttpCodeTarget.TARGET_5XX_COUNT, period=Duration.minutes(5)
                ),
                threshold=5,
                evaluation_periods=1,
                treat_missing_data=cloudwatch.TreatMissingData.NOT_BREACHING,
                alarm_description="The API returned 5xx errors.",
            ),
            cloudwatch.Alarm(
                self,
                "ApiUnhealthy",
                metric=api_targets.metrics.unhealthy_host_count(period=Duration.minutes(5)),
                threshold=1,
                evaluation_periods=2,
                alarm_description="The API task is failing its health check.",
            ),
            cloudwatch.Alarm(
                self,
                "WebUnhealthy",
                metric=web_targets.metrics.unhealthy_host_count(period=Duration.minutes(5)),
                threshold=1,
                evaluation_periods=2,
                alarm_description="The web task is failing its health check.",
            ),
            cloudwatch.Alarm(
                self,
                "DbStorageLow",
                metric=db.metric_free_storage_space(period=Duration.minutes(15)),
                threshold=2 * 1024**3,
                comparison_operator=cloudwatch.ComparisonOperator.LESS_THAN_THRESHOLD,
                evaluation_periods=1,
                alarm_description="Less than 2 GB free in the database.",
            ),
            cloudwatch.Alarm(
                self,
                "DbCpuHigh",
                metric=db.metric_cpu_utilization(period=Duration.minutes(15)),
                threshold=80,
                evaluation_periods=2,
                alarm_description="Database CPU above 80% for 30 minutes.",
            ),
        ]
        for alarm in alarms:
            alarm.add_alarm_action(notify)

        # --- monthly AWS budget ---------------------------------------------------------
        budgets.CfnBudget(
            self,
            "Budget",
            budget=budgets.CfnBudget.BudgetDataProperty(
                budget_type="COST",
                time_unit="MONTHLY",
                budget_limit=budgets.CfnBudget.SpendProperty(
                    amount=config.monthly_budget_usd, unit="USD"
                ),
                budget_name="kairos-monthly",
            ),
            notifications_with_subscribers=[
                budgets.CfnBudget.NotificationWithSubscribersProperty(
                    notification=budgets.CfnBudget.NotificationProperty(
                        notification_type=kind,
                        comparison_operator="GREATER_THAN",
                        threshold=threshold,
                        threshold_type="PERCENTAGE",
                    ),
                    subscribers=[
                        budgets.CfnBudget.SubscriberProperty(
                            subscription_type="EMAIL", address=config.owner_email
                        )
                    ],
                )
                for kind, threshold in (("ACTUAL", 80), ("FORECASTED", 100))
            ],
        )

        CfnOutput(self, "AppUrl", value=app_url)
        CfnOutput(self, "AppSecretName", value=app_secret.secret_name)
        CfnOutput(self, "UserPoolId", value=pool.user_pool_id)
        CfnOutput(self, "LogGroup", value=logs_group.log_group_name)

    def _cloudfront_prefix_list(self) -> str:
        """The AWS-managed prefix list of CloudFront's origin-facing addresses."""
        lookup = ec2.PrefixList.from_lookup(
            self,
            "CloudFrontOrigins",
            prefix_list_name="com.amazonaws.global.cloudfront.origin-facing",
        )
        return lookup.prefix_list_id

    def _ses_send_only(self, owner: str) -> iam.PolicyStatement:
        """The task may send email from and to the owner only (as ADR-010's IAM user)."""
        return iam.PolicyStatement(
            actions=["ses:SendEmail"],
            resources=[f"arn:aws:ses:{self.region}:{self.account}:identity/{owner}"],
            conditions={
                "StringEquals": {"ses:FromAddress": owner},
                "ForAllValues:StringEquals": {"ses:Recipients": [owner]},
            },
        )
