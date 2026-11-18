"""``cdk synth`` / ``cdk deploy`` entry point. Settings come from CDK context:

    npx aws-cdk@2.1148.1 deploy -c owner_email=you@example.com -c cognito_prefix=kairos-abc123

``-c with_browser=true`` adds Chromium; ``-c monthly_budget_usd=60`` changes the budget.
The account comes from the AWS profile in use; the region is ca-central-1 (ADR-006).
"""

import os

from aws_cdk import App, Environment

from kairos_infra.stack import Config, KairosStack

REGION = "ca-central-1"


def main() -> None:
    app = App()

    def context(key: str, default: str | None = None) -> str:
        value = app.node.try_get_context(key)
        if value is None:
            if default is None:
                raise SystemExit(f"missing context value: -c {key}=...")
            return default
        return str(value)

    config = Config(
        owner_email=context("owner_email"),
        cognito_domain_prefix=context("cognito_prefix"),
        monthly_budget_usd=float(context("monthly_budget_usd", "80")),
        with_browser=context("with_browser", "false").lower() == "true",
        postgres_version=context("postgres_version", "17.9"),
    )
    KairosStack(
        app,
        "KairosProd",
        config=config,
        env=Environment(account=os.environ.get("CDK_DEFAULT_ACCOUNT"), region=REGION),
        description="Kairos personal agent: lean production stack (ADR-017)",
    )
    app.synth()


if __name__ == "__main__":
    main()
