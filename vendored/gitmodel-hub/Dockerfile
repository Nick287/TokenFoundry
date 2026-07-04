# syntax=docker/dockerfile:1

# GitModel Hub container image.
FROM python:3.13-slim

# Faster, cleaner Python in containers.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Install dependencies first for better layer caching.
COPY requirements.txt ./
RUN pip install -r requirements.txt

# Copy application code.
COPY hub ./hub

# The hub is stateless: all durable secrets (Copilot OAuth token, hub /v1 key,
# admin token) are injected as env vars at deploy time, and SQLite is an
# ephemeral scratch DB. Point HUB_DATA_DIR at a writable temp dir; no VOLUME so
# nothing is expected to persist across restarts. (Terraform overrides this env
# too, but keep the image self-consistent.)
ENV HUB_DATA_DIR=/tmp/hubdata

# Bind to all interfaces inside the container; map the port on the host.
ENV HUB_HOST=0.0.0.0 \
    HUB_PORT=8088

EXPOSE 8088

CMD ["python", "-m", "hub"]
