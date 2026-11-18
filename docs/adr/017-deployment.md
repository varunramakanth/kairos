# ADR-017: Deployment: one lean AWS stack in CDK (Python)

- **Status:** Accepted (2026-11-18; architecture, environments, models and address chosen
  by the owner; the cost-cutting revision approved by the owner)
- **Milestone:** M8
- **Code:** `infra/cdk` (stack), `infra/docker` (images)
- **Runbook:** [docs/runbook.md](../runbook.md)

## Context

Kairos serves one person, so the deployment should cost tens of dollars a month, not hundreds. It must still be:

- **always on:** scheduled jobs and watches fire whether or not a laptop is awake;
- **closed:** reachable only after sign-in;
- **recoverable:** backed up, with a rollback path.

The owner's choices:

- **Architecture:** the lean one: Fargate, RDS, and an ALB behind CloudFront.
- **Environments:** `prod` only to start.
- **Models:** the Anthropic API, with the key in Secrets Manager.
- **Address:** the CloudFront default, with no custom domain.

## Decision

```
browser ──HTTPS──▶ CloudFront ──VPC origin──▶ internal ALB ──▶ one Fargate task (ARM64)
                    │                                      ├ /api/*  ▶ api + worker :8000
                    └ *.cloudfront.net certificate         └ else    ▶ web (Next.js) :3000
                                                                          │
                                                            RDS Postgres 17 (isolated subnets)
```

**Network**

- **One VPC, two AZs, no NAT gateway.**
  - **The task:** runs in a public subnet with a public IP. It needs outbound access to Anthropic, ECR and Secrets Manager. Its security group admits only the ALB.
  - **The ALB:** sits in isolated subnets.
  - **The database:** sits in other isolated subnets with no route out.

**Edge and sign-in**

- **CloudFront terminates HTTPS** with its default certificate. It reaches the
  internal ALB through a **VPC origin**, so the ALB has no public address.
- **The ALB's security group** admits only CloudFront's origin-facing prefix list.
- **Sign-in is in the app, not at the ALB.** Without a domain the ALB cannot serve HTTPS, so ALB-level Cognito authentication is not possible.
  - **How it works:** the web UI runs the Cognito Hosted UI with PKCE; the API verifies access tokens (RS256, JWKS, `token_use=access`, `client_id`).
  - **Cognito settings:** MFA (TOTP) required; no self sign-up; the owner is the only user.

**Compute**

- **One task, two containers:**
  - **API:** with the embedded worker.
  - **Web UI:** Next.js standalone build.
- **Size:** 0.5 vCPU / 1.5 GB on Graviton.
- **Deploys:** they replace the task (`min_healthy_percent=0`), so two workers never overlap, though leases make overlap safe anyway. The circuit breaker rolls back a deploy that fails health checks.
- **Database settings come from secrets:** the task builds `KAIROS_DATABASE_URL` from the RDS-generated secret at start. Migrations run before uvicorn starts.

**Data**

- **RDS Postgres t4g.micro, single AZ:** pgvector; gp3, 20 GB growing to 50 GB, encrypted.
- **Backups:** 7 days of automated backups with point-in-time restore. These replace M7's local backups, which are off in AWS.
- **Protection:** deletion protection is on, and removing the stack leaves a final snapshot.

**Secrets**

- **API keys:** `kairos/app` holds the API keys (Anthropic, Tavily, SerpApi, VAPID). The owner fills it in after the first deploy; an empty value leaves that feature off.
- **Database credentials:** the RDS secret, which RDS generates itself.

**Email**

- **SES, send-only:** the task role may send only from and to the owner (same policy as ADR-010).

**Watchfulness**

- **Alarms, by email through SNS:**
  - API 5xx errors;
  - unhealthy API or web targets;
  - low database storage;
  - high database CPU.
- **Monthly AWS budget:** $80 by default, with emails at 80% of actual spend and at 100% of forecast spend.
- **Model spend is separate:** it stays capped at $5 a day by the app (ADR-015).

**Online evals**

- **What is scored:** every finished production run, with label-free deterministic metrics: run health, tool errors, plan completion, cost and latency.
- **How:** spans are collected in memory and written every 6 hours as the `online` suite (`kairos_worker.online`).
- **Not included:** a sampled LLM judge would need prompt content in spans and paid model calls, so it is a later, opt-in step.

**Production model:** the API runs `claude-sonnet-5-5` as its main model in AWS, for cost.
Locally the default is Opus. Change `KAIROS_MODEL` in the stack to switch.

## Cost (ca-central-1, on-demand, 730 h/month)

Prices are from the AWS price-list files for `ca-central-1`, fetched 2026-11-18.

| Item | Rate | Per month |
|---|---|---|
| Fargate ARM, 0.5 vCPU | $0.03565 per vCPU-hour | $13.01 |
| Fargate ARM, 1.5 GB | $0.00389 per GB-hour | $4.26 |
| ALB | $0.02475 per hour | $18.07 |
| ALB capacity (~0.1 LCU average) | $0.0088 per LCU-hour | ~$0.64 |
| Public IPv4 (the task) | $0.005 per hour | $3.65 |
| RDS db.t4g.micro, single AZ | $0.018 per hour | $13.14 |
| RDS gp3, 20 GB | $0.127 per GB-month | $2.54 |
| RDS backups (up to the database size) | free | $0 |
| Secrets Manager, 2 secrets | $0.40 each | $0.80 |
| CloudWatch: 5 alarms, ~0.5 GB of logs | $0.10 each; $0.55/GB | ~$0.78 |
| ECR and CDK asset storage, ~2 GB | $0.10 per GB-month | ~$0.20 |
| CloudFront, VPC origin, Cognito (<10k MAU), SNS email, budget | free tiers | $0 |
| **Total** | | **~$57** |

Model calls are billed by Anthropic, separately. The $5 daily cap bounds them, and typical
use costs a few dollars a month. Turning on the browser (`-c with_browser=true`) adds
1 GB of task memory (~$2.84).

### Rejected layouts

- **Two tasks and a public ALB guarded by a secret header:** ~$81 a month.
  - **Public IPv4 addresses:** the ALB and each task need one, at $3.65 each per month.
  - **A second task:** each task pays its own CPU minimum.
- **NAT gateway with private tasks:** adds ~$37 a month in `ca-central-1` for no benefit to one user; the task's security group already blocks inbound traffic.
- **App Runner, Lightsail, or a single EC2 instance:**
  - **App Runner:** cheaper at idle, but it cannot run the always-on worker without paying for constant activity.
  - **EC2:** cheapest, but patching becomes ours, and it drops the managed health checks and rollbacks this reference codebase wants to show.
- **Aurora Serverless v2:** its minimum capacity costs more than a t4g.micro for this load.

## Consequences

- **Deploys are coupled:** a web-only change redeploys the API task too, which takes about two minutes of downtime with one task. That is acceptable for one user.
- **No custom domain:** sign-in redirect URLs use the `*.cloudfront.net` name. Adding a domain later means a certificate in `us-east-1` for CloudFront and new Cognito callback URLs.
- **Recorded lookups:** the stack looks up the CloudFront prefix list and the account's AZs. `cdk.context.json` records them and is committed after the first synth.
- **`dev` environment:** deferred. The stack is parameterized by `Config`, so a second instance needs a second stack id and a different Cognito prefix.
