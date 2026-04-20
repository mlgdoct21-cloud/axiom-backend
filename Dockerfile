FROM python:3.11-slim
WORKDIR /app
ENV PYTHONUNBUFFERED=1
RUN pip install fastapi uvicorn
COPY main_test.py .
CMD ["uvicorn", "main_test:app", "--host", "0.0.0.0", "--port", "8000"]
