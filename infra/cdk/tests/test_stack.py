"""Offline assertions on the synthesized production template (no AWS calls).

These pin the properties that keep the stack cheap and closed: no NAT gateway, a
private database that survives deletion, one ARM64 task, an internal ALB that only
CloudFront reaches, Cognito with MFA and no self sign-up, and least-privilege email.
"""

from typing import Any

import pytest
from aws_cdk import App, Environment
from aws_cdk.assertions import Template

from kairos_infra.stack import Config, KairosStack

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


def test_no_nat_gateway_and_one_public_task(template: Template) -> None:
    template.resource_count_is("AWS::EC2::NatGateway", 0)
    service = only(template, "AWS::ECS::Service")
    network = service["NetworkConfiguration"]["AwsvpcConfiguration"]
    assert network["AssignPublicIp"] == "ENABLED"
    assert service["DesiredCount"] == 1
    assert service["DeploymentConfiguration"]["MinimumHealthyPercent"] == 0
    assert service["DeploymentConfiguration"]["MaximumPercent"] == 100
    ports = {(lb["ContainerName"], lb["ContainerPort"]) for lb in service["LoadBalancers"]}
    assert ports == {("api", 8000), ("web", 3000)}


def test_task_is_arm64_with_both_containers(template: Template) -> None:
    task = only(template, "AWS::ECS::TaskDefinition")
    assert task["RuntimePlatform"]["CpuArchitecture"] == "ARM64"
    assert (task["Cpu"], task["Memory"]) == ("512", "1536")
    assert {c["Name"] for c in task["ContainerDefinitions"]} == {"api", "web"}


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


def test_alb_is_internal_and_routes_api_by_path(template: Template) -> None:
    alb = only(template, "AWS::ElasticLoadBalancingV2::LoadBalancer")
    assert alb["Scheme"] == "internal"
    listener = only(template, "AWS::ElasticLoadBalancingV2::Listener")
    assert listener["Port"] == 80
    assert listener["DefaultActions"][0]["Type"] == "forward"
    rule = only(template, "AWS::ElasticLoadBalancingV2::ListenerRule")
    (condition,) = rule["Conditions"]
    assert condition["PathPatternConfig"]["Values"] == ["/api/*"]


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


def test_cloudfront_redirects_to_https_and_uses_a_vpc_origin(template: Template) -> None:
    props = only(template, "AWS::CloudFront::Distribution")["DistributionConfig"]
    assert props["DefaultCacheBehavior"]["ViewerProtocolPolicy"] == "redirect-to-https"
    (origin,) = props["Origins"]
    assert "VpcOriginConfig" in origin
    assert "CustomOriginConfig" not in origin
    vpc_origin = only(template, "AWS::CloudFront::VpcOrigin")["VpcOriginEndpointConfig"]
    assert vpc_origin["OriginProtocolPolicy"] == "http-only"
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


def test_api_secrets_are_injected_not_plain_environment(template: Template) -> None:
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
