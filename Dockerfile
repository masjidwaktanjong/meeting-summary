# MWT Meeting Summary API - Cloud Run container
# Includes ffmpeg (for audio extraction) + a small Flask app that calls Gemini.

FROM python:3.12-slim

# Install ffmpeg (needed to strip video and shrink recordings to audio-only)
RUN apt-get update && \
    apt-get install -y --no-install-recommends ffmpeg && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ ./app/

ENV PORT=8080
EXPOSE 8080

# Cloud Run sets $PORT; gunicorn binds to it. Single worker, generous
# timeout since a long meeting's ffmpeg + Gemini processing can take minutes.
CMD exec gunicorn --bind 0.0.0.0:${PORT} --workers 1 --threads 4 --timeout 900 app.main:app
