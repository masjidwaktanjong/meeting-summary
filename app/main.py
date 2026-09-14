"""
MWT Meeting Summary API (Cloud Run)
-----------------------------------
Staff visit this service's web page, upload a meeting recording directly
from their browser (multipart file upload — NOT base64/JSON, which would
inflate a 238MB file to ~317MB and hit request-size limits sooner), and
the service:
  1. Strips the recording to audio-only using ffmpeg (real CPU work —
     this is why Cloud Run is used instead of an edge/Workers platform)
  2. Uploads the audio to Gemini's Files API
  3. Asks Gemini for a structured summary: key discussions, decisions,
     action items
  4. Forwards that small JSON result to a Power Automate "When a HTTP
     request is received" flow, which posts it into a Teams channel

Power Automate never sees the actual recording — only the small JSON
summary at the very end — which avoids Power Automate's HTTP action
limits (100MB body cap, 120-second timeout) entirely, since those don't
apply to the tiny outbound payload.

Portable by design: this whole service is a plain Flask app in a Docker
container. Any masjid (or MWT after a staff handover) can redeploy it
under their own free Google Cloud Run project — nothing here is tied to
a specific Google account. See README.md for deploy steps.
"""

import json
import logging
import os
import re
import subprocess
import tempfile
import time

import requests
from flask import Flask, jsonify, render_template, request

app = Flask(__name__)
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
POWER_AUTOMATE_WEBHOOK_URL = os.environ.get("POWER_AUTOMATE_WEBHOOK_URL")  # where results get posted
# Gemini 2.5 models are being retired (shutdown announced for Oct 2026) and
# were returning intermittent 404s on generateContent well before that date
# — a known, widely-reported Google-side issue as the model family winds
# down (confirmed present even for models still listed as available via
# ListModels for this account — see /debug/models). Using the "-latest"
# alias instead of a pinned model name specifically to avoid this problem
# recurring: Google moves this alias forward as models are deprecated, so
# it shouldn't need manual updating again the way a pinned name does.
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-flash-latest")
GEMINI_BASE = "https://generativelanguage.googleapis.com"

# Simple shared passphrase, entered on the upload form itself, so a
# stranger who finds the URL can't trigger paid Gemini calls. Not meant to
# be strong security — just enough friction that only staff who were given
# the passphrase (e.g. pinned in the Teams channel) can use it. Set this
# in Cloud Run's environment variables; leave unset to disable the check
# entirely (not recommended once this is shared beyond initial testing).
UPLOAD_PASSPHRASE = os.environ.get("UPLOAD_PASSPHRASE")

# Staff must submit their own @waktanjong.org email so the meeting summary
# can be posted to their personal chat with Flow bot (Power Automate uses
# this to address the "Post message in a chat or channel" action at that
# specific person, rather than only posting to a shared channel).
REQUIRED_EMAIL_DOMAIN = os.environ.get("REQUIRED_EMAIL_DOMAIN", "waktanjong.org")

# Cloud Run itself supports request bodies up to 32MB by default on the
# older gen1 execution environment, but gen2 (the current default for new
# services) supports considerably larger streamed uploads — multipart
# file uploads are streamed to disk rather than buffered fully in memory,
# so this is not the same hard wall the old base64-JSON approach hit.
# Real-world ceiling is more likely to be upload time over a slow
# connection than a hard size rejection. See README "Known limits".


@app.after_request
def allow_teams_embedding(response):
    """
    By default, Flask doesn't set any framing headers, which can leave
    Teams uncertain whether it's safe to embed this page in a tab's
    iframe — Teams falls back to opening the page in an external browser
    window instead. Explicitly allowing Teams' own domains as frame
    ancestors fixes this. (Deliberately not touching X-Frame-Options here
    — it's the older, single-origin mechanism and is superseded by CSP
    frame-ancestors for this purpose; setting both can conflict.)
    """
    response.headers["Content-Security-Policy"] = (
        "frame-ancestors 'self' https://teams.microsoft.com "
        "https://*.teams.microsoft.com https://*.skype.com "
        "https://teams.microsoft.us https://*.teams.microsoft.us "
        "https://*.cloud.microsoft"
    )
    return response


@app.route("/", methods=["GET"])
def upload_page():
    """Serves the staff-facing upload page."""
    return render_template("upload.html")


@app.route("/health", methods=["GET"])
def health():
    """Simple health check / sanity endpoint."""
    return jsonify({"status": "ok", "message": "MWT Meeting Summary API is running."})


