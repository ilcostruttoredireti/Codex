FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Mount credentials.json and token.json as Docker secrets / volumes at runtime
# e.g.: docker run -v /path/to/credentials.json:/app/credentials.json \
#                  -v /path/to/token.json:/app/token.json \
#                  --env-file .env gmail-hubspot-sync

CMD ["python", "main.py"]
