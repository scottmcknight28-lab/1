FROM python:3.12-slim

WORKDIR /app

# System deps for lxml
RUN apt-get update && apt-get install -y --no-install-recommends \
        gcc libxml2-dev libxslt-dev \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Volumes: /data (SQLite + reports), config mounted separately
ENV WINE_CONFIG_PATH=/config/config.yaml \
    WINE_DB_PATH=/data/wine_auctions.db \
    WINE_REPORTS_DIR=/data/reports \
    HOST=0.0.0.0 \
    PORT=8000

EXPOSE 8000

CMD ["python", "web_main.py"]
