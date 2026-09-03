FROM python:3.11-slim

# task93 3단계(2026-09-03) 마기 재정정 결론: Tesseract + Render 신규
# Docker서비스(무료티어), 비용 $0, 카드등록 불필요.
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        tesseract-ocr \
        tesseract-ocr-kor \
        libgl1 && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py .

ENV PYTHONUNBUFFERED=1
CMD ["sh", "-c", "gunicorn app:app --bind 0.0.0.0:${PORT:-10000} --timeout 60"]
