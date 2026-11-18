# ADR-006: Region: ca-central-1, models through the Anthropic API

- **Status:** Accepted (2026-11-18; Anthropic API chosen by the owner)
- **Milestone:** M8

## Context

PLAN.md left the region open until M8. The question was really about Bedrock: as of
2026-11 the current Claude models have no in-region inference in `ca-central-1` or
`us-east-1`; both use cross-region inference profiles. For M8 the owner chose the
Anthropic API (key in Secrets Manager) instead of Bedrock, so the model region is no
longer tied to the AWS region.

## Decision

Everything deployed, and every byte at rest, lives in **`ca-central-1`** (Canada Central):

- the database;
- its snapshots;
- logs;
- secrets;
- the Cognito user pool.

The SES identity was already verified there (docs/ses-setup.md).

## Consequences

- **Data residency.** Conversation history, memories and backups stay in Canada.
  Prompts and tool results still travel to Anthropic's API for inference, as they
  do locally. That is the trade the owner accepted by choosing the Anthropic API.
- **Bedrock later.** The `bedrock` provider still works (ADR-001). Moving to it
  would add an inference profile: a Canada-only one if AWS has one for the model,
  otherwise a geographic profile, which sends inference outside Canada.
- **Prices.** `ca-central-1` prices are a little above `us-east-1`. The estimate in
  ADR-017 uses the `ca-central-1` price list.
- **Availability zones.** CloudFront VPC origins are not supported in the AZ with ID
  `cac1-az3`. The stack takes `-c availability_zones=…` to avoid it (runbook).
