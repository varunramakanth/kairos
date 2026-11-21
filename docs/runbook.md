# Runbook: Kairos on AWS

The production stack is described in [ADR-017](adr/017-deployment.md) and costs about
$59/month. Everything here uses an **admin profile**, never the root user and never the
SES-only key in `.env` (the `just` recipes drop those variables).

Throughout, `kairos-admin` is the AWS profile name and `ca-central-1` the region (ADR-006).

## 1. One-time account setup

1. **Admin access through IAM Identity Center.**
   - In the console, open IAM Identity Center and enable it in `ca-central-1`.
   - Create a user for yourself and assign it the `AdministratorAccess` permission set
     on this account.
   - Sign in on the CLI:

     ```bash
     aws configure sso --profile kairos-admin
     ```

     ```bash
     aws sts get-caller-identity --profile kairos-admin
     ```

     The ARN should contain `AWSReservedSSO_AdministratorAccess`, not `root`.

2. **Check the availability zones.** CloudFront VPC origins do not work in the AZ with
   ID `cac1-az3`.

   ```bash
   aws ec2 describe-availability-zones --region ca-central-1 --profile kairos-admin --query "AvailabilityZones[].[ZoneName,ZoneId]" --output text
   ```

   If `ca-central-1a` or `ca-central-1b` maps to `cac1-az3`, add
   `-c availability_zones=<two other zone names>` to every synth, diff and deploy below.

3. **Bootstrap CDK** (creates the asset bucket and ECR repository CDK deploys from):

   ```bash
   npx -y aws-cdk@2.1148.1 bootstrap aws://<account id>/ca-central-1 --profile kairos-admin
   ```

4. **SES** is already verified in `ca-central-1` (docs/ses-setup.md). Sandbox mode is
   fine: Kairos only emails you.

5. Docker Desktop must be running: deploys build both images (ARM64, native on an
   Apple-silicon Mac).

## 2. First deploy

Pick a Cognito domain prefix that is unique in the region, such as `kairos-` plus a few
random letters. It becomes `https://<prefix>.auth.ca-central-1.amazoncognito.com`.

1. **Synth.** This resolves the AZ and prefix-list lookups and records them in
   `infra/cdk/cdk.context.json`; commit that file.

   ```bash
   just synth kairos-admin -c owner_email=you@example.com -c cognito_prefix=kairos-abcd
   ```

2. **Review** what will be created:

   ```bash
   just cdk-diff kairos-admin -c owner_email=you@example.com -c cognito_prefix=kairos-abcd
   ```

3. **Deploy.** This takes 20–30 minutes, mostly RDS and CloudFront, and starts billing:

   ```bash
   just deploy kairos-admin -c owner_email=you@example.com -c cognito_prefix=kairos-abcd
   ```

   The outputs are `AppUrl`, `AppSecretName`, `UserPoolId` and `LogGroup`.

4. **Confirm the alarm subscription.** AWS emails a link from SNS; click it, or alarms
   never reach you.

5. **Fill in the API keys.** Open Secrets Manager, find `kairos/app`, choose
   *Retrieve secret value* then *Edit*, and paste:
   - `ANTHROPIC_API_KEY` (required);
   - `TAVILY_API_KEY` and `SERPAPI_API_KEY` (optional);
   - the VAPID pair from `uv run kairos vapid-keys` (optional).

   Use the console rather than a file on disk. Then restart the task so it reads them:

   ```bash
   aws ecs update-service --profile kairos-admin --region ca-central-1 --cluster <cluster> --service <service> --force-new-deployment
   ```

   Cluster and service names are on the ECS console page, or from
   `aws ecs list-clusters` and `aws ecs list-services`.

6. **Sign in.**
   - Cognito emails a temporary password.
   - Open `AppUrl` and sign in with your email and that password.
   - Set a new password of 12 characters or more.
   - Scan the TOTP QR code with an authenticator app.

7. **Check it works.**
   - `<AppUrl>/api/health` returns `ok`.
   - Send a chat message and watch it stream.
   - Approve a `notify_me` and the email arrives.

## 3. Day to day

| Task | How |
|---|---|
| Deploy a change | `just deploy kairos-admin -c owner_email=… -c cognito_prefix=…` (about 2 minutes of downtime while the single task is replaced) |
| Watch logs | `aws logs tail <LogGroup> --follow --profile kairos-admin --region ca-central-1` |
| Stop agent actions now | The kill switch on the Settings page (actions off) |
| Stop everything | `aws ecs update-service … --desired-count 0`; set it back to `1` to resume. RDS keeps billing |
| Rotate an API key | Edit `kairos/app`, then `--force-new-deployment` as in section 2 |
| Online eval results | `/evals`, suite `online` (written every 6 hours) |
| Costs | The `kairos-monthly` budget emails at 80% actual and 100% forecast; Cost Explorer for detail |

## 4. Rollback

- **Failed deploy:** ECS's circuit breaker rolls the service back to the last healthy task
  definition on its own, and CloudFormation rolls the stack back.
- **Bad but healthy release:** check out the previous good commit and `just deploy` it.
  This is safe when the newer release's migrations only added tables or columns.
  Otherwise, restore the database to before the deploy (section 5).

## 5. Restore the database

RDS keeps 7 days of point-in-time recovery. A restore creates a **new** instance; the
trick is to give it the old instance's identifier, so its endpoint, which the task uses,
stays the same.

1. Stop the task: `--desired-count 0`.
2. Note the instance identifier, DB subnet group and security group of the current
   database (RDS console → the instance → *Connectivity & security*).
3. Rename the current instance to `<identifier>-old`. Under *Modify*, change the DB
   instance identifier and apply immediately.
4. Restore to the original identifier:

   ```bash
   aws rds restore-db-instance-to-point-in-time --profile kairos-admin --region ca-central-1 --source-db-instance-identifier <identifier>-old --target-db-instance-identifier <identifier> --restore-time 2026-11-18T09:00:00Z --db-subnet-group-name <subnet group> --vpc-security-group-ids <security group> --db-instance-class db.t4g.micro --no-publicly-accessible --deletion-protection
   ```

5. When it is `available`, set `--desired-count 1` and check the app.
6. After a few days, delete `<identifier>-old`: turn off its deletion protection first,
   and keep a final snapshot.

The stack still points at the same identifier, so later deploys keep working. Expect
`cdk diff` to show drift for settings the restore did not copy, such as the backup
window; the next deploy realigns them.

## 6. Tear down

1. Run `npx -y aws-cdk@2.1148.1 destroy --profile kairos-admin` from `infra/cdk`.
2. What survives on purpose:
   - **The RDS instance:** deletion protection is on. Turn it off, delete the instance,
     and RDS takes a final snapshot.
   - **The Cognito user pool:** it is retained; delete it in the console.
   - **Secrets:** they are scheduled for deletion and stay recoverable for a recovery
     window; Secrets Manager lists them as *pending deletion*.
3. Final snapshots cost $0.105 per GB-month until you delete them.
