# Server image only. The camera agent runs on the machine with the camera
# (laptop / Raspberry Pi) and is not part of this image.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    DATA_DIR=/data PORT=8000

WORKDIR /app
COPY server/requirements.txt server/requirements.txt
RUN pip install --no-cache-dir -r server/requirements.txt

COPY server/ server/
COPY frontend/ frontend/
# The built-in sirens (previewed in the dashboard) are generated here.
COPY alert.py sirens.py ./

RUN useradd --create-home app && mkdir -p /data && chown app /data
USER app
VOLUME /data
EXPOSE 8000

# ONE worker: the live-view relay holds frames in memory. Threads give the
# concurrency (each open live view holds one).
CMD gunicorn --bind 0.0.0.0:${PORT} --workers 1 --worker-class gthread --threads 64 \
    --timeout 120 --access-logfile - "server.app:create_app()"
