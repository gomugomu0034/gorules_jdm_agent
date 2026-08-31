FROM python:3.13-slim

WORKDIR /app

# Install dependencies first to leverage Docker layer caching
COPY backend/requirements.txt requirements.txt
RUN pip install --no-cache-dir -r requirements.txt