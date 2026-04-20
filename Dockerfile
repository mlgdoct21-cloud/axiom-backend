# Build stage
FROM python:3.9-slim

# Cache buster - change this to force full rebuild
ARG CACHE_BUST=20260420v2

# Set working directory
WORKDIR /app

# Ensure Python output is sent straight to logs without buffering
ENV PYTHONUNBUFFERED=1

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements
COPY requirements.txt .

# Install Python dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY . .

# Expose health check port
EXPOSE 8000

# Run application (Telegram Bot + Crawler + Health Check HTTP server)
CMD ["python", "bot_runner.py"]
