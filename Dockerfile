FROM python:3.12-slim-bookworm

RUN useradd --create-home --uid 1000 reclaimarr
WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY static ./static

RUN mkdir -p /data && chown -R reclaimarr:reclaimarr /app /data
USER reclaimarr

ENV DATA_DIR=/data
EXPOSE 8585

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8585"]
