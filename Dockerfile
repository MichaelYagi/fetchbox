# Fetchbox: one image for linux/amd64 and linux/arm64.
#   docker run -d -p 8787:8787 -p 6881:6881 -p 6881:6881/udp \
#     -e FETCHBOX_ADMIN_PASSWORD=change-me -e PUID=1000 -e PGID=1000 \
#     -v fetchbox-config:/config -v /srv/media:/data ghcr.io/michaelyagi/fetchbox
# /config holds the database, secrets key and logs; /data holds downloads and the
# library (Torrents/, Movies/, TV/ by default) on ONE volume, so imports are hardlinks.

FROM node:24-slim AS web
WORKDIR /src/web
COPY web/package.json web/package-lock.json ./
RUN npm ci
COPY web/ ./
RUN npm run build   # writes ../src/fetchbox/static

FROM python:3.12-slim AS app
COPY --from=ghcr.io/astral-sh/uv:0.13.7 /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
WORKDIR /app
COPY pyproject.toml uv.lock README.md LICENSE ./
COPY src/ src/
COPY --from=web /src/src/fetchbox/static src/fetchbox/static
RUN uv sync --frozen --no-dev --no-editable && rm -rf /root/.cache

FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends tini \
    && rm -rf /var/lib/apt/lists/*
COPY --from=app /app/.venv /app/.venv
COPY docker/entrypoint.sh /entrypoint.sh
ENV PATH="/app/.venv/bin:$PATH" \
    FETCHBOX_DATA_DIR=/config \
    FETCHBOX_DOWNLOADS_ROOT=/data \
    FETCHBOX_HOST=0.0.0.0 \
    FETCHBOX_PORT=8787 \
    PUID=1000 PGID=1000
VOLUME ["/config", "/data"]
EXPOSE 8787 6881/tcp 6881/udp
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8787/api/v1/system/ping', timeout=4).status == 200 else 1)"
ENTRYPOINT ["/usr/bin/tini", "--", "/entrypoint.sh"]
CMD ["fetchbox", "serve"]
