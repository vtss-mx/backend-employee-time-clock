FROM python:3.14-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    FACE_MODELS_DIR=/app/models

WORKDIR /app

# libglib2.0-0 es requerido por OpenCV headless en imágenes slim.
RUN apt-get update \
    && apt-get install -y --no-install-recommends libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY app/facial_recognition/model_store.py /tmp/model_store.py
# Descarga y verifica (SHA-256) los modelos ONNX durante el build.
RUN python /tmp/model_store.py /app/models && rm /tmp/model_store.py

COPY . .

RUN useradd --create-home --uid 1000 appuser && chown -R appuser:appuser /app
USER appuser

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/api/health/live', timeout=4).status == 200 else 1)"

ENTRYPOINT ["./entrypoint.sh"]

# ---------- Imagen de desarrollo/calidad (no se usa en producción) ----------
#   docker build --target dev -t time-clock-backend-dev backend
#   docker run --rm -v "$PWD/backend:/app" time-clock-backend-dev   # pruebas + anomalías
FROM runtime AS dev
USER root
COPY requirements-dev.txt .
RUN pip install -r requirements-dev.txt
# Los modelos ONNX fuera de /app: al montar el código del proyecto en /app para las pruebas siguen
# disponibles y el motor facial REAL también se prueba (tests/test_face_engine_real.py).
# Legibles para cualquier usuario: las pruebas corren con el uid de quien las lanza (-u).
RUN cp -r /app/models /opt/models && chmod -R a+rX /opt/models
ENV TEST_FACE_MODELS_DIR=/opt/models
USER appuser
ENTRYPOINT []
CMD ["python", "scripts/quality.py"]
