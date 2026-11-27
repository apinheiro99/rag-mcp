FROM python:3.12-slim

ARG RAG_BUILD_VERSION=dev
ARG RAG_BUILD_COMMIT=
LABEL org.opencontainers.image.version=$RAG_BUILD_VERSION \
      org.opencontainers.image.revision=$RAG_BUILD_COMMIT

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    RAG_BUILD_VERSION=$RAG_BUILD_VERSION \
    RAG_BUILD_COMMIT=$RAG_BUILD_COMMIT

WORKDIR /srv/rag

RUN apt-get update \
    && apt-get install -y --no-install-recommends libmobi-tools djvulibre-bin libchm-bin unrtf \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY config ./config
COPY scripts/evaluate_retrieval.py ./scripts/evaluate_retrieval.py
COPY examples/evaluation/retrieval.json ./examples/evaluation/retrieval.json

ENV DATA_DIR=/rag-private/data \
    DEFAULTS_FILE=/srv/rag/config/defaults.yaml \
    RAG_HOST=0.0.0.0 \
    RAG_APP_PORT=8000

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=10s --start-period=25s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/health', timeout=8)"

CMD ["sh", "-c", "if [ \"$RAG_DEV_RELOAD\" = \"true\" ] || [ \"$RAG_DEV_RELOAD\" = \"1\" ]; then exec python -m uvicorn app.main:app --host \"$RAG_HOST\" --port \"$RAG_APP_PORT\" --reload --reload-dir /srv/rag/app --reload-dir /srv/rag/config; else exec python -m app.main; fi"]
