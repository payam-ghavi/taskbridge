FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ ./app/

ENV TASKBRIDGE_DATA=/data \
    PORT=3737 \
    PYTHONUNBUFFERED=1

EXPOSE 3737
VOLUME ["/data"]

CMD ["python", "-m", "app"]
