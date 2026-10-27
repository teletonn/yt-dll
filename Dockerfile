FROM python:3.12-slim-bookworm

ARG YTDLP_VERSION=latest
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    YTDLL_DATA_DIR=/data

# ffmpeg for merging/transcoding, ca-cert for TLS to YouTube
RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg \
        ca-certificates \
        curl \
    && rm -rf /var/lib/apt/lists/*

# Static yt-dlp binary (updated on every image build)
RUN if [ "$YTDLP_VERSION" = "latest" ]; then \
        curl -fsSL https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp -o /usr/local/bin/yt-dlp; \
    else \
        curl -fsSL "https://github.com/yt-dlp/yt-dlp/releases/download/${YTDLP_VERSION}/yt-dlp" -o /usr/local/bin/yt-dlp; \
    fi && chmod 0755 /usr/local/bin/yt-dlp

WORKDIR /app

# Application code
COPY app.py start.sh ./
COPY static ./static

# Prepare runtime directories
RUN mkdir -p /app/videos /data/videos && chmod +x /app/start.sh

# Entrypoint ensures persistent data files exist
COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod +x /usr/local/bin/docker-entrypoint.sh

EXPOSE 49977

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python3 -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:49977/api/state', timeout=3)" || exit 1

ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]
CMD ["python3", "app.py"]
