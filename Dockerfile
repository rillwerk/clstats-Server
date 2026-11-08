FROM python:3.13-slim

LABEL org.opencontainers.image.title="ClStats Server" \
      org.opencontainers.image.description="Self-hosted companion server for the ClStats iOS app: polls your Claude plan usage, keeps the history, serves insights." \
      org.opencontainers.image.licenses="MIT"

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 DATA_DIR=/data
WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app

# Runs as uid 99 / gid 100 (nobody:users on Unraid). With a bind mount elsewhere, either
# `chown 99:100 data` or set `user:` in docker-compose.yml.
RUN useradd --uid 99 --gid 100 --no-create-home --shell /usr/sbin/nologin app 2>/dev/null || true \
 && mkdir -p /data && chown 99:100 /data
USER 99:100
VOLUME /data
EXPOSE 8080
HEALTHCHECK --interval=60s --timeout=5s CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/api/health')"
CMD ["uvicorn", "app.main:app_factory", "--factory", "--host", "0.0.0.0", "--port", "8080", "--proxy-headers", "--forwarded-allow-ips", "*"]
