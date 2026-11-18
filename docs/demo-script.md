# Demo video script (about 3 minutes)

**Audience:** engineers and hiring managers.

**The story:** one person hands real chores to an agent and stays in control. Then the
camera shows the engineering underneath: durable runs, approvals, traces and evals.

**Cost:** nothing. The demo runs on the scripted model (`just api-demo`): answers are
hand-written, the tools are real, and nothing calls a paid API. Every take is identical,
so shots can be redone freely.

## Before filming

1. **Start fresh.** Drop the demo database; it is recreated on start:

   ```bash
   docker exec kairos-postgres-1 dropdb -U kairos --if-exists kairos_demo
   ```

2. **Start both servers** in two terminals:

   ```bash
   just api-demo
   ```

   ```bash
   just web
   ```

3. **Load eval results** into the demo database so `/evals` has something to show.
   This replays cassettes offline, so it is free:

   ```bash
   KAIROS_DATABASE_URL=postgresql+asyncpg://kairos:kairos@localhost:5432/kairos_demo just evals-report
   ```

4. **Prepare the browser.**
   - Use a fresh profile or a private window at 1440×900, in the light theme so it is
     easier to read on video. Pick it in the sidebar footer or with ⌘K → "Theme".
   - Hide the bookmarks bar and zoom to 110%.
   - If the tour has already been dismissed, clear site data so it shows again.
5. **For the phone shot**, use Chrome DevTools at iPhone 15 size, or install the PWA on
   a phone pointed at a deployed copy.

`next dev` shows a small "N" badge in the corner. For a clean recording, run
`pnpm --dir apps/web build && pnpm --dir apps/web start` instead of `just web`.

## Shot list

Times are cumulative. **VO** is the voice-over; *italics* are what happens on screen.

### 0:00 Cold open (10 s)

*The home page. The tour card slides in at step 1, "Ask in plain words"; click Next
twice, then "Open the setup checklist".*

**VO:** "This is Kairos, an agent that keeps working when I close the laptop: it
answers, watches and reminds, and it asks before it does anything on my behalf."

### 0:10 Setup at a glance (15 s)

*The Welcome page. Hover over the checklist: the model is on in demo mode; web search,
flights, email and push each say how to switch them on.*

**VO:** "A new install starts here. Each feature says whether it's on, and if not,
the one setting that turns it on. Secrets never reach the browser, only whether they
exist."

### 0:25 A plan you can watch (20 s)

*Click "Plan a three-step comparison of Postgres queue libraries." The plan checklist
appears, then the answer streams in.*

**VO:** "Answers stream in, with the agent's plan as a live checklist. Under each turn
are its cost and a link to its full trace."

### 0:45 Work that happens later (30 s)

*⌘K, type "new", press Enter. Click the fare-watch example. The "Proposed schedule"
card shows the next three run times. Click **Confirm**. Then ⌘K → "Tasks" to show it
active, next to the weekly digest.*

**VO:** "Ask for something recurring and it becomes a watch: every weekday at eight it
checks the fare and alerts me under seven hundred dollars. The agent can only propose
it; nothing runs until I confirm."

### 1:15 Asking before acting (35 s)

*New chat, then the email example. The run pauses with the exact arguments it wants to
send. Pause on that for a beat, then click **Approve**. The tool runs and the answer
says where the message went.*

**VO:** "Anything with a side effect waits for me, and I see exactly what it will send.
Under the hood the run is paused in Postgres, so it survives a restart or a deploy. When
I approve, it resumes from its checkpoint, and the send is keyed so a retry can never
send twice."

### 1:50 Memory, with consent (15 s)

*The memory example, then ⌘K → "Memory": the proposed fact is waiting; click Confirm.*

**VO:** "It remembers what matters, but only what I confirm."

### 2:05 The engineering underneath (40 s)

*Back to the email thread and click **Trace**: the step timeline with model and tool
spans, tokens and cost. Then `/evals`: per-layer scores, the trend chart, one failing
case opened.*

**VO:** "Every run is an OpenTelemetry trace. The same traces feed the eval framework:
ninety-five curated cases, recorded once against the real model and replayed offline
in CI, so a change that makes the agent worse fails the build. In production, every
run is scored too."

### 2:45 Anywhere (10 s)

*Phone-size view: the menu drawer, the ⌘K search button, then installing as an app.*

**VO:** "It works on a phone and installs like an app."

### 2:55 Close (5 s)

*The README's architecture diagram, then the repository URL.*

**VO:** "Kairos: Python, FastAPI and Postgres, with no agent framework, deployed on AWS
for about sixty dollars a month. The code and every design decision are in the repo."

## Notes

- **Re-recording the demo.** If you change a prompt or tool and the demo stops
  matching, run `just record-demo`; `tests/e2e/test_demo.py` fails until you do.
- **What not to film.** The demo model only knows the four example prompts. Typing
  anything else fails the run with a cassette miss, which is not a good look on camera.
- **Live footage instead.** To show a real model, run `just api` with an API key. The
  fare watch then also needs `SERPAPI_API_KEY`. Live answers vary from take to take
  and cost a few cents each.
