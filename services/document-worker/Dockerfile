FROM python:3.12-slim

RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        libmupdf-dev \
        gcc \
        g++ \
        libgl1 \
        libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY services/document-worker/requirements.txt .
RUN pip install --no-cache-dir torch torchvision --extra-index-url https://download.pytorch.org/whl/cpu && \
    pip install --no-cache-dir -r requirements.txt

COPY services/document-worker/worker/ ./worker/
COPY services/document-worker/benchmark/ ./benchmark/
COPY services/document-worker/integration/ ./integration/

RUN mkdir -p /tmp/pdf_downloads

CMD ["python", "-m", "worker"]
