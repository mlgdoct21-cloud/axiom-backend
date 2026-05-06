# Microsoft's official Playwright base image — has Python 3.11 + Chromium +
# all required system libs preinstalled. Avoids the Debian trixie font-package
# breakage we hit with `playwright install --with-deps chromium` on
# python:3.11-slim. This image is the canonical setup for running Playwright
# in production containers.
FROM mcr.microsoft.com/playwright/python:v1.42.0-jammy
WORKDIR /app

ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1

# Backend-specific system libs (gcc + libpq-dev for psycopg2 build).
# The base image is jammy so apt is available; everything else (Chromium
# runtime libs, fonts, libnss3, libgbm1, libasound2, etc.) is already there.
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libpq-dev \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies. The `playwright` Python package is in
# requirements.txt; the browser binaries are already in the base image so
# no `playwright install` step is needed.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY . .

# Non-root kullanıcı. The base image already has a `pwuser` (uid=1000) so we
# reuse it instead of creating a new one — that way the preinstalled
# /ms-playwright browsers stay readable without re-chowning.
RUN chmod +x /app/start.sh \
    && chown -R pwuser:pwuser /app
USER pwuser

# Expose port (Railway domain's targetPort is 8000; PORT env var controls the bind)
EXPOSE 8000

# Run alembic upgrade head before uvicorn so DB schema stays in sync on every boot.
CMD ["/app/start.sh"]
