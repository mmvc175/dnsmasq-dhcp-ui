# syntax=docker/dockerfile:1
FROM alpine:3.20

LABEL org.opencontainers.image.title="dnsmasq-dhcp-ui"
LABEL org.opencontainers.image.description="dnsmasq DHCP/DNS server with a web UI for leases, static reservations and per-client DNS"
LABEL org.opencontainers.image.source="https://github.com/local/dnsmasq-dhcp-ui"
LABEL org.opencontainers.image.licenses="MIT"

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    DATA_DIR=/data \
    WEB_PORT=8080 \
    APP_DIR=/app

RUN apk add --no-cache \
        dnsmasq \
        python3 \
        iputils \
        tini \
    && mkdir -p /data /etc/dnsmasq.d /app \
    && rm -rf /var/cache/apk/*

WORKDIR /app
COPY app/ /app/app/
COPY scripts/ /app/scripts/
RUN chmod +x /app/scripts/lease_notify.py

# DHCP 需要 67/68，DNS 需要 53；Web 管理端口 8080
EXPOSE 53/udp 53/tcp 67/udp 8080/tcp

VOLUME ["/data"]

HEALTHCHECK --interval=30s --timeout=6s --start-period=10s --retries=3 \
  CMD ["python3", "/app/scripts/healthcheck.py"]

ENTRYPOINT ["/sbin/tini", "--"]
CMD ["python3", "-u", "/app/app/main.py"]
