# Use official Python 3.11 slim image
FROM python:3.11-slim

# Install system dependencies needed by Playwright / Chromium
RUN apt-get update && apt-get install -y \
    wget gnupg ca-certificates \
    libnss3 libatk1.0-0 libatk-bridge2.0-0 libcups2 \
    libxcomposite1 libxrandr2 libxdamage1 libxkbcommon0 \
    libgbm1 libasound2 libpangocairo-1.0-0 libgtk-3-0 \
    libdbus-1-3 libxcb1 libx11-6 fonts-liberation curl unzip \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy requirements first for caching
COPY requirements.txt .

# Upgrade pip and install Python dependencies
RUN pip install --upgrade pip
RUN pip install -r requirements.txt

# Install Playwright browsers
RUN playwright install --with-deps chromium

# Copy all project files
COPY . .

# Ensure Python outputs logs in real-time
ENV PYTHONUNBUFFERED=1

# Command to start FastAPI with uvicorn
CMD ["uvicorn", "web_bypass:app", "--host", "0.0.0.0", "--port", "8080"]# -----------------------------
# Copy Python dependencies and install
# -----------------------------
COPY requirements.txt .

RUN pip install --upgrade pip
RUN pip install -r requirements.txt

# -----------------------------
# Install Playwright browsers
# -----------------------------
# Chromium is enough for most cases; add firefox/webkit if needed
RUN playwright install --with-deps chromium

# -----------------------------
# Copy bot code
# -----------------------------
COPY . .

# -----------------------------
# Environment settings
# -----------------------------
# Ensures Python prints logs to stdout immediately
ENV PYTHONUNBUFFERED=1

# -----------------------------
# Default command
# -----------------------------
# Starts the bot
CMD ["python", "bot.py"]
