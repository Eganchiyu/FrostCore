FROM python:3.11-slim

WORKDIR /app

COPY pyproject.toml README.md ./
COPY src/ ./src/
COPY examples/ ./examples/

RUN pip install --no-cache-dir .

ENV PYTHONUNBUFFERED=1
ENV FROSTCORE_SEED_DIR=/data/bot.seed

HEALTHCHECK --interval=15s --timeout=3s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8778/health', timeout=2)"

CMD ["python", "examples/01_quickstart.py"]
