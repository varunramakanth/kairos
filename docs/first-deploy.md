# The first deploy: what went wrong, how it was found, what it teaches

Kairos first went live on AWS on 2026-11-21. Six things went wrong along the way. None
lost data or exposed anything. Each one is written up here as a short post-mortem:

- **Symptom:** what you saw.
- **Cause:** what was actually wrong.
- **How it was found:** the investigation, step by step, because that is the part worth learning.
- **Fix:** what changed.
- **Lesson:** what to take away.

They are in the order they happened. For the vocabulary (Fargate, CloudFront, Cognito
and so on), see [aws-explained.md](aws-explained.md).

| # | Problem | Kind | Would it have reached production? |
|---|---|---|---|
| 1 | AWS sign-in run in the wrong terminal | Setup | No, it just wouldn't work |
| 2 | Fargate memory size not allowed | Infrastructure bug | No: the deploy would have failed |
| 3 | Tests picked up copies of the repo | Tooling | No |
| 4 | Signing in never finished, and the app opened without a session | **Application bug** | **Yes**: it did, for about 30 minutes |
| 5 | The fix looked like it hadn't worked | Operations | — |
| 6 | iCloud kept duplicating files | Local environment | Possibly |

---

## 1. `aws configure sso` hung, in the wrong terminal

**Symptom.** The command printed a long `https://oidc.ca-central-1.amazonaws.com/authorize?…`
link and then waited forever.

**Cause.** It was running in **AWS CloudShell**, the terminal built into the AWS console,
not on the Mac. The sign-in flow ends by sending your browser back to
`http://127.0.0.1:<port>/oauth/callback`, which means "this computer". For CloudShell,
"this computer" is a machine at AWS, so your browser could never reach the waiting
command. Even if it had worked, the profile would have been saved inside CloudShell,
while deploys run on the Mac, because Docker builds the images there.

**How it was found.** The tab was labelled `ca-central-1`, which is CloudShell's style.
Checking the app's own Terminal panel showed it wasn't running anything, which confirmed it.

**Fix.** The same command in the Mac's Terminal app. For machines with no browser,
`aws configure sso --use-device-code` shows a short code to type into a web page instead.

**Lesson.** Credentials belong on the machine that will use them. A `127.0.0.1` callback
only works when the browser and the command are on the same computer.

---

## 2. Fargate rejected the task size, caught before deploying

**Symptom.** None: it was caught before anything was deployed. The first `just synth`
against the real account printed:

```
WARNING Cpu: Cpu '512' is not compatible with Memory '1536' for Fargate (CloudFormation Validate)
```

**Cause.** Fargate only accepts certain pairs of CPU and memory. At 0.5 vCPU (`512`), the
memory must be a whole number of gigabytes from 1 to 4. The stack asked for 1.5 GB, because
the API needs about 1 GB and the web container about 0.5. The offline tests checked that
the stack asked for 1.5 GB, but not whether AWS would accept it.

**How it was found.** CDK checks the template against CloudFormation's rules during
synth. That check only runs with the real tooling, which is why it never showed up in
the offline tests.

**Fix.** 2 GB ([`stack.py`](../infra/cdk/src/kairos_infra/stack.py)), adding about
$1.40 a month. The test now also checks that the memory is a size Fargate accepts at
0.5 vCPU ([`test_stack.py`](../infra/cdk/tests/test_stack.py)).

**Lesson.** Read warnings, not just errors: this one would have made the deploy fail and
roll back after 15 minutes. A test that only repeats the code's own numbers
(`assert memory == 1536`) proves nothing; check the rule instead (`memory in {1024,
2048, 3072, 4096}`). And `synth` and `diff` are free, so run them before every deploy.

---

## 3. The test run tried to test copies of the repository

**Symptom.** After the first synth, `pytest` failed while collecting tests, with errors
from paths like `infra/cdk/cdk.out/asset.4513d…/packages/kairos_policy/tests/…`.

**Cause.** Synth copies the whole repository into `infra/cdk/cdk.out/asset.*` folders.
Those copies are the Docker build contexts, the files Docker builds each image from.
Pytest searched inside them and found a second copy of every test.

**Fix.** `pyproject.toml` now tells pytest (`norecursedirs`) and pyright (`exclude`) to
skip `cdk.out`.

