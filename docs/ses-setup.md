# Email through Amazon SES (send-only)

Kairos emails exactly one person: you. These steps give it a key that can do only
that. It can send from one verified address to one recipient, and it cannot read
mail, change SES, or touch anything else in your AWS account.

Throughout, replace `you@example.com` with your address and `123456789012` with
your AWS account id.

## 1. Verify your address in SES

1. Sign in to the AWS console (use an admin user with MFA, not the root user, for daily work).
2. Switch the region to **Canada (Central) `ca-central-1`**.
3. Open **Amazon SES → Configuration → Identities → Create identity**.
4. Choose **Email address**, enter `you@example.com`, and create it.
5. Open the verification email AWS sends you and click the link. The identity
   should show **Verified**.

A new account is in the **SES sandbox**: it can send only to verified addresses,
200 emails a day, 1 per second. That is exactly what Kairos needs, since it only
emails you. You do not need production access.

## 2. Create a send-only policy

**IAM → Policies → Create policy → JSON**, then paste:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "KairosSendToOwnerOnly",
      "Effect": "Allow",
      "Action": "ses:SendEmail",
      "Resource": "arn:aws:ses:ca-central-1:123456789012:identity/you@example.com",
      "Condition": {
        "StringEquals": { "ses:FromAddress": "you@example.com" },
        "ForAllValues:StringEquals": { "ses:Recipients": ["you@example.com"] }
      }
    }
  ]
}
```

Name it `KairosSesSendToOwner`.

- **What it allows:** `SendEmail` from `you@example.com`, to `you@example.com`
  only. Nothing else, not even listing identities.
- **Why the conditions:** the `ses:Recipients` condition means that even a leaked
  key cannot email anyone else.
- **If sending later fails with `AccessDenied`:** the error message names the
  resource that was refused. Add that ARN to `Resource` rather than widening it to `*`.

## 3. Create a user for Kairos with only that policy

**IAM → Users → Create user**, name it `kairos-ses-sender`:

1. Do **not** give it console access.
2. Attach **only** `KairosSesSendToOwner`.
3. Open the user → **Security credentials → Create access key**. Choose
   **Application running outside AWS**.
4. Store it **outside the repo**, in `~/.aws/credentials`. The repo folder is
   synced by iCloud; the `~/.aws` folder is not. With the AWS CLI installed:

   ```bash
   aws configure --profile kairos-ses
   ```

   Enter the key, secret and region `ca-central-1`. Leave the output format empty.

A long-lived key is acceptable here because the policy makes it worthless for
anything but emailing you. When Kairos moves to AWS (M8) it uses the task's IAM
role instead, and this key can be deleted.

## 4. Point Kairos at it

Add to `.env`, with no spaces around `=`:

```
KAIROS_EMAIL_PROVIDER=ses
KAIROS_EMAIL_TO=you@example.com
KAIROS_SES_REGION=ca-central-1
KAIROS_SES_PROFILE=kairos-ses
```

Restart the API (`just api`). **Settings** should now say email is set up.
**Send a test email** sends one message, and the result per channel shows under
*Recent notifications*.

## Notes

- **Spam folder:** a `@gmail.com` (or other free-mail) sender address, sent
  through SES, does not pass the sender's DMARC alignment. Gmail usually still
  delivers it, but the first messages may land in Spam. Mark them **Not spam**,
  or add a filter for `[Kairos]` in the subject. If you own a domain, verifying
  the domain (with DKIM) in SES and sending from it fixes this properly.
- **Cost:** a few cents per thousand emails; a personal agent sends a handful a day.
- **Revoking:** delete or deactivate the access key under the IAM user. Kairos
  keeps working without email; failures show per channel in Settings.
