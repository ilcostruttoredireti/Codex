FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY gmail_hubspot_sync.py .

ENV POLL_INTERVAL_SECONDS=300

CMD ["python", "gmail_hubspot_sync.py"]