@app.route("/debug/models", methods=["GET"])
def debug_list_models():
    """
    Diagnostic endpoint: lists models this Gemini API key actually has
    access to, and which support generateContent. Useful when the
    configured GEMINI_MODEL starts 404ing — model names/availability
    change over time, and this reflects the real, current state for this
    specific account rather than relying on documentation that may be
    stale. Not linked from the upload page; visit directly when debugging.
    """
    try:
        if not GEMINI_API_KEY:
            return jsonify({"error": "GEMINI_API_KEY is not configured"}), 500

        resp = requests.get(
            f"{GEMINI_BASE}/v1beta/models",
            headers={"x-goog-api-key": GEMINI_API_KEY},
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()

        models = data.get("models", [])
        supporting_generate = [
            {
                "name": m.get("name"),
                "displayName": m.get("displayName"),
                "supportedGenerationMethods": m.get("supportedGenerationMethods", []),
            }
            for m in models
            if "generateContent" in m.get("supportedGenerationMethods", [])
        ]

        return jsonify(
            {
                "currently_configured_model": GEMINI_MODEL,
                "models_supporting_generateContent": supporting_generate,
                "total_models_returned": len(models),
            }
        )
    except Exception as e:  # noqa: BLE001
        return jsonify({"error": _scrub_secrets(str(e))}), 500


@app.route("/process", methods=["POST"])
def process_meeting():
    """
    Expects a multipart/form-data POST (a normal browser file upload form):
      - file field named "recording" — the .mp4/.m4a/.mp3 recording
      - form field "meetingTitle" (optional)

    Streams the upload to a temp file (not buffered as base64/JSON — that
    would inflate a 238MB file to ~317MB and made the old design hit
    request-size limits sooner than necessary).

    On success, forwards a small JSON summary to POWER_AUTOMATE_WEBHOOK_URL
    (if configured) so it can be posted into Teams, AND returns the same
    JSON directly to the browser so the upload page can show a live result
    without waiting on Teams.
    """
    try:
        if UPLOAD_PASSPHRASE:
            provided = request.form.get("passphrase", "")
            if provided != UPLOAD_PASSPHRASE:
                return jsonify({"success": False, "error": "Incorrect passphrase"}), 401

        staff_email = _validate_staff_email(request.form.get("staffEmail", ""))

        if "recording" not in request.files:
            return jsonify({"success": False, "error": "No 'recording' file in upload"}), 400

        uploaded = request.files["recording"]
        if uploaded.filename == "":
            return jsonify({"success": False, "error": "Empty filename"}), 400

        file_name = uploaded.filename
        meeting_title = request.form.get("meetingTitle") or _strip_extension(file_name)

        with tempfile.TemporaryDirectory() as tmpdir:
            input_path = os.path.join(tmpdir, file_name)
            audio_path = os.path.join(tmpdir, "audio.m4a")

            # 1. Stream the uploaded recording straight to disk.
            uploaded.save(input_path)
            input_size = os.path.getsize(input_path)
            logger.info("Received file %s (%d bytes)", file_name, input_size)

            # 2. Extract audio-only using ffmpeg. Needs a real container
            #    (Cloud Run), not a CPU-time-capped edge Worker. -vn drops
            #    video entirely; re-encoded to a modest-bitrate mono AAC
            #    to shrink size well below the original video file.
            _extract_audio(input_path, audio_path)
            audio_size = os.path.getsize(audio_path)
            logger.info("Extracted audio: %d bytes (from %d byte original)", audio_size, input_size)

            # 3. Upload audio to Gemini's Files API.
            file_uri, file_mime = _gemini_upload_file(audio_path, "audio/mp4")

            # 4. Ask Gemini to summarize.
            result = _gemini_summarize(file_uri, file_mime)

        response_payload = {
            "success": True,
            "title": meeting_title,
            "staffEmail": staff_email,
            "summary": result.get("summary", ""),
            "keyDiscussions": result.get("keyDiscussions", []),
            "decisions": result.get("decisions", []),
            "actionItems": result.get("actionItems", []),
        }

        # 5. Forward to Power Automate so it can post into Teams. This is
        #    a tiny JSON payload regardless of original recording size, so
        #    it never touches Power Automate's 100MB/120s HTTP limits.
        #    staffEmail lets the flow post to that person's personal chat
        #    with Flow bot, not just a shared channel.
        _forward_to_teams(response_payload)

        return jsonify(response_payload)

    except InvalidEmailError as e:
        # Deliberately returned before any file handling or Gemini calls
        # run, so a bad/missing email never triggers a paid API call.
        return jsonify({"success": False, "error": str(e)}), 400

    except Exception as e:  # noqa: BLE001 - always return JSON, never a raw 500 HTML page
        logger.exception("Failed to process meeting")
        error_payload = {"success": False, "error": _scrub_secrets(str(e))}
        # Best-effort: let the Teams channel know it failed too, so
        # failures aren't silent even if the staff member closes the tab.
        try:
            _forward_to_teams(error_payload)
        except Exception:
            logger.exception("Also failed to notify Teams of the failure")
        return jsonify(error_payload), 500


def _scrub_secrets(text):
    """
    Defense-in-depth: strips anything that looks like an API key from an
    error message before it's ever returned to the browser or forwarded to
    Teams. The Gemini calls now send the key as a header rather than a URL
    parameter specifically to avoid this, but this catches it regardless —
    e.g. if a future change reintroduces a key-in-URL pattern, or a
    third-party library's own error message includes one.
    """
    if not text:
        return text
    # Covers "?key=XXXX" / "&key=XXXX" query-param style leaks.
    text = re.sub(r"([?&]key=)[^&\s\"']+", r"\1***REDACTED***", text)
    # Covers the actual configured key appearing verbatim anywhere else.
    if GEMINI_API_KEY:
        text = text.replace(GEMINI_API_KEY, "***REDACTED***")
    return text


def _forward_to_teams(payload):
    """POSTs the result JSON to the configured Power Automate webhook
    trigger, which posts it into the Teams channel. If no webhook URL is
    configured, this is a no-op (useful for local testing before Power
    Automate is wired up)."""
    if not POWER_AUTOMATE_WEBHOOK_URL:
        logger.warning("POWER_AUTOMATE_WEBHOOK_URL not configured — skipping Teams post")
        return
    try:
        resp = requests.post(POWER_AUTOMATE_WEBHOOK_URL, json=payload, timeout=30)
        resp.raise_for_status()
    except Exception:
        logger.exception("Failed to forward result to Power Automate webhook")
        raise


class InvalidEmailError(Exception):
    pass


def _validate_staff_email(email):
    """
    Requires a non-empty email ending in @REQUIRED_EMAIL_DOMAIN (case-
    insensitive). Raises InvalidEmailError with a clear message if not —
    caught in process_meeting() and returned as a 400 before any file
    handling or Gemini calls happen, so an invalid email never triggers
    a paid API call.
    """
    email = (email or "").strip()
    if not email:
        raise InvalidEmailError("Your @waktanjong.org email is required.")

    domain_suffix = "@" + REQUIRED_EMAIL_DOMAIN.lower()
    if not email.lower().endswith(domain_suffix):
        raise InvalidEmailError(f"Email must be a {domain_suffix} address.")

    # Light sanity check beyond just the domain suffix — catches obvious
    # typos like "@@waktanjong.org" or missing a local part before the @.
    local_part = email[: -len(domain_suffix)]
    if not local_part or "@" in local_part or " " in email:
        raise InvalidEmailError(f"That doesn't look like a valid {domain_suffix} address.")

    return email


def _strip_extension(filename):
    return os.path.splitext(filename)[0]


def _extract_audio(input_path, output_path):
    """
    Runs ffmpeg to strip video and re-encode to a compact audio-only file.
    -vn: no video
    -ac 1: mono (speech doesn't need stereo, halves size again)
    -b:a 64k: modest bitrate, plenty for speech intelligibility/transcription
    """
    cmd = [
        "ffmpeg",
        "-y",
        "-i", input_path,
        "-vn",
        "-ac", "1",
        "-b:a", "64k",
        output_path,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg audio extraction failed: {result.stderr[-2000:]}")


def _gemini_upload_file(file_path, mime_type):
    """
    Uploads a file to Gemini's Files API using the resumable upload
    protocol, and polls until the file is ACTIVE. Returns (file_uri, mime_type).
    """
    if not GEMINI_API_KEY:
        raise RuntimeError("GEMINI_API_KEY is not configured on the server")

    file_size = os.path.getsize(file_path)
    display_name = os.path.basename(file_path)

    # Step 1: start the resumable upload session. API key goes in a header
    # (x-goog-api-key), not the URL — a key in the URL ends up echoed back
    # verbatim in requests' HTTPError messages, which is how it previously
    # leaked into an error shown in the browser.
    start_resp = requests.post(
        f"{GEMINI_BASE}/upload/v1beta/files",
        headers={
            "x-goog-api-key": GEMINI_API_KEY,
            "X-Goog-Upload-Protocol": "resumable",
            "X-Goog-Upload-Command": "start",
            "X-Goog-Upload-Header-Content-Length": str(file_size),
            "X-Goog-Upload-Header-Content-Type": mime_type,
            "Content-Type": "application/json",
        },
        json={"file": {"display_name": display_name}},
        timeout=30,
    )
    start_resp.raise_for_status()
    upload_url = start_resp.headers.get("x-goog-upload-url")
    if not upload_url:
        raise RuntimeError(f"Gemini upload did not return an upload URL: {start_resp.text}")

    # Step 2: upload the bytes and finalize. upload_url is a Google-issued
    # session URL (no API key embedded), so nothing to scrub here.
    with open(file_path, "rb") as f:
        upload_resp = requests.post(
            upload_url,
            headers={
                "Content-Length": str(file_size),
                "X-Goog-Upload-Offset": "0",
                "X-Goog-Upload-Command": "upload, finalize",
            },
            data=f,
            timeout=300,
        )
    upload_resp.raise_for_status()
    file_info = upload_resp.json()
    file_obj = file_info.get("file", {})
    if "uri" not in file_obj:
        raise RuntimeError(f"Gemini upload response missing file URI: {upload_resp.text}")

    return _wait_until_active(file_obj)


def _wait_until_active(file_obj, max_attempts=45, poll_seconds=2):
    """Polls a just-uploaded Gemini file until it's ACTIVE (processed) or fails."""
    state = file_obj.get("state")
    uri = file_obj.get("uri")
    name = file_obj.get("name")  # e.g. "files/abc123"
    mime_type = file_obj.get("mimeType", "audio/mp4")

    attempts = 0
    while state == "PROCESSING" and attempts < max_attempts:
        time.sleep(poll_seconds)
        check_resp = requests.get(
            f"{GEMINI_BASE}/v1beta/{name}",
            headers={"x-goog-api-key": GEMINI_API_KEY},
            timeout=30,
        )
        check_resp.raise_for_status()
        checked = check_resp.json()
        state = checked.get("state")
        uri = checked.get("uri")
        attempts += 1

    if state != "ACTIVE":
        raise RuntimeError(f"Gemini file did not become ACTIVE in time (last state: {state})")

    return uri, mime_type


def _gemini_summarize(file_uri, mime_type):
    """Calls generateContent with the uploaded audio, requesting structured JSON."""
    prompt = (
        "You are summarising an internal meeting recording for an Islamic "
        "education organisation in Singapore. The conversation freely mixes "
        "English, Malay, and some Arabic (religious/technical terms). "
        "Listen to the full recording and produce: "
        "(1) a concise overall summary (3-6 sentences); "
        "(2) a list of key discussion points - the substantive topics "
        "raised and discussed, not just decisions; "
        "(3) a list of decisions made, stated clearly and specifically; "
        "(4) a list of action items, each with the responsible person if "
        "mentioned in the recording. "
        "Respond ONLY with valid JSON, no markdown fences, matching exactly "
        "this shape: "
        '{"summary": string, "keyDiscussions": [string], '
        '"decisions": [string], "actionItems": [string]}'
    )

    request_body = {
        "contents": [
            {
                "parts": [
                    {"text": prompt},
                    {"file_data": {"file_uri": file_uri, "mime_type": mime_type}},
                ]
            }
        ],
        "generationConfig": {
            "responseMimeType": "application/json",
            "temperature": 0.2,
        },
    }

    resp = requests.post(
        f"{GEMINI_BASE}/v1beta/models/{GEMINI_MODEL}:generateContent",
        headers={"x-goog-api-key": GEMINI_API_KEY},
        json=request_body,
        timeout=600,
    )
    resp.raise_for_status()
    parsed = resp.json()

    candidates = parsed.get("candidates", [])
    if not candidates:
        raise RuntimeError(f"Gemini returned no candidates: {parsed}")

    parts = candidates[0].get("content", {}).get("parts", [])
    raw_text = "".join(p.get("text", "") for p in parts)

    try:
        result = json.loads(raw_text)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"Could not parse Gemini JSON output: {raw_text[:500]}") from e

    result.setdefault("summary", "")
    result.setdefault("keyDiscussions", [])
    result.setdefault("decisions", [])
    result.setdefault("actionItems", [])
    return result


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
