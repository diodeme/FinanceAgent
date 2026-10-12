FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY requirements.txt ./
RUN pip install -r requirements.txt

COPY finagent ./finagent
ENV PYTHONPATH=/app

EXPOSE 8000
# fin schema (idempotent) before serving; retry loop covers pg restarts
CMD ["sh", "-c", "python -m finagent.migrate && uvicorn finagent.app:app --host 0.0.0.0 --port 8000"]
