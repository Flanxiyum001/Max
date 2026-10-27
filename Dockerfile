FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY pyproject.toml README.md ./
COPY src/ ./src/
RUN python -m pip install --no-cache-dir .

COPY config/ ./config/
COPY skills/ ./skills/
COPY eval/ ./eval/

RUN mkdir -p /app/var /app/workspace
ENV MAX_DATA_DIR=/app/var

# Discord uses an outbound gateway connection; no HTTP port is needed.
CMD ["python", "-m", "max", "discord", "run"]
