# MWT Meeting Summary API (v4 - Direct-to-Storage Upload + Teams Webhook)

Staff visit a simple web page, upload their meeting recording, and a
summary (key points grouped by topic, plus action items) is sent
automatically
to their Teams chat with Flow bot a few minutes later. No folder-hunting,
no separate transcript file, no Power Automate form with fiddly
file-picker fields.

This is the fourth iteration of this system - see the version history
note at the bottom for why earlier designs (Apps Script, "For a selected
file" trigger, uploading straight to Cloud Run) were set aside. The
short version: **Cloud Run has a hard, non-configurable 32MB request size
limit**, enforced at Google's own front-end load balancer before your
code ever runs - confirmed by direct testing (a `curl` upload of a 250MB
file returned `413 Request Entity Too Large` from `Google Frontend`
itself, with zero corresponding entry in the container's own logs, since
the container was never involved). No memory increase, timeout increase,
or code change can fix this - it's a platform-level ceiling. This version
routes large files around Cloud Run entirely using Cloud Storage.

## How it works

```
Staff visit the Cloud Run URL, fill in email + passphrase, pick recording.mp4
        |
        v
1. Browser asks Cloud Run for a short-lived signed upload URL (tiny
   request - just filename + email, well under any size limit)
        |
        v
2. Browser uploads the recording DIRECTLY to Google Cloud Storage using
   that signed URL - Cloud Run is NOT in this path at all, so its 32MB
   request limit never applies, regardless of file size
        |
        v
3. Browser tells Cloud Run "the file is at gs://bucket/xxx.mp4, go"
   (another tiny request - just an object name)
        |
        v
Cloud Run (this service)
        |
        +- Downloads the file from GCS server-to-server (no 32MB limit
        |  on server-to-server GCS reads)
        +- ffmpeg strips video, keeps audio-only (huge size reduction)
        +- Uploads audio to Gemini's Files API
        +- Gemini produces: key points grouped by topic, action items
        +- Deletes the GCS object once done (success or failure)
        v
Cloud Run POSTs the small JSON result to a Power Automate
"When a HTTP request is received" flow
        |
        v
Power Automate posts the summary to the staff member's personal chat
with Flow bot
```

**The key design point**: neither Power Automate NOR Cloud Run's own
front end ever touches the actual recording as a direct HTTP body. Power
Automate only ever receives a small JSON summary at the very end (avoids
its 100MB/120s HTTP limits); Cloud Run only ever receives the large file
via a server-to-server GCS download, never as an inbound HTTP request
body (avoids Cloud Run's 32MB limit). Both of MWT's real hard blockers
are sidestepped this way, for recordings of any realistic size.

---

## Part 1 - Deploy to Cloud Run

(You've already done the Google Cloud account + billing + initial
deployment setup from the earlier version - this is an update to that
same service, not a fresh setup. If starting fresh, see "First-time setup"
below.)

### New in this version: create a Cloud Storage bucket

This version needs one new piece of infrastructure - a GCS bucket to
receive large uploads before Cloud Run processes them.

1. Cloud Console -> **Cloud Storage** -> **Buckets** -> **Create**.
2. Name it something like `mwt-meeting-summary-uploads` (bucket names
   are globally unique across all of Google Cloud, so add a distinguishing
   prefix if that exact name is taken).
3. Region: same as your Cloud Run service (`asia-southeast1`), so
   downloads between them are fast and don't cross regions.
4. Storage class: **Standard**.
5. Access control: **Uniform** (the default) is fine.
6. **Public access prevention: leave this ON** (the default) - nothing in
   this bucket should be publicly readable; access happens only via the
   short-lived signed URLs this service generates, and via the service's
   own server-to-server downloads.
7. Create the bucket, then copy its exact name for the environment
   variable below.

**Grant the Cloud Run service account permission to sign URLs.**
Generating a signed upload URL requires a specific IAM permission
(`iam.serviceAccounts.signBlob`) that isn't included by default, even for
a service that otherwise has full access to its own bucket. This is a
well-documented Cloud Run + GCS papercut (Cloud Run's default credentials
are a bare token with no private key attached, so signing has to be
routed through the IAM API instead - the code already handles this, but
it needs these two things enabled/granted first):

1. **Enable the Service Account Credentials API** on the project - Cloud
   Console -> **APIs & Services** -> **Library** -> search "IAM Service
   Account Credentials API" -> **Enable**. Without this, signing fails
   even with the correct IAM role granted below.
2. Cloud Console -> **IAM & Admin** -> find the service account Cloud Run
   is running as (usually `PROJECT_NUMBER-compute@developer.gserviceaccount.com`,
   visible on your Cloud Run service's details page under "Security" or
   "Service account").
3. Grant it the **Storage Admin** role on the bucket (or at minimum
   **Storage Object Admin**, scoped to just this bucket, for tighter
   permissions) - this covers both the signed-URL generation and the
   service's own reads/deletes.
4. Also grant it the **Service Account Token Creator** role (on itself) -
   this is specifically what allows the IAM signBlob-based signing to
   work from within Cloud Run; without it, signed URL generation fails
   with a permissions error even though the bucket access itself is fine.

### Updating an existing deployment
1. Push this folder's contents to the same GitHub repo Cloud Run is
   watching, replacing the old files.
2. Cloud Run's continuous deployment trigger will automatically rebuild
   and redeploy - or trigger it manually from the Cloud Run console if
   auto-deploy isn't set up.
3. **Add these environment variables** (Cloud Run console -> your service
   -> Edit & Deploy New Revision -> Variables & Secrets):
   - `GCS_BUCKET_NAME` = the bucket name from the step above (new in this
     version - required, the service won't work without it)
   - `POWER_AUTOMATE_WEBHOOK_URL` = the URL from Part 2 below (you'll get
     this once you build the Power Automate flow - come back and fill
     this in after Part 2, if not already set from before)
4. **Memory**: if you haven't already, raise this to **2 GiB** under
   Containers -> Settings -> Resources (the default 512 MiB was enough to
   OOM-kill the container on a large file before the GCS fix, and while
   this version downloads more carefully, there's no reason to run it
   tight - 2 GiB is cheap at this usage volume).
5. Keep `UPLOAD_PASSPHRASE`, `GEMINI_API_KEY`, `REQUIRED_EMAIL_DOMAIN`
   as already configured from the previous version.

### First-time setup (if deploying fresh)
1. Google Cloud Console -> new project -> **enable billing**.
2. Create the GCS bucket and grant IAM permissions per the steps above.
3. Cloud Run -> **Deploy container** -> **Continuously deploy from a
   repository** -> connect this repo.
4. Region: `asia-southeast1` (closest to Singapore, and matching the
   bucket's region).
5. **Authentication: Allow public access** (this is correct and
   intentional - the `UPLOAD_PASSPHRASE` check inside the app is what
   actually protects it, not Cloud Run's own auth gate; Cloud Run auth
   would block staff's browsers too, since they don't have Google
   identity tokens).
6. **Billing: Request-based** (scales to zero between uses, free at
   MWT's volume).
7. **Memory: 2 GiB** (Containers -> Settings -> Resources).
8. Environment variables (Variables & Secrets tab):
   - `GEMINI_API_KEY` = your Gemini API key
   - `UPLOAD_PASSPHRASE` = a simple shared phrase for staff
   - `GCS_BUCKET_NAME` = your bucket name from above
   - `POWER_AUTOMATE_WEBHOOK_URL` = fill in after building Part 2
9. **Container -> Settings -> Request timeout**: set to `600` seconds
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

**Note on the data shape**: `keyPoints` is a nested structure - an array
of `{topic, points}` objects, one per topic Gemini identified, each with
its own list of bullet points underneath. This is different from a flat
array of strings, so it can't be turned into text with a single `join()`
expression the way `actionItems` can. Building the message needs a loop
(Apply to each) that appends each topic's heading and bullets into a
running string as it goes. This is more steps than a typical Power
Automate flow, but it's the only way to render nested, grouped data as
formatted text.

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
     "error": "",
     "keyPoints": [
       {
         "topic": "Quick reply revamp",
         "points": ["Point one", "Point two"]
       }
     ],
     "actionItems": ["Action one"]
   }
   ```
2. Power Automate will generate the JSON schema automatically from this
   sample - this is what lets you reference `title`, `staffEmail`,
   `keyPoints`, etc. as dynamic content later without a separate Parse
   JSON step, and lets the `keyPoints` array be looped over correctly.
3. **Save the flow once** (even without adding more steps yet) - this
   generates the actual webhook URL, shown at the top of the trigger card
   as **"HTTP POST URL"**. Copy this.
4. **Go back to Cloud Run** and paste this URL into the
   `POWER_AUTOMATE_WEBHOOK_URL` environment variable (Part 1 above), then
   redeploy the Cloud Run revision so it picks up the new variable.

### Step 3: Build the Key Points section as formatted text

1. **+ New step** -> **Initialize variable** (Variable connector)
   - Name: `KeyPointsHtml`
   - Type: **String**
   - Value: leave empty - it gets built up inside the loop below

2. **+ New step** -> **Apply to each** (Control connector)
   - Select an output from previous steps -> pick `keyPoints` (from the
     trigger's dynamic content)

3. **Inside the Apply to each loop** -> **+ Add an action** -> **Append to
   string variable** (Variable connector)
   - Name: `KeyPointsHtml`
   - Value: click the expression editor (fx) and enter:
     ```
     concat('<b>', item()?['topic'], '</b><br>- ', join(item()?['points'], concat('<br>', '- ')), '<br><br>')
     ```
   - This appends, for each topic: a bolded heading, a bulleted list of
     its points (using `<br>` for line breaks since the message body
     renders HTML - a plain `\n` gets silently collapsed by Teams' rich
     text renderer, a known quirk this project hit earlier), then a
     blank line before the next topic.

### Step 4: Post to the staff member's personal chat with Flow bot

**+ New step** (after the Apply to each loop closes) -> **"Post message
in a chat or channel"** (Microsoft Teams connector)
- Post as: **Flow bot**
- Post in: **Chat with Flow bot**
- Recipient: click into this field and insert the dynamic content
  `staffEmail` (from the trigger) - this addresses the message directly
  to whoever uploaded the recording, using the email they typed on the
  upload form.
- Message: switch the message editor to HTML view (the `</>` icon in the
  toolbar) and build:
  ```
  📝 Meeting Summary: @{triggerBody()?['title']}
  <br><br>
  Key Points
  <br><br>
  @{variables('KeyPointsHtml')}
  <br>
  Action Items:
  <br>- @{join(triggerBody()?['actionItems'], concat('<br>', '- '))}
  ```
  (Reference `variables('KeyPointsHtml')` - the string built by the loop
  in Step 3 - rather than trying to reference `keyPoints` directly here,
  since the trigger's raw `keyPoints` is still the unrendered nested
  array at this point in the flow.)

**Note on staffEmail validation**: Cloud Run already rejects any upload
where the email doesn't end in `@waktanjong.org` before any processing
happens (see `_validate_staff_email` in `app/main.py`), so by the time
Power Automate receives this webhook call, `staffEmail` is guaranteed to
be a real MWT address - safe to use directly as the chat recipient
without extra validation in the flow itself.

### Step 5: Handle failure
**+ New step** -> **Condition**
- Left: dynamic content -> `success` (from the trigger)
- Operator: **is equal to**
- Right: `false`
- **If yes:** **"Post message in a chat or channel"** -> **Chat with Flow
  bot** -> Recipient: `staffEmail` -> text:
  `Meeting summary failed: @{triggerBody()?['error']}` - this tells the
  specific staff member their upload failed, rather than only logging it
  somewhere no one checks.

### Step 6: Respond to Cloud Run (recommended)
Cloud Run's forwarding call waits up to 30 seconds for a response from
this webhook. Add a **"Response"** action (Request connector) at the end
returning a simple `200 OK` - without this, Power Automate's default
response can be slow enough to occasionally cause Cloud Run's forwarding
call to time out (the Teams post itself would likely still succeed, but
Cloud Run's own logs would show a spurious error). Response body:
`{"status": "received"}`, status code `200`.

### Flow structure summary

For reference, the finished flow should run in this order:
```
1. When a HTTP request is received  (trigger)
2. Initialize variable: KeyPointsHtml (empty string)
3. Apply to each: keyPoints
     -> Append to string variable: KeyPointsHtml
4. Condition: success = false?
     -> Yes: Post failure message to staffEmail's chat
     -> No: (continue to step 5)
5. Post message in a chat or channel (the actual summary, to staffEmail)
6. Response (200 OK, back to Cloud Run)
```
Steps 4's two branches both eventually need the flow to end cleanly;
the simplest arrangement is to put step 5 (the real summary post) in the
Condition's "No" branch, and leave the "Yes" branch with just the failure
message, so only one message is ever sent per run.

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
  the added complexity for MWT's scale and timeline. The signed GCS
  upload URL is separately short-lived (30 minutes) and scoped to one
  specific object, so it can't be reused or guessed even if intercepted.
- **Orphaned GCS objects on rare failure paths** - the object is deleted
  in a `finally` block after processing, which covers the normal
  success/failure cases, but if Cloud Run itself crashes hard (e.g. an
  OOM kill) between download and cleanup, the uploaded object could be
  left in the bucket. Not cleaned up automatically in this version; worth
  periodically checking the bucket's `uploads/` folder isn't
  accumulating stale files, or adding a GCS lifecycle rule (delete
  objects older than 1 day) as a backstop if this becomes a real issue.
- **No download option in this version** - staff never see or download
  anything from the upload page itself; the only output is the Teams
  post. If someone needs the raw text later, it currently only exists in
  the Teams channel history and inside the Gemini/Cloud Run logs
  transiently, not saved anywhere durable. Worth flagging as a gap if
  MWT wants a searchable archive of past summaries later.
- **Cost reduction not yet applied by default** - `GEMINI_MODEL` can be
  set to `gemini-flash-lite-latest` (cheaper per token than the default
  `gemini-flash-latest`) to cut ongoing Gemini spend further. Left as an
  opt-in env var rather than the default, since Flash-Lite's summary
  quality on MWT's specific English/Malay/Arabic code-switching hasn't
  been validated yet - test it against a real recording before switching
  permanently.

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
7. **Uploading directly to Cloud Run (v3) worked for a 20MB test file but
   failed silently on every real recording (200MB+), with zero server
   logs for the failed requests.** Initial hypotheses - insufficient
   container memory (raised 512 MiB -> 2 GiB), an intermittent Gemini
   model 404 (switched to the `gemini-flash-latest` alias), a malformed
   Power Automate payload on the failure path (fixed to always send a
   consistent field shape) - were all real bugs worth fixing, but none
   of them explained the silent failure on large files, since the
   request was never reaching the container at all. A direct `curl`
   test from Cloud Shell (bypassing the browser and local network as
   variables) confirmed the actual cause: **Cloud Run enforces a hard,
   non-configurable 32MB request size limit at Google's own front-end
   load balancer**, returning `413 Request Entity Too Large` before the
   container is even invoked - which is exactly why nothing ever showed
   up in the container's logs. No setting in Cloud Run's console
   (memory, timeout, concurrency) affects this; it's a platform-level
   ceiling documented independently across several unrelated sources.
   **This version (v4) fixes it properly**: the browser uploads the
   large file directly to a Cloud Storage bucket using a short-lived
   signed URL (GCS has no such limit), and Cloud Run only ever receives
   a tiny "the file is at this path, go" request - well under 32MB
   regardless of how large the actual recording is. Cloud Run then pulls
   the file from GCS server-to-server, where the 32MB limit doesn't
   apply either.
8. **Feedback from a real user (Nazlin) after the first live meeting
   summary: the separate "Key Discussions" and "Decisions Made" sections
   repeated the same points twice and were hard to follow.** Merged into
   a single `keyPoints` section, grouped by topic (Gemini infers topic
   names freely from what was actually discussed) with a bolded heading
   per topic and point-form bullets underneath - matching the "Key
   Points" style the org already uses elsewhere. This changed the
   response shape from flat `keyDiscussions`/`decisions`/`summary`
   fields to a single nested `keyPoints: [{topic, points}]` array, which
   in turn required reworking the Power Automate message-building step
   from simple `join()` expressions into an Initialize Variable + Apply
   to Each + Append to String Variable loop, since Power Automate can't
   flatten nested arrays into formatted text with a one-line expression.
