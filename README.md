# MWT Meeting Summary API (Cloud Run)

A small, self-contained service: receives a meeting recording, strips it to
audio-only, sends it to Gemini for accurate English/Malay/Arabic
transcription and summarisation, and returns structured JSON (summary, key
discussions, decisions, action items) for Power Automate to post into a
Teams channel.

Runs on **Google Cloud Run's free tier** (effectively $0/month at MWT's
meeting volume — see "Cost" below). No Apps Script, no personal Google
account dependency for the compute itself — just a container anyone with a
free Google Cloud account can deploy.

## Why Cloud Run and not [other option]

- **Cloudflare Workers (free)** can't do this — Workers Free caps CPU time
  at 10ms per request, nowhere near enough to run `ffmpeg`. Cloud Run gives
  a real container with real CPU/memory, so audio extraction just works.
- **Google Apps Script** was the original build, but ties the whole system
  to whichever Google account owns the script — an institutional
  hand-off problem once the person who set it up leaves. This version is
  plain Python in a Docker container, deployable under *any* Google
  account (or in principle, any container host at all).

## How it works

```
Staff right-clicks recording.mp4 in their own OneDrive/SharePoint folder
        |
        v
Power Automate flow: "For a selected file" (manual trigger)
        |
        +- Looks for a matching .vtt in the same folder
        |
        v
HTTP POST -> this Cloud Run service (/process)
        |
        +- ffmpeg strips video, keeps audio-only (shrinks file size a lot,
        |  and works around Power Automate/Cloud Run request-size limits)
        +- Uploads audio to Gemini's Files API
        +- Gemini produces: summary, key discussions, decisions, action items
        v
Returns JSON to Power Automate
        |
        v
Power Automate posts an Adaptive Card into the Teams channel
```

---

## Part 1 - Deploy to Cloud Run (~20 minutes, one-time)

You'll need a Google account (ideally one MWT controls long-term, e.g. a
shared admin account — not a personal one, for the same continuity reason
that motivated moving off Apps Script) and the `gcloud` CLI installed, or
you can deploy straight from the Cloud Console UI without any local setup.

### Option A - Deploy from the Cloud Console (no local tools needed)
1. Go to https://console.cloud.google.com → create a new project (e.g.
   "mwt-meeting-summary").
2. **Enable billing** on the project — required even for free-tier usage,
   but you will not be charged unless MWT's usage grows far beyond current
   meeting volume (see "Cost" below).
3. Go to **Cloud Run** → **Deploy container** → **Continuously deploy from
   a repository** (this connects to a GitHub repo containing this code —
   push this folder to a new GitHub repo first if you haven't).
4. Region: pick one close to Singapore (e.g. `asia-southeast1`).
5. Under **Container, Networking, Security → Variables & Secrets**, add
   environment variables:
   - `GEMINI_API_KEY` = your Gemini API key (see step 6 below)
   - `API_SECRET` = a random string you make up — this is the shared
     secret Power Automate must send with every request, so random
     internet traffic can't trigger paid Gemini calls against your key.
6. Get a Gemini API key at https://aistudio.google.com/apikey (use the
   same Google account as this Cloud Run project for simplicity). Enable
   billing on it too — paid tier is required for production use since the
   free tier lets Google use your inputs to improve their models, which
   isn't appropriate for internal meeting content. Paid tier costs
   roughly $0.037/minute of audio (~$3.30 for a 90-minute meeting).
7. **Deploy**. Cloud Run will build the container from the Dockerfile and
   give you a service URL like `https://mwt-meeting-summary-xxxx.a.run.app`.
8. Set **minimum instances to 0** (default) so it scales to zero and costs
   nothing when idle — this is already the default, just don't change it.
9. Set the **request timeout** to at least 600 seconds (Cloud Run's
   default is 300s, which may not be enough for a long recording's
   ffmpeg + Gemini processing time) — under the service's **Edit &
   Deploy New Revision → Container → Request timeout**.

### Option B - Deploy from the command line (if you have gcloud installed)
```bash
gcloud auth login
gcloud config set project YOUR_PROJECT_ID
gcloud run deploy mwt-meeting-summary \
  --source . \
  --region asia-southeast1 \
  --allow-unauthenticated \
  --timeout 600 \
  --set-env-vars GEMINI_API_KEY=your_key_here,API_SECRET=your_secret_here
```

### Quick sanity check
Visit the Cloud Run service URL in a browser — you should see:
```json
{"status":"ok","message":"MWT Meeting Summary API is running."}
```

---

## Part 2 - Power Automate flow (~20 minutes)

Same overall shape as before, pointed at the new Cloud Run URL instead of
an Apps Script Web App URL.

### Trigger
- **For a selected file** (OneDrive for Business or SharePoint) — shows up
  as a right-click option on any file, in any staff member's own folder.

### Step 1 - Get file content
- **Get file content**, using the trigger's file reference.

