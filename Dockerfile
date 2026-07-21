FROM ghcr.io/astral-sh/uv:0.11.26 AS uv

FROM python:3.13-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PATH="/app/.venv/bin:${PATH}" \
    HOME=/home/grokreg \
    DISPLAY=:99 \
    GROK_REG_DATA_DIR=/data \
    GROK_REG_WEB_HOST=0.0.0.0 \
    GROK_REG_WEB_PORT=5000

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        chromium \
        chromium-sandbox \
        dbus-x11 \
        fonts-liberation \
        fonts-noto-cjk \
        tini \
        tk \
        xvfb \
    && rm -rf /var/lib/apt/lists/*

RUN groupadd --gid 10001 grokreg \
    && useradd --uid 10001 --gid 10001 --create-home --shell /usr/sbin/nologin grokreg \
    && install -d -o grokreg -g grokreg -m 0700 /app /data \
    && install -d -o root -g root -m 1777 /tmp/.X11-unix

COPY --from=uv /uv /uvx /usr/local/bin/

WORKDIR /app
COPY --chown=grokreg:grokreg pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev

COPY --chown=grokreg:grokreg . .
RUN chmod 0555 /app/docker-entrypoint.sh \
    && python -m compileall -q . \
    && python -m unittest discover -s tests

USER grokreg:grokreg

VOLUME ["/data"]
EXPOSE 5000

HEALTHCHECK --interval=15s --timeout=5s --start-period=20s --retries=4 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:5000/login', timeout=3)"

ENTRYPOINT ["/usr/bin/tini", "--", "/app/docker-entrypoint.sh"]
