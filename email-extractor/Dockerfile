ARG BUILD_FROM=python:3.12-slim-bookworm
FROM ${BUILD_FROM}

ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1

# OCR + PDF rasterization stack (Slovak + Czech + English)
RUN apt-get update && apt-get install -y --no-install-recommends \
        tesseract-ocr tesseract-ocr-ces tesseract-ocr-slk tesseract-ocr-eng \
        poppler-utils ghostscript \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt
COPY app/ ./app/

CMD ["python", "-m", "app.main"]
