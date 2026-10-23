# One image for every application service (api, worker, verifier, llm-gateway,
# llm-stub, chroma, ingest, locust). Services differ only in their command, so
# the image is built once and every deployment boundary runs identical code.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PROMETHEUS_DISABLE_CREATED_SERIES=True \
    HOME=/home/app

RUN useradd --create-home --uid 1000 app
WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

# Download all-MiniLM-L6-v2 (ONNX) at build time so containers start offline.
USER app
RUN python -c "from chromadb.utils.embedding_functions import ONNXMiniLM_L6_V2 as E; E()(['warm up'])"

COPY --chown=app:app ecs ./ecs
COPY --chown=app:app prompts ./prompts
COPY --chown=app:app web ./web
COPY --chown=app:app eval ./eval
COPY --chown=app:app loadtest ./loadtest
COPY --chown=app:app corpus ./corpus

EXPOSE 8000
CMD ["uvicorn", "ecs.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
