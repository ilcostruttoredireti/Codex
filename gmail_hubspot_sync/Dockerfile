FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Mount credentials.json and token.json as secrets/volumes at runtime
# Mount .env as a secret or pass env vars directly

CMD ["python", "sync.py", "--loop"]
