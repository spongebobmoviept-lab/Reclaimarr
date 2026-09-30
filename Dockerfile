FROM python:3.12-slim-bookworm

RUN useradd --create-home --uid 1000 reclaimarr
WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY static ./static

COPY entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod 0755 /usr/local/bin/entrypoint.sh \
    && mkdir -p /data && chown -R reclaimarr:reclaimarr /data

# The entrypoint starts as root only to make /data writable, then drops to
# PUID:PGID (default 1000) before starting the app. Set `user:` in compose
# to skip that step entirely.

ENV DATA_DIR=/data
EXPOSE 8585

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8585"]
