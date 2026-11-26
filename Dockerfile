# demoreel API + worker (analysis and 2D renders). The in-game renderer is NOT part of this image:
# it needs a machine with CS2, a GPU and a desktop session — run `demoreel worker -c ingame` there.
FROM python:3.13-slim

# Inside the container the server listens on every interface; what reaches it is decided by the published
# port (docker-compose.yml: this machine only). The public URL names the host the server answers to while no
# key is set: change it to the address people use when you expose the port (with a key, see the compose file).
ENV PYTHONUNBUFFERED=1 PYTHONUTF8=1 \
    UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy \
    DEMOREEL_DATA_DIR=/data DEMOREEL_HOST=0.0.0.0 DEMOREEL_PORT=8000 \
    DEMOREEL_PUBLIC_URL=http://localhost:8000

# skia needs GL/fontconfig at import time; fonts are for the 2D renderer (names with symbols/emoji included)
RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg libgl1 libegl1 libfontconfig1 fonts-dejavu-core fonts-noto-core fonts-noto-color-emoji \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app
COPY pyproject.toml uv.lock README.md LICENSE ./
RUN uv sync --frozen --no-dev --no-install-project
COPY src ./src
RUN uv sync --frozen --no-dev

RUN useradd --create-home --uid 10001 demoreel && mkdir -p /data && chown demoreel /data
USER demoreel
VOLUME /data
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=3).status == 200 else 1)"

ENTRYPOINT ["/app/.venv/bin/demoreel"]
CMD ["serve"]
