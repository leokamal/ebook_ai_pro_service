# E-Book AI Pro
# Build:  docker build -t ebook-ai .
# Run:    docker run --env-file .env -p 8000:8000 -v ebook_ai_data:/data ebook-ai
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    # everything stateful lives under /data: mount a persistent volume there
    CHROMA_PERSIST_DIRECTORY=/data/chroma_pro \
    HF_HOME=/data/hf

WORKDIR /app

# gosu: the entrypoint drops privileges after fixing the volume ownership
RUN apt-get update && apt-get install -y --no-install-recommends gosu \
    && rm -rf /var/lib/apt/lists/*

# dependencies first (cached layer). The local embedding model (torch) is optional:
#   docker build --build-arg INSTALL_HUGGINGFACE=true -t ebook-ai .
ARG INSTALL_HUGGINGFACE=false
COPY requirements.txt requirements-huggingface.txt ./
RUN if [ "$INSTALL_HUGGINGFACE" = "true" ]; then pip install -r requirements-huggingface.txt; \
    else pip install -r requirements.txt; fi

COPY . .

# The entrypoint starts as root ONLY to chown the mounted volume, then runs the app as the unprivileged `app` user.
RUN useradd --create-home --uid 10001 app && mkdir -p /data && chown -R app:app /data /app \
    && chmod +x /app/docker-entrypoint.sh
VOLUME ["/data"]
EXPOSE 8000

# /health is a cheap liveness probe; /api/v1/health reports index/worker details.
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
    CMD python -c "import os,urllib.request;urllib.request.urlopen('http://127.0.0.1:%s/health' % os.environ.get('PORT','8000'),timeout=4)"

# ONE worker on purpose: Chroma's PersistentClient and the in-process job runner are single-process.
# Scale up the instance (RAM/CPU), not the worker count.
ENTRYPOINT ["/app/docker-entrypoint.sh"]
CMD ["sh", "-c", "exec uvicorn main:app --host 0.0.0.0 --port ${PORT:-8000} --workers 1 --proxy-headers"]
