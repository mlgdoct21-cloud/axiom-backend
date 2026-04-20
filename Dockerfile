# Build stage
FROM python:3.9-slim

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

# Run application (Telegram Bot + Crawler - no HTTP port needed)
CMD ["python", "bot_runner.py"]