**Lesson.** Generated folders must be excluded from every tool that searches the tree,
not just from git.

---

## 4. "Sign in required" after signing in

This is the one worth studying: a real application bug that only appeared in production.

### Symptom

Signing in worked: email, password, then the authenticator code. The app opened. But
every message failed with **"sign in required"**.

### How it was found

A plan helped: narrow it down one layer at a time, using evidence rather than guesses.

**Step 1: which side loses the token?** "Sign in required" is one of three 401 messages
in [`auth.py`](../services/api/src/kairos_api/auth.py), and each means something different:

| Message | Meaning |
|---|---|
| `sign in required` | The request had **no** `Authorization` header at all |
| `invalid or expired token` | A token arrived, but its signature or expiry was wrong |
| `token not issued for this app` | A valid token, but for a different Cognito app |

Sending a fake token straight to the live API through CloudFront showed which:

```bash
curl -H "Authorization: Bearer not-a-real-token" https://<app>/api/threads
# {"detail":"invalid or expired token"}
```

So CloudFront **does** pass the header on, and the API reads it. The browser simply
wasn't sending one.

**Step 2: which requests fail?** The API's logs list every request with its status.
Every ordinary call (`/api/threads`, `/api/settings`, `/api/approvals`) was 401,
repeating every 15 seconds: the sidebar refreshing. So it wasn't one broken page: **no**
request carried a token.

**Step 3: why would the token be missing?** The web app attaches the token in one
place, a request **interceptor**. That is a function the generated API client runs on
every request before sending it. Reading the client's code showed it does run every
registered interceptor. So the interceptor had never been **registered**.

[`initAuth()`](../apps/web/src/lib/auth.ts) registers it, and only when the API's
`/auth/config` says sign-in is enabled. So the question became: did `initAuth` decide
sign-in was **off**?

**Step 4: the timeline.** The logs showed the 401s starting at 01:15. Returning from
Cognito is a full page load, so it should start with an `/api/auth/config` request, but
there wasn't one at 01:15. So the page *did* ask for its settings, just not from `/api/…`.

**Step 5: reproduce it.** Opening the deployed `/auth/callback` page in a browser and
watching its network requests settled it:

```
GET https://<app>/auth/config   → 404
```

No `/api` prefix. That request went to the web container, which has no such page.

### Cause

Two mistakes, each harmless alone and broken together.

**Mistake 1: the order modules load in.** The API client starts with no base URL.
[`lib/api.ts`](../apps/web/src/lib/api.ts) sets it to `/api` when it is first imported:

```ts
client.setConfig({ baseUrl: API_URL });   // API_URL = "/api" in production
```

Most pages import `lib/api.ts` indirectly, so it was usually set in time. But the page
Cognito sends you back to, `/auth/callback`, only imported `lib/auth.ts`, and that never
imported `lib/api.ts`. On that page, the first request went out with no base URL:
`/auth/config` instead of `/api/auth/config`. Locally this never showed, because there
sign-in is off and the callback page is never used.

**Mistake 2: failing open.** When the settings request failed, `initAuth` assumed sign-in
was switched off:

```ts
const { data } = await authConfig();
settings = data ?? { enabled: false };   // a failure became "auth is off"
```

So on the callback page:
1. The settings request got a 404, and `initAuth` concluded sign-in was **off**.
2. It skipped the step that saves your session from Cognito's code, and registered no
   interceptor.
3. The page sent you into the app, which rendered **without asking anyone to sign in**.
4. Every API call went without a token, and the API refused them all with 401.

**The real security boundary held.** The API checks the token on every request and
refused everything, so no data was exposed. But the web app's own check had failed open,
and it shouldn't.

### Fix

Commit `9f7ad53`:
1. **The setup is imported first.** [`lib/auth.ts`](../apps/web/src/lib/auth.ts) now
   imports `lib/api.ts` itself, so the base URL is set before any sign-in code runs,
   whichever page loads first.
