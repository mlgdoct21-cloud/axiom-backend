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

# Copy application code
COPY . .

# Non-root kullanıcı oluştur ve dosya sahipliğini ata
# Container içinde root yetkisiyle çalışmak güvenlik riskidir; eğer bir kütüphanede
# RCE bulunursa saldırgan tüm sistem üzerinde root olur. Non-root kullanıcı bu
# yetkiyi sınırlar.
RUN chmod +x /app/start.sh \
    && useradd --create-home --shell /bin/bash --uid 1000 appuser \
    && chown -R appuser:appuser /app
USER appuser

# Expose port (Railway domain's targetPort is 8000; PORT env var controls the bind)
EXPOSE 8000

# Run alembic upgrade head before uvicorn so DB schema stays in sync on every boot.
CMD ["/app/start.sh"]
