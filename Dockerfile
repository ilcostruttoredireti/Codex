FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Mount credentials.json and token.json as Docker secrets or volumes
ENTRYPOINT ["python", "main.py"]