2. **Failing closed.** If the settings can't be read, `initAuth` throws and caches
   nothing. The [sign-in gate](../apps/web/src/components/auth-gate.tsx) then shows
   "Kairos could not be reached" with a **Try again** button. Only the API explicitly
   answering `enabled: false` turns sign-in off.

   ```ts
   const { data } = await authConfig();
   if (!data) throw new Error("Could not load the sign-in settings from Kairos.");
   settings = data;
   ```
3. **Tests** ([`auth.test.ts`](../apps/web/src/lib/auth.test.ts)) check three things:
   - importing `lib/auth.ts` sets the base URL;
   - a failed settings request throws and caches nothing;
   - only an explicit `enabled: false` turns sign-in off.

After deploying, the same check gave the right answers: `/auth/callback` requested
`/api/auth/config` (200) and, without a real Cognito code, refused with "Sign-in failed"
instead of opening the app.

### Lessons

- **Fail closed.** When a security check can't get the information it needs, deny, never
  allow. The `?? { enabled: false }` fallback looked harmless, and it was the bug.
- **Don't depend on import order.** A module that needs setup should import that setup
  itself. Code that only works when some other file happened to load first breaks on the
  one page that loads first.
- **Test the production path.** Sign-in is switched off locally, so this flow never ran
  until the first real deploy. End-to-end tests against a local Cognito, or a staging
  stack, would have caught it earlier.
- **Specific error messages are worth it.** Three different 401 messages turned "it
  doesn't work" into "the header never arrived" in one `curl`.
- **Defence in depth works.** The UI check failed, and the API check stopped it.

---

## 5. After the fix, it still said "sign in required"

**Symptom.** Right after the fix was deployed, a question still came back with "sign in
required".

**Cause.** That browser tab had loaded the app **before** the fix and was still running
the old code. A deploy replaces what the server sends, but a page that is already open
keeps the code it loaded. In this case, the old code had also never saved a session, so
a fresh sign-in was needed too.

**How it was found.** The logs again: after the deploy, there was no new
`/api/auth/config` request from the user, though every fresh page load makes one. The
only new one was from the investigator's own test tab, which was adding token-less
requests of its own.

**Fix.** Reload the tab, then sign in once more.

**Lesson.** After a front-end fix, reload every open tab. When reading logs, account for
your own test traffic.

---

## 6. iCloud kept creating copies of files

**Symptom.** Hundreds of files like `stack 2.py`, `shell 2.tsx` and
`0001_runs 2.py` appeared, then 17,000 more inside `.venv` and `node_modules`. Tests ran
twice, imports failed (`ModuleNotFoundError`), and the type check failed on
`routes.d 2.ts`.

**Cause.** The repository lives in `~/Desktop`, which iCloud syncs. iCloud expects files
to change rarely and one at a time. A code project rewrites thousands of them a minute:
dependencies, build caches, git's own files. iCloud hits sync conflicts and keeps both
versions, as `name 2.ext`. It also marks some files as hidden, and Python 3.13 skips
hidden `.pth` files, which is what causes the `ModuleNotFoundError`.

**Fixes so far.**
- The copies were **moved, not deleted**, to `~/kairos-icloud-conflict-copies-2026-11-18`.
- `.venv` and `.next` were rebuilt; both are generated, so nothing was lost.
- `chflags -R nohidden .venv` clears the hidden flag, and `just sync` does it automatically.

**The lasting fix**, still to do: move the repository out of iCloud, for example to
`~/code/kairos`. (Desktop & Documents syncing can also be switched off in System
Settings → Apple Account → iCloud → iCloud Drive.)

**Lesson.** Keep code out of synced folders (iCloud, Dropbox, OneDrive). Git and GitHub
are the backup.

---

## The investigation toolkit used here

| Question | Tool |
|---|---|
| Is the app up? | `curl https://<app>/api/health` |
| Which side lost something? | `curl` with a deliberately bad input, then compare the error messages |
| What did the server see? | CloudWatch Logs: `aws logs filter-log-events --log-group-name <group> --filter-pattern '"HTTP/1.1"'` |
| What did the browser send? | The browser's network tab (or the built-in browser's request list) |
| Is the service healthy? | `aws ecs describe-services …` (running count, rollout state), and load balancer target health |
| What will a deploy change? | `just cdk-diff` (free) |
| Did a deploy pick up new secrets? | The task's startup log lines (e.g. "web search disabled" disappearing) |