### Step 2 - Try to find the matching .vtt
- **Compose**: `replace(triggerOutputs()?['body/Name'], '.mp4', '.vtt')`
- **List files in folder** (same folder as the trigger file)
- **Condition**: does a file with that name exist in the listing?
  - **If yes:** **Get file content** for the .vtt
  - **If no:** continue with an empty transcript — summary/discussions/
    decisions/action items still generate, just without a speaker-labels
    confirmation flag

### Step 3 - Call the Cloud Run API
- **HTTP** action
  - Method: `POST`
  - URI: `https://mwt-meeting-summary-xxxx.a.run.app/process` (your actual
    Cloud Run URL + `/process`)
  - Headers: `Content-Type: application/json`
  - Body:
    ```json
    {
      "fileBase64": "@{base64(body('Get_file_content'))}",
      "fileName": "@{triggerOutputs()?['body/Name']}",
      "mimeType": "video/mp4",
      "vttText": "@{if(equals(outputs('Condition')?['status'], 'Skipped'), '', body('Get_file_content_2'))}",
      "meetingTitle": "@{triggerOutputs()?['body/Name']}",
      "apiSecret": "PASTE_YOUR_API_SECRET_HERE"
    }
    ```
  - As before, re-confirm the exact dynamic-content expressions against
    your actual flow's action names once built — Power Automate names
    them based on build order.

### Step 4 - Parse the response
- **Parse JSON**, schema:
  ```json
  {
    "type": "object",
    "properties": {
      "success": { "type": "boolean" },
      "title": { "type": "string" },
      "summary": { "type": "string" },
      "keyDiscussions": { "type": "array", "items": { "type": "string" } },
      "decisions": { "type": "array", "items": { "type": "string" } },
      "actionItems": { "type": "array", "items": { "type": "string" } },
      "hasSpeakerLabels": { "type": "boolean" },
      "error": { "type": "string" }
    }
  }
  ```

### Step 5 - Post to Teams
- **Post card in a chat or channel**, Adaptive Card body referencing
  `body('Parse_JSON')?['summary']`, `?['keyDiscussions']`, `?['decisions']`,
  `?['actionItems']` — same card JSON as documented in the previous
  version's spec.

### Step 6 - Handle failure
- **Condition**: `body('Parse_JSON')?['success']` equals `false` → post a
  short failure message (with `?['error']`) instead of silently doing
  nothing.

---

## Cost

At MWT's realistic volume (a handful of meetings a week):

- **Cloud Run**: $0. Free tier is 180,000 vCPU-seconds and 360,000
  GiB-seconds per month, and this allowance renews monthly and never
  expires. Even generously estimating 2 minutes of actual CPU time per
  meeting (ffmpeg + relay overhead) and 20 meetings/month, that's 2,400
  vCPU-seconds/month — about 1.3% of the free allowance.
- **Gemini API (paid tier)**: ~$0.037/minute of audio. A 90-minute meeting
  costs about $3.30; weekly meetings run roughly $13-15/month. This is the
  only real recurring cost in the whole system.

## Portability to other masjids

This is a plain Docker container with no MWT-specific configuration baked
into the code — everything masjid-specific (Gemini key, secret, meeting
title formatting) is either an environment variable or safely genericised
in the prompt already. To stand this up for another masjid:

1. They (or whoever supports them technically) push this same repo to
   their own GitHub, or just copy the folder.
2. They create their own free Google Cloud project and their own Gemini
   API key — nobody inherits MWT's or your personal account.
3. Deploy following Part 1 above, ~20 minutes.
4. They build their own Power Automate flow following Part 2, pointing at
   their own Cloud Run URL.

No code changes needed for a different organisation's name/context beyond
optionally editing the prompt string in `app/main.py`'s `_gemini_summarize`
function, which currently says "Islamic education organisation in
Singapore" — a different org would want to adjust that framing sentence,
nothing else.

## Known limits

- **No chunking** — a single Gemini request per recording. Should handle
  meetings up to a few hours based on Gemini's audio limits, but hasn't
  been stress-tested past ~1.5-2hrs against Cloud Run's request timeout
  (currently set to 600s in the deploy steps above — increase if a real
  test shows longer recordings need more time for ffmpeg + Gemini
  processing combined).
- **Request size** — the recording arrives as base64 JSON in Power
  Automate's HTTP action body, before audio extraction happens server-side
  (extraction can't happen before upload, since it needs Cloud Run's CPU).
  This means the *original* video file's size is still what matters for
  Power Automate's own outbound request limits. If a raw recording is
  large enough to be rejected before it even reaches this service, the
  fallback is either a client-side audio-only conversion step before
  upload (adds a manual step for staff), or splitting delivery across
  multiple smaller HTTP calls — not built here, flagged for testing first.
- **Full transcript is not returned** in this version, only the
  summary/discussions/decisions/action items — matching the simplified
  Teams-channel-card scope. The Gemini call could be extended to also
  return full timestamped segments if a future version wants to save a
  complete transcript alongside the short-form card.
- **Speaker names aren't woven into the summary** — the .vtt is only
  checked for the presence of speaker tags (`hasSpeakerLabels`), not used
  to attribute individual discussion points, since Gemini already
  attributes points to people where they're named in the audio itself.
