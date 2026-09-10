# MWT Meeting Summary API (v3 - Upload Page + Teams Webhook)

Staff visit a simple web page, upload their meeting recording, and a
summary (key discussions, decisions, action items) posts automatically
into a Teams channel a few minutes later. No folder-hunting, no separate
transcript file, no Power Automate form with fiddly file-picker fields.

This is the third iteration of this system - see the version history note
at the bottom for why earlier designs (Apps Script, "For a selected file"
trigger, base64-through-Power-Automate) were set aside.

## How it works

```
Staff visit the Cloud Run URL, upload recording.mp4 + type a passphrase
        |
        v
Cloud Run (this service)
        |
        +- Streams the upload straight to disk (not base64/JSON - that
        |  would inflate a 238MB file to ~317MB for no reason)
        +- ffmpeg strips video, keeps audio-only (huge size reduction)
        +- Uploads audio to Gemini's Files API
        +- Gemini produces: summary, key discussions, decisions, action items
        v
Cloud Run POSTs the small JSON result to a Power Automate
"When a HTTP request is received" flow
        |
        v
Power Automate posts an Adaptive Card into the Teams channel
```

**The key design point**: Power Automate never touches the actual
recording. It only ever receives a small JSON summary at the very end -
this is what avoids Power Automate's hard limits (100MB request body cap,
120-second timeout) entirely, since those limits made the earlier
"send the whole file through Power Automate" designs impossible for
MWT's real recording sizes (200MB+ for a 30-minute meeting).

---

## Part 1 - Deploy to Cloud Run

(You've already done the Google Cloud account + billing + initial
deployment setup from the earlier version - this is an update to that
same service, not a fresh setup. If starting fresh, see "First-time setup"
below.)

### Updating an existing deployment
1. Push this folder's contents to the same GitHub repo Cloud Run is
   watching, replacing the old files.
2. Cloud Run's continuous deployment trigger will automatically rebuild
   and redeploy - or trigger it manually from the Cloud Run console if
   auto-deploy isn't set up.
3. **Add one new environment variable** (Cloud Run console -> your service
   -> Edit & Deploy New Revision -> Variables & Secrets):
   - `POWER_AUTOMATE_WEBHOOK_URL` = the URL from Part 2 below (you'll get
     this once you build the Power Automate flow - come back and fill
     this in after Part 2)
4. **Rename/add** the passphrase variable:
   - `UPLOAD_PASSPHRASE` = a simple word or phrase staff will type on the
     upload form (e.g. pin this in the Teams channel description so
     staff can find it). This replaces the old `API_SECRET` - the old one
     protected a machine-to-machine API call; this one protects a
     human-facing form, so it needs to be something a person can type.
5. You can remove `API_SECRET` if it's still set from the earlier
   version - it's no longer used by this version of the code.
6. Optionally set `REQUIRED_EMAIL_DOMAIN` - defaults to `waktanjong.org`
   if not set, so you only need this if MWT's domain ever changes.
7. Keep `GEMINI_API_KEY` as-is (rotate it first if you haven't since it
   was accidentally shown in a screenshot earlier).

### First-time setup (if deploying fresh)
1. Google Cloud Console -> new project -> **enable billing**.
2. Cloud Run -> **Deploy container** -> **Continuously deploy from a
   repository** -> connect this repo.
3. Region: `asia-southeast1` (closest to Singapore).
4. **Authentication: Allow public access** (this is correct and
   intentional - the `UPLOAD_PASSPHRASE` check inside the app is what
   actually protects it, not Cloud Run's own auth gate; Cloud Run auth
   would block staff's browsers too, since they don't have Google
   identity tokens).
