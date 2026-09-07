FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY agent ./agent
COPY main.py .
ENV PYTHONUNBUFFERED=1 PYTHONPATH=/app
CMD ["python", "main.py", "run"]
