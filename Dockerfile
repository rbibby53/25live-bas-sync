# 25Live -> Niagara schedule sync — container image.
#
# This packages the headless sync (main.py). The Tkinter editor is not run in a
# container; edit space_mapping.yaml / config.yaml on a workstation, then mount
# them in (see README "Run with Docker").
#
# Build:  docker build -t 25live-niagara-sync .
# Run:    docker run --rm \
#           -e BAS_25LIVE_PASSWORD=... -e BAS_NIAGARA_PASSWORD=... \
#           -v "$(pwd)/config:/config:ro" \
#           25live-niagara-sync --validate

FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

# tzdata: zoneinfo needs a zone database (mirrors the requirements.txt note for
# Windows; the OS package keeps it current). ca-certificates: HTTPS to 25Live.
RUN apt-get update \
 && apt-get install -y --no-install-recommends tzdata ca-certificates \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt ./
RUN pip install -r requirements.txt

COPY main.py docker-entrypoint.sh ./
RUN chmod +x docker-entrypoint.sh \
 && useradd --create-home --uid 10001 appuser \
 && mkdir -p /config /app/logs \
 && chown -R appuser:appuser /app /config

USER appuser

# Default config locations inside the image — mount your files at /config, or
# override these to point elsewhere.
ENV BAS_CONFIG=/config/config.yaml \
    BAS_DEFAULTS=/config/defaults.yaml \
    BAS_SPACE_MAP=/config/space_mapping.yaml

# Args after the image name are passed straight to main.py (e.g. --validate,
# --dry-run, --discover). With no args, it does a live sync.
ENTRYPOINT ["./docker-entrypoint.sh"]
CMD []
