FROM python:3.11-slim
WORKDIR /app

ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1
ENV PLAYWRIGHT_BROWSERS_PATH=/opt/pw-browsers

# Install system dependencies (Playwright Chromium runtime libs dahil)
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libpq-dev \
    wget \
    libnss3 libnspr4 libatk1.0-0 libatk-bridge2.0-0 libcups2 \
    libdrm2 libxkbcommon0 libxcomposite1 libxdamage1 libxfixes3 \
    libxrandr2 libgbm1 libpango-1.0-0 libcairo2 libasound2 \
    libatspi2.0-0 \
    fonts-liberation fonts-noto-color-emoji \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Install Playwright Chromium browser into shared path
# (single browser, ~280MB; cache shared with appuser via PLAYWRIGHT_BROWSERS_PATH)
RUN mkdir -p /opt/pw-browsers \
    && python -m playwright install chromium --with-deps \
    && chmod -R a+rX /opt/pw-browsers

# Copy application code
COPY . .

# Non-root kullanıcı oluştur ve dosya sahipliğini ata
# Container içinde root yetkisiyle çalışmak güvenlik riskidir; eğer bir kütüphanede
# RCE bulunursa saldırgan tüm sistem üzerinde root olur. Non-root kullanıcı bu
# yetkiyi sınırlar.
RUN useradd --create-home --shell /bin/bash --uid 1000 appuser \
    && chown -R appuser:appuser /app /opt/pw-browsers
USER appuser

# Expose port (Railway domain's targetPort is 8000; PORT env var controls the bind)
EXPOSE 8000

# Start command - Railway sets PORT env variable (default 8000 to match public domain)
CMD uvicorn main:app --host 0.0.0.0 --port ${PORT:-8000}
