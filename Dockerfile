# Pinned by digest, not just tag. A floating tag means the build is not
# reproducible and a replaced upstream tag propagates silently.
# python:3.11-slim-bookworm
FROM python:3.11-slim-bookworm@sha256:6ed54c4a1e4f0e2a2d0f88f2a0ba9a8fd4e0cb2c4b6f2d5c0d6b8e4a2c9f1e3a AS base

# Do not buffer stdout, and do not write .pyc into the image layer.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Install dependencies as root, before dropping privileges.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application
COPY app/ ./app/

# Run as an unprivileged user. Without this the process runs as uid 0, so a
# remote code execution bug starts with root inside the container, and any
# host path mounted writable is writable as root.
RUN groupadd --system --gid 1001 appuser \
    && useradd --system --uid 1001 --gid appuser --no-create-home appuser \
    && chown -R appuser:appuser /app
USER appuser

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health').status==200 else 1)"

# --forwarded-allow-ips is intentionally omitted: set it only when running
# behind a proxy you control, otherwise clients can spoof their source IP and
# defeat rate limiting.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
