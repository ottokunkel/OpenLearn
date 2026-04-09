FROM python:3.12-slim

RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        libmupdf-dev \
        gcc \
        g++ \
        libgl1-mesa-glx \
        libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir torch --extra-index-url https://download.pytorch.org/whl/cpu && \
    pip install --no-cache-dir -r requirements.txt

COPY worker/ ./worker/

RUN mkdir -p /tmp/pdf_downloads

CMD ["python", "-m", "worker"]
