FROM python:3.11-slim

RUN apt-get update && apt-get install -y \
    build-essential curl ca-certificates && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --upgrade pip
RUN pip install -r requirements.txt

COPY . .

ENV PYTHONUNBUFFERED=1
CMD ["uvicorn", "web_bypass:app", "--host", "0.0.0.0", "--port", "8080"]
