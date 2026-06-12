FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY gmail_hubspot_sync.py .

# Mount credentials at runtime:
#   docker run -v $(pwd)/credentials.json:/app/credentials.json \
#              -v $(pwd)/token.json:/app/token.json \
#              -e HUBSPOT_TOKEN=pat-xx-... \
#              gmail-hubspot --continuous --timeline

ENTRYPOINT ["python", "gmail_hubspot_sync.py"]
