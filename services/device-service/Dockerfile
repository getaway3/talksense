# Сборка из каталога services/: docker build -f device-service/Dockerfile services
FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PYTHONPATH=/srv/common \
    DB_PATH=/data/device.db
WORKDIR /srv
COPY common/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt
COPY common/talksense_common common/talksense_common
COPY device-service/device_service device_service
RUN useradd --create-home app && mkdir -p /data && chown app /data
USER app
EXPOSE 8000
HEALTHCHECK --interval=10s --timeout=3s --retries=5 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health', timeout=2)"
CMD ["uvicorn", "device_service.main:app", "--host", "0.0.0.0", "--port", "8000"]
