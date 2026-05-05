FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY *.py ./

# Mount credentials and state as volumes at runtime:
#   -v $(pwd)/credentials.json:/app/credentials.json:ro
#   -v $(pwd)/token.json:/app/token.json
#   -v $(pwd)/sync_state.json:/app/sync_state.json

CMD ["python", "main.py"]
