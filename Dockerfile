# One image, three services: `server`, `tts` and `agent` (see scripts/entrypoint.sh).
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PYTHONDONTWRITEBYTECODE=1

# ffmpeg: recordings and Edge-TTS decoding. sqlite3: backups. curl: healthchecks.
RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg sqlite3 curl \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY . .

# The agent worker's local models (Silero VAD, end-of-turn detector) are fetched
# at build time so the first call on a fresh host does not wait for a download.
RUN python agent.py download-files || true

RUN chmod +x scripts/entrypoint.sh scripts/backup.sh
VOLUME ["/app/data"]
EXPOSE 8000 8002
ENTRYPOINT ["scripts/entrypoint.sh"]
CMD ["server"]
