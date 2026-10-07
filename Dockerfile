FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
RUN mkdir -p logs

CMD ["sh", "-c", "python prestart.py && uvicorn main:app --host 0.0.0.0 --port 8000 --log-level debug --log-config logging.yaml > logs/fastapi.log 2>&1"]
