# Мини-апп конструктора VPN-ботов (constructor_web → /app/constructor_web/dist)
FROM node:22-slim AS web

WORKDIR /web
COPY constructor_web/package.json constructor_web/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY constructor_web/ ./
RUN npm run build

FROM python:3.13-slim AS builder

RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    && rm -rf /var/lib/apt/lists/*

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

COPY requirements.txt .

RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

FROM python:3.13-slim

ARG VERSION="v2.9.3"
ARG BUILD_DATE
ARG VCS_REF

RUN apt-get update && apt-get install -y --no-install-recommends \
    wget \
    && rm -rf /var/lib/apt/lists/* \
    && apt-get clean

COPY --from=builder /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

RUN groupadd -g 1000 app && \
    useradd -u 1000 -g 1000 -m -s /bin/bash app

WORKDIR /app

COPY --chown=app:app . .
COPY --from=web --chown=app:app /web/dist ./constructor_web/dist

RUN mkdir -p logs data && \
    chown -R app:app /app logs data

USER app

ENV PYTHONPATH=/app \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    VERSION=${VERSION} \
    BUILD_DATE=${BUILD_DATE} \
    VCS_REF=${VCS_REF}

EXPOSE 8080

LABEL org.opencontainers.image.title="Bedolaga RemnaWave Bot" \
      org.opencontainers.image.description="Telegram bot for RemnaWave VPN service" \
      org.opencontainers.image.version="${VERSION}" \
      org.opencontainers.image.created="${BUILD_DATE}" \
      org.opencontainers.image.revision="${VCS_REF}" \
      org.opencontainers.image.source="https://github.com/fr1ngg/remnawave-bedolaga-telegram-bot" \
      org.opencontainers.image.url="https://github.com/fr1ngg/remnawave-bedolaga-telegram-bot" \
      org.opencontainers.image.vendor="fr1ngg"

HEALTHCHECK --interval=30s --timeout=10s --start-period=30s --retries=3 \
    CMD wget --no-verbose --tries=1 --spider http://localhost:8080/health || exit 1

CMD ["python", "main.py"]