5. **Billing: Request-based** (scales to zero between uses, free at
   MWT's volume).
6. Environment variables (Variables & Secrets tab):
   - `GEMINI_API_KEY` = your Gemini API key
   - `UPLOAD_PASSPHRASE` = a simple shared phrase for staff
   - `POWER_AUTOMATE_WEBHOOK_URL` = fill in after building Part 2
7. **Container -> Settings -> Request timeout**: set to `600` seconds
   (default 300s may not be enough for a long recording).
8. Deploy. Copy the service URL - this is what you'll share with staff.

### Quick sanity check
Visit `https://your-service-url.a.run.app/health` - should show:
```json
{"status":"ok","message":"MWT Meeting Summary API is running."}
```
Visit the root URL (`https://your-service-url.a.run.app/`) - should show
the upload form.

---

## Part 2 - Power Automate flow (receiving side)

This flow does the opposite of earlier versions: instead of *sending* a
file, it *receives* a small JSON result from Cloud Run and posts it to
Teams.

### Step 1: Create the flow
1. **make.powerautomate.com** -> **Create** -> **Instant cloud flow**
2. Name: `MWT Meeting Summary - Post to Teams`
3. Trigger: search for and select **"When a HTTP request is received"**
4. **Create**

### Step 2: Configure the trigger
1. On the trigger card, click **"Use sample payload to generate schema"**
   and paste this:
   ```json
   {
     "success": true,
     "title": "Weekly Sync",
     "staffEmail": "hafizuddin@waktanjong.org",
     "summary": "The team discussed...",
     "keyDiscussions": ["Point one", "Point two"],
     "decisions": ["Decision one"],
     "actionItems": ["Action one"],
     "error": ""
   }
   ```
2. Power Automate will generate the JSON schema automatically from this
   sample - this is what lets you reference `title`, `summary`,
   `staffEmail`, etc. as dynamic content later without a separate Parse
   JSON step.
3. **Save the flow once** (even without adding more steps yet) - this
   generates the actual webhook URL, shown at the top of the trigger card
   as **"HTTP POST URL"**. Copy this.
4. **Go back to Cloud Run** and paste this URL into the
   `POWER_AUTOMATE_WEBHOOK_URL` environment variable (Part 1 above), then
   redeploy the Cloud Run revision so it picks up the new variable.

### Step 3: Post to the staff member's personal chat with Flow bot
**+ New step** -> **"Post message in a chat or channel"** (Microsoft Teams connector)
- Post as: **Flow bot**
- Post in: **Chat with Flow bot**
- Recipient: click into this field and insert the dynamic content
  `staffEmail` (from the trigger) - this addresses the message directly
  to whoever uploaded the recording, using the email they typed on the
  upload form.
- Message: build the summary text using dynamic content, e.g.:
  ```
  📝 Meeting Summary: @{triggerBody()?['title']}

  @{triggerBody()?['summary']}

  Key Discussions:
  - @{join(triggerBody()?['keyDiscussions'], '\n- ')}

  Decisions Made:
  - @{join(triggerBody()?['decisions'], '\n- ')}

  Action Items:
  - @{join(triggerBody()?['actionItems'], '\n- ')}
  ```
  (Plain text works fine here since "Post message" doesn't render
  Adaptive Cards the way "Post card in a chat or channel" does - if you
  want the nicer card layout instead, swap this step for **"Post card in
  a chat or channel"** with **Post in: Chat with Flow bot** and
  **Recipient: staffEmail**, using the same Adaptive Card JSON structure
  from the channel-posting version of this project.)

**Note on staffEmail validation**: Cloud Run already rejects any upload
where the email doesn't end in `@waktanjong.org` before any processing
happens (see `_validate_staff_email` in `app/main.py`), so by the time
Power Automate receives this webhook call, `staffEmail` is guaranteed to
be a real MWT address - safe to use directly as the chat recipient
without extra validation in the flow itself.

### Step 4: Handle failure
**+ New step** -> **Condition**
- Left: dynamic content -> `success` (from the trigger)
- Operator: **is equal to**
- Right: `false`
- **If yes:** **"Post message in a chat or channel"** -> **Chat with Flow
  bot** -> Recipient: `staffEmail` -> text:
  `Meeting summary failed: @{triggerBody()?['error']}` - this tells the
  specific staff member their upload failed, rather than only logging it
  somewhere no one checks.

### Step 5: Respond to Cloud Run (recommended)
Cloud Run's forwarding call waits up to 30 seconds for a response from
this webhook. Add a **"Response"** action (Request connector) at the end
returning a simple `200 OK` - without this, Power Automate's default
response can be slow enough to occasionally cause Cloud Run's forwarding
call to time out (the Teams post itself would likely still succeed, but
Cloud Run's own logs would show a spurious error). Response body:
`{"status": "received"}`, status code `200`.

---

## Part 3 - Test end to end

1. Visit the Cloud Run URL.
2. Enter the passphrase, pick a real recording, submit.
3. Watch the progress bar - large files take a while to upload depending
   on the connection; this is normal and expected.
4. Once upload finishes, the page shows "transcribing and summarising" -
   this can take several minutes for a long recording (ffmpeg extraction
   + Gemini processing time combined).
5. Check the Teams channel for the posted card.
6. If it fails, check:
   - **Cloud Run logs** (Console -> your service -> Logs) - most issues
     (ffmpeg errors, Gemini API errors) will show clearly here
   - **Power Automate run history** - confirms whether Cloud Run's
     forwarded JSON actually reached the flow, and whether the Teams
     post itself succeeded

---

## Known limits

- **No chunking** - a single Gemini request per recording. Untested past
  ~1.5-2hrs of audio against Cloud Run's request timeout.
- **Upload time depends on staff's connection** - a 200MB+ file over a
  slow connection could take several minutes just to upload before
  processing even starts. The progress bar keeps staff informed, but
  there's no way around physics here without asking staff to pre-shrink
  files (which was deliberately ruled out to keep this a true one-step,
  zero-effort process).
- **Passphrase is basic protection, not real security** - anyone with the
  passphrase can trigger a paid Gemini call. Fine for an internal tool
  shared within a small organisation; if this becomes a problem, a
  proper login system would be the next step, but wasn't judged worth
  the added complexity for MWT's scale and timeline.
- **No download option in this version** - staff never see or download
  anything from the upload page itself; the only output is the Teams
  post. If someone needs the raw text later, it currently only exists in
  the Teams channel history and inside the Gemini/Cloud Run logs
  transiently, not saved anywhere durable. Worth flagging as a gap if
  MWT wants a searchable archive of past summaries later.

## Version history (for institutional record)

Earlier designs were tried and set aside - documented here so a future
maintainer understands why, rather than re-discovering the same dead ends:

1. **Google Apps Script Web App** (staff upload form -> downloadable .md)
   - worked, but tied the whole system to a personal Google account,
   creating a handover problem when the builder leaves the role.
2. **Apps Script as a pure JSON API, called by Power Automate's
   "For a selected file" trigger** - solved the account-portability
   concern partially (moved to Cloud Run later) but assumed recordings
   and their .vtt transcripts sit in a predictable, watchable folder.
   In practice, recordings live in each staff member's own OneDrive (or
   a channel's SharePoint library if the meeting was held in a channel),
   and the "Transcript" file next to a recording turned out to sometimes
   be another small .mp4, not a .vtt - the actual .vtt has to be
   downloaded separately from within Teams/Stream. This made automatic,
   no-staff-effort triggering unreliable.
3. **Sending the recording through Power Automate's HTTP action** (either
   to Apps Script or Cloud Run) - hit two hard Microsoft-side limits:
   Power Automate's HTTP action caps around 100MB request bodies and
   times out at 120 seconds for synchronous calls, neither of which is
   configurable. MWT's real recordings (200MB+ for a 30-minute meeting,
   since screen-share video inflates file size heavily) don't fit either
   constraint.
