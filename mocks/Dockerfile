# Образ для моков внешних систем: docker build --build-arg MOCK=speechkit -f mocks/Dockerfile mocks
FROM python:3.12-slim
ARG MOCK
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /srv
RUN pip install --no-cache-dir "fastapi==0.115.*" "uvicorn==0.32.*" "python-multipart==0.0.*"
COPY ${MOCK}/app.py app.py
USER nobody
EXPOSE 8000
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]
