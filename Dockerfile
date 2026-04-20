FROM python:3.11-slim
WORKDIR /app
ENV PYTHONUNBUFFERED=1
RUN pip install --no-cache-dir fastapi uvicorn[standard] sqlalchemy asyncpg aiosqlite python-dotenv requests feedparser aiogram passlib[bcrypt] python-jose python-multipart email-validator pydantic==2.5.3 pydantic-settings
COPY . .
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
