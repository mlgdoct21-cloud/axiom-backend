FROM python:3.11-slim
WORKDIR /app

ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1

# Install system dependencies. Chromium runtime libs listed explicitly because
# `playwright install --with-deps` pulls font packages (ttf-unifont,
# ttf-ubuntu-font-family) that aren't available on Debian trixie and break
# the build. The set below is the minimum Chromium needs to launch headless.
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libpq-dev \
    libnss3 \
    libnspr4 \
    libatk1.0-0 \
    libatk-bridge2.0-0 \
    libcups2 \
    libdrm2 \
    libxcomposite1 \
    libxdamage1 \
    libxfixes3 \
    libxrandr2 \
    libgbm1 \
    libxkbcommon0 \
    libpango-1.0-0 \
    libcairo2 \
    libasound2 \
    libatspi2.0-0 \
    fonts-liberation \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Install Playwright Chromium for the CoinGlass ETF scheduler.
# Use the bare `install` (no --with-deps) — system libs are handled above.
# The browser binary lands under /ms-playwright (default PLAYWRIGHT_BROWSERS_PATH).
RUN python -m playwright install chromium

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
