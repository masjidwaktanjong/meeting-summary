"""
MWT Meeting Summary API (Cloud Run)
-----------------------------------
Receives a meeting recording (video or audio) plus an optional .vtt
transcript, strips the recording down to audio-only using ffmpeg (this is
the step that can't run on Cloudflare Workers' free tier, since it needs
real CPU time, not just I/O waiting), uploads the audio to Gemini's Files
API, and asks Gemini to produce a structured summary: key discussions,
decisions made, and action items. Designed to be called from a Power
Automate "HTTP" action.

Portable by design: this whole service is a plain Flask app in a Docker
container. Any masjid (or MWT after a staff handover) can redeploy it
under their own free Google Cloud Run project — nothing here is tied to
a specific Google account. See README.md for deploy steps.
"""

import base64
import json
import logging
import os
import subprocess
import tempfile
import time
import uuid

import requests
from flask import Flask, jsonify, request

app = Flask(__name__)
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
API_SECRET = os.environ.get("API_SECRET")  # shared secret Power Automate must send
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
GEMINI_BASE = "https://generativelanguage.googleapis.com"

# Cloud Run request body limit is 32MB by default for standard HTTP requests.
# We expect the raw recording to arrive base64-encoded in JSON, so keep an
# eye on this — if MWT's recordings routinely exceed that even before audio
# extraction, the fallback documented in README.md (client-side pre-trim) is
# needed. See README "Known limits" section.
MAX_REQUEST_MB = 200  # Flask/Werkzeug side limit; actual platform ceiling may differ


@app.route("/", methods=["GET"])
def health():
    """Simple health check / sanity endpoint."""
    return jsonify({"status": "ok", "message": "MWT Meeting Summary API is running."})


@app.route("/process", methods=["POST"])
def process_meeting():
    """
    Expects JSON body:
    {
      "fileBase64": "...",          # the recording, base64-encoded
      "fileName": "recording.mp4",
      "mimeType": "video/mp4",      # best guess is fine; we re-detect via ffmpeg anyway
      "vttText": "...",             # optional, empty string if not supplied
      "meetingTitle": "...",        # optional
      "apiSecret": "..."            # must match API_SECRET env var
    }

    Returns JSON:
    {
      "success": true,
      "title": "...",
      "summary": "...",
      "keyDiscussions": [...],
      "decisions": [...],
      "actionItems": [...],
      "hasSpeakerLabels": true/false
    }
    """
    try:
        _check_auth(request)
        payload = request.get_json(force=True, silent=False)

        if not payload or "fileBase64" not in payload:
            return jsonify({"success": False, "error": "fileBase64 is required"}), 400

        file_name = payload.get("fileName", "recording.mp4")
        meeting_title = payload.get("meetingTitle") or _strip_extension(file_name)
        vtt_text = payload.get("vttText", "") or ""

        with tempfile.TemporaryDirectory() as tmpdir:
            input_path = os.path.join(tmpdir, file_name)
            audio_path = os.path.join(tmpdir, "audio.m4a")

            # 1. Decode and save the uploaded recording.
            raw_bytes = base64.b64decode(payload["fileBase64"])
            with open(input_path, "wb") as f:
                f.write(raw_bytes)
            logger.info("Received file %s (%d bytes)", file_name, len(raw_bytes))

            # 2. Extract audio-only using ffmpeg. This is the step that
            #    needs a real container (Cloud Run), not a 10ms-CPU-capped
            #    edge Worker. -vn drops video entirely; we re-encode to a
            #    modest-bitrate AAC (.m4a) to shrink file size substantially
            #    versus the original video file.
            _extract_audio(input_path, audio_path)
            audio_size = os.path.getsize(audio_path)
            logger.info("Extracted audio: %d bytes", audio_size)

            # 3. Upload audio to Gemini's Files API (handles large files
            #    without hitting generateContent's inline request-size cap).
            file_uri, file_mime = _gemini_upload_file(audio_path, "audio/mp4")

            # 4. Ask Gemini to summarize.
            result = _gemini_summarize(file_uri, file_mime)

        has_speaker_labels = _vtt_has_speakers(vtt_text)

        return jsonify(
            {
                "success": True,
                "title": meeting_title,
                "summary": result.get("summary", ""),
                "keyDiscussions": result.get("keyDiscussions", []),
                "decisions": result.get("decisions", []),
                "actionItems": result.get("actionItems", []),
                "hasSpeakerLabels": has_speaker_labels,
            }
        )

    except AuthError as e:
        return jsonify({"success": False, "error": str(e)}), 401
    except Exception as e:  # noqa: BLE001 - want to always return JSON, never a raw 500 HTML page
        logger.exception("Failed to process meeting")
        return jsonify({"success": False, "error": str(e)}), 500


class AuthError(Exception):
    pass


def _check_auth(req):
    """Shared-secret check so this endpoint can't be triggered by strangers
    who find the URL and run up the Gemini bill. Power Automate sends the
    secret as a field in the JSON body (simplest to configure from a flow,
    no custom header wiring needed)."""
    if not API_SECRET:
        # No secret configured on the server — allow through, but this is
        # not recommended for anything beyond initial local testing.
        return
    payload = req.get_json(force=True, silent=True) or {}
    provided = payload.get("apiSecret")
    if provided != API_SECRET:
        raise AuthError("Unauthorized: missing or invalid API secret")


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

    # Step 1: start the resumable upload session.
    start_resp = requests.post(
        f"{GEMINI_BASE}/upload/v1beta/files?key={GEMINI_API_KEY}",
        headers={
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

    # Step 2: upload the bytes and finalize.
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
        check_resp = requests.get(f"{GEMINI_BASE}/v1beta/{name}?key={GEMINI_API_KEY}", timeout=30)
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
        f"{GEMINI_BASE}/v1beta/models/{GEMINI_MODEL}:generateContent?key={GEMINI_API_KEY}",
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


def _vtt_has_speakers(vtt_text):
    """Quick check for whether the supplied .vtt contains speaker tags,
    just to report hasSpeakerLabels back to the caller."""
    if not vtt_text or not vtt_text.strip():
        return False
    return "<v " in vtt_text or "<v\t" in vtt_text


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