4. **This version** - moves the file upload *out* of Power Automate
   entirely. Staff upload directly to Cloud Run (which has no such size
   limit for a real container), and Power Automate only ever handles the
   tiny JSON result at the end. Speaker labels were also dropped in this
   version, since reliably obtaining the .vtt turned out not to be
   possible without adding a manual step - and adding staff steps was
   explicitly ruled out as counter to the goal of zero-effort adoption.
5. **Teams tab embedding was attempted** via the generic "Website" tab
   type, but Teams kept opening it in an external browser window instead
   of a true embedded iframe, even after adding a permissive
   Content-Security-Policy header. A full custom Teams app manifest would
   likely fix this, but wasn't pursued given the timeline - the page
   works fine as a plain link shared in the channel instead.
6. **Personal chat delivery was attempted via a Power App Teams-tab
   front-end** (using `User().Email` for automatic identity), which would
   have solved both the tab-embedding and per-user-identity problems at
   once. Set aside because it would have needed a Custom Connector to
   avoid re-introducing Power Automate's 100MB/120s file-size wall, on
   top of an app manifest - too much new surface area for the remaining
   timeline. **Resolved instead with a simple required email field** on
   the existing HTML form (validated both client-side and server-side
   against the `@waktanjong.org` domain), passed through Cloud Run to
   Power Automate, which uses it to post the summary directly to that
   person's chat with Flow bot.
