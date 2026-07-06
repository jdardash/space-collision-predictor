FROM python:3.12-slim

LABEL maintainer="jdardash"
LABEL description="Satellite conjunction analysis engine with SGP4 propagation"

WORKDIR /app

COPY pyproject.toml README.md ./
COPY src/ src/

RUN pip install --no-cache-dir .

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD python -c "import httpx; r = httpx.get('http://localhost:8000/health'); r.raise_for_status()" || exit 1

CMD ["python", "-m", "sda.api"]
