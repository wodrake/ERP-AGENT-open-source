FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

COPY requirements.lock requirements.txt requirements-dev.txt requirements-observability.txt ./
RUN pip install --no-cache-dir -r requirements.lock

COPY . .

EXPOSE 8000 9000 8081

# 具体进程由 docker-compose 的 command 覆盖。
CMD ["python", "-m", "src.api_view.web_main"]
