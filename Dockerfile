FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DATA_DIR=/data \
    TZ=Asia/Dubai

WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends tzdata curl \
    && rm -rf /var/lib/apt/lists/*

RUN useradd --uid 1000 --create-home --shell /usr/sbin/nologin ace

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY integrations ./integrations
COPY skills ./skills
COPY config ./config
COPY templates ./templates

RUN mkdir -p /data/logs /data/ace && chown -R ace:ace /data
USER ace

EXPOSE 8080
HEALTHCHECK --interval=60s --timeout=5s --retries=3 CMD curl -fs http://localhost:8080/health || exit 1
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080", "--proxy-headers"]
