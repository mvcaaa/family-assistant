FROM python:3.12-slim-bookworm

RUN useradd --system --uid 10001 --home-dir /data --shell /usr/sbin/nologin familybot \
    && mkdir -p /data /opt/family-assistant \
    && chown familybot:familybot /data

WORKDIR /opt/family-assistant
COPY src /opt/family-assistant/src
COPY deploy/docker-entrypoint.sh /usr/local/bin/family-assistant-entrypoint
RUN chmod 755 /usr/local/bin/family-assistant-entrypoint

ENV PYTHONPATH=/opt/family-assistant/src \
    PYTHONUNBUFFERED=1 \
    DATA_DIR=/data \
    GATEWAY_HOST=0.0.0.0 \
    GATEWAY_PORT=8787

EXPOSE 8787
ENTRYPOINT ["family-assistant-entrypoint"]
