ARG PYTHON_IMAGE=python:3.12-slim
FROM ${PYTHON_IMAGE}

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 TZ=Asia/Shanghai
WORKDIR /app
COPY pyproject.toml ./
COPY cloud_monitor ./cloud_monitor
RUN pip install --no-cache-dir . \
    && useradd --uid 10001 --create-home monitor \
    && mkdir -p /app/data /app/config \
    && chown -R monitor:monitor /app/data
USER monitor
EXPOSE 8080
ENTRYPOINT ["python", "-m", "cloud_monitor"]
CMD ["--config", "/app/config/config.yaml", "serve"]
