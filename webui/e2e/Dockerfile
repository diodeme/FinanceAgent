FROM mcr.microsoft.com/playwright/python:v1.55.0-noble

WORKDIR /e2e
RUN pip install --no-cache-dir playwright==1.55.0 httpx==0.28.1 openpyxl==3.1.5
COPY acceptance.py /e2e/acceptance.py

ENV PYTHONUNBUFFERED=1 \
    FIN_API_URL=http://127.0.0.1:8000 \
    FIN_WEB_URL=http://127.0.0.1:8080 \
    FIN_USER_ID=dev

ENTRYPOINT ["python", "/e2e/acceptance.py"]
