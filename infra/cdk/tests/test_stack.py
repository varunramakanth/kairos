"""Offline assertions on the synthesized production template (no AWS calls).

These pin the properties that keep the stack cheap and closed: no NAT gateway, a
private database that survives deletion, an ALB that only answers CloudFront, Cognito
with MFA and no self sign-up, and least-privilege email sending.
"""

from typing import Any

import pytest
from aws_cdk import App, Environment
from aws_cdk.assertions import Match, Template

from kairos_infra.stack import ORIGIN_HEADER, Config, KairosStack

OWNER = "owner@example.com"


@pytest.fixture(scope="module")
def template() -> Template:
    app = App(context={"aws:cdk:bundling-stacks": []})  # stage assets without building images
    stack = KairosStack(
        app,
        "KairosTest",
        config=Config(owner_email=OWNER, cognito_domain_prefix="kairos-test"),
        env=Environment(account="111111111111", region="ca-central-1"),
    )
    return Template.from_stack(stack)


def only(template: Template, kind: str) -> dict[str, Any]:
    resources = template.find_resources(kind)
    assert len(resources) == 1, f"expected one {kind}, found {len(resources)}"
    return next(iter(resources.values()))["Properties"]


def test_no_nat_gateway_and_tasks_get_public_ips(template: Template) -> None:
    template.resource_count_is("AWS::EC2::NatGateway", 0)
    services = template.find_resources("AWS::ECS::Service")
    assert len(services) == 2
    for service in services.values():
        network = service["Properties"]["NetworkConfiguration"]["AwsvpcConfiguration"]
        assert network["AssignPublicIp"] == "ENABLED"


def test_database_is_private_encrypted_and_protected(template: Template) -> None:
    db = template.find_resources("AWS::RDS::DBInstance")
    assert len(db) == 1
    (resource,) = db.values()
    props = resource["Properties"]
    assert props["DBInstanceClass"] == "db.t4g.micro"
    assert props["PubliclyAccessible"] is False
    assert props["StorageEncrypted"] is True
    assert props["DeletionProtection"] is True
    assert props["BackupRetentionPeriod"] == 7
    assert props["MultiAZ"] is False
    assert resource["DeletionPolicy"] == "Snapshot"


def test_alb_answers_only_requests_with_the_origin_header(template: Template) -> None:
    template.has_resource_properties(
        "AWS::ElasticLoadBalancingV2::Listener",
        {
            "Port": 80,
            "DefaultActions": [
                Match.object_like(
                    {
                        "Type": "fixed-response",
                        "FixedResponseConfig": Match.object_like({"StatusCode": "403"}),
                    }
                )
            ],
        },
    )
    rules = template.find_resources("AWS::ElasticLoadBalancingV2::ListenerRule")
    assert len(rules) == 2
    for rule in rules.values():
        fields = [
            c.get("HttpHeaderConfig", {}).get("HttpHeaderName")
            for c in rule["Properties"]["Conditions"]
        ]
        assert ORIGIN_HEADER in fields


def test_alb_ingress_is_cloudfront_prefix_list_only(template: Template) -> None:
    groups = template.find_resources(
        "AWS::EC2::SecurityGroup", {"Properties": {"GroupDescription": "CloudFront to ALB"}}
    )
    ((group_id, group),) = groups.items()
    assert "SecurityGroupIngress" not in group["Properties"]  # no inline (CIDR) rules
    ingress = [
        rule["Properties"]
        for rule in template.find_resources("AWS::EC2::SecurityGroupIngress").values()
        if rule["Properties"]["GroupId"] == {"Fn::GetAtt": [group_id, "GroupId"]}
    ]
    assert len(ingress) == 1
    assert ingress[0]["SourcePrefixListId"].startswith("pl-")
    assert ingress[0]["FromPort"] == ingress[0]["ToPort"] == 80


def test_cloudfront_redirects_to_https_and_sends_the_header(template: Template) -> None:
    props = only(template, "AWS::CloudFront::Distribution")["DistributionConfig"]
    assert props["DefaultCacheBehavior"]["ViewerProtocolPolicy"] == "redirect-to-https"
    (origin,) = props["Origins"]
    assert origin["CustomOriginConfig"]["OriginProtocolPolicy"] == "http-only"
    assert origin["OriginCustomHeaders"][0]["HeaderName"] == ORIGIN_HEADER
    paths = {b["PathPattern"] for b in props["CacheBehaviors"]}
    assert paths == {"/api/*", "/_next/static/*"}


def test_cognito_requires_mfa_and_admin_created_users(template: Template) -> None:
    pool = only(template, "AWS::Cognito::UserPool")
    assert pool["MfaConfiguration"] == "ON"
    assert pool["EnabledMfas"] == ["SOFTWARE_TOKEN_MFA"]
    assert pool["AdminCreateUserConfig"]["AllowAdminCreateUserOnly"] is True
    assert pool["Policies"]["PasswordPolicy"]["MinimumLength"] == 12
    client = only(template, "AWS::Cognito::UserPoolClient")
    assert client["AllowedOAuthFlows"] == ["code"]
    assert client["GenerateSecret"] is False
    template.has_resource_properties("AWS::Cognito::UserPoolUser", {"Username": OWNER})


def test_task_may_send_email_only_to_and_from_the_owner(template: Template) -> None:
    policies = template.find_resources("AWS::IAM::Policy")
    ses = [
        s
        for p in policies.values()
        for s in p["Properties"]["PolicyDocument"]["Statement"]
        if s["Action"] == "ses:SendEmail"
    ]
    assert len(ses) == 1
    assert ses[0]["Condition"] == {
        "StringEquals": {"ses:FromAddress": OWNER},
        "ForAllValues:StringEquals": {"ses:Recipients": [OWNER]},
    }


def test_api_runs_one_task_with_secrets_injected(template: Template) -> None:
    template.has_resource_properties(
        "AWS::ECS::Service",
        {
            "DesiredCount": 1,
            "DeploymentConfiguration": Match.object_like(
                {"MinimumHealthyPercent": 0, "MaximumPercent": 100}
            ),
        },
    )
    tasks = template.find_resources("AWS::ECS::TaskDefinition")
    containers = {
        c["Name"]: c for t in tasks.values() for c in t["Properties"]["ContainerDefinitions"]
    }
    api = containers["api"]
    secret_names = {s["Name"] for s in api["Secrets"]}
    assert {"ANTHROPIC_API_KEY", "DB_USER", "DB_PASSWORD"} <= secret_names
    env_names = {e["Name"] for e in api["Environment"]}
    assert "ANTHROPIC_API_KEY" not in env_names  # never a plain environment variable
    assert {"KAIROS_AUTH_ISSUER", "KAIROS_AUTH_CLIENT_ID", "KAIROS_PATH_PREFIX"} <= env_names


def test_alarms_and_budget_email_the_owner(template: Template) -> None:
    template.resource_count_is("AWS::CloudWatch::Alarm", 5)
    template.has_resource_properties(
        "AWS::SNS::Subscription", {"Protocol": "email", "Endpoint": OWNER}
    )
    budget = only(template, "AWS::Budgets::Budget")
    assert budget["Budget"]["BudgetLimit"] == {"Amount": 80, "Unit": "USD"}
    kinds = {n["Notification"]["NotificationType"] for n in budget["NotificationsWithSubscribers"]}
    assert kinds == {"ACTUAL", "FORECASTED"}
