FROM python:3.11-slim
WORKDIR /app

ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libpq-dev \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Install Playwright Chromium for the CoinGlass ETF scheduler. Done as root
# before the user switch so --with-deps can apt-get system libraries.
# The browser binary lands under /ms-playwright (default PLAYWRIGHT_BROWSERS_PATH).
RUN python -m playwright install --with-deps chromium

# Copy application code
COPY . .

# Non-root kullanıcı oluştur ve dosya sahipliğini ata
# Container içinde root yetkisiyle çalışmak güvenlik riskidir; eğer bir kütüphanede
# RCE bulunursa saldırgan tüm sistem üzerinde root olur. Non-root kullanıcı bu
# yetkiyi sınırlar.
RUN chmod +x /app/start.sh \
    && useradd --create-home --shell /bin/bash --uid 1000 appuser \
    && chown -R appuser:appuser /app /ms-playwright
USER appuser

# Expose port (Railway domain's targetPort is 8000; PORT env var controls the bind)
EXPOSE 8000

# Run alembic upgrade head before uvicorn so DB schema stays in sync on every boot.
CMD ["/app/start.sh"]
