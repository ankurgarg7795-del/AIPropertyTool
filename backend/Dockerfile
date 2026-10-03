FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY pyproject.toml .
COPY app ./app
COPY sql ./sql
# faster-whisper (".[media]") adds ~1 GB; install it on the STT worker image instead.
RUN pip install --no-cache-dir . "pillow>=10"
ENV APT_SQL_DIR=/app/sql
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
