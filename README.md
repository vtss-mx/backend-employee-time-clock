# Backend — FastAPI

API REST de Employee Time Clock. La documentación general (arquitectura, endpoints, ejemplos y seguridad) está en el [README principal](../README.md).

## Requisitos

- Python 3.12
- PostgreSQL 14 o superior (para pruebas se puede usar SQLite)

## Instalación y ejecución

```bash
python3.12 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
# TODA la configuración del backend vive en backend/.env (solo variables con valor; ver README principal, §7).
python scripts/generate_secrets.py > .env   # instalación nueva: secretos y base de datos
python -m app.facial_recognition.model_store    # YuNet + SFace + CLIP (verificados con SHA-256)
alembic upgrade head
uvicorn app.main:app --reload --port 8000       # Swagger: http://localhost:8000/docs
```

Para crear el usuario COMPANY:

```bash
python -m app.cli create-company --email admin@empresa.com
```

Con Docker: `docker build --target runtime -t timeclock-backend .`. El contenedor aplica las migraciones al iniciar.

## Base de datos y catálogos

- La estructura la crean las migraciones de `alembic/versions/` (`alembic upgrade head`); los modelos de `app/models/` deben coincidir con ellas (lo verifica `scripts/quality.sh --postgres`).
- Todas las listas de valores son tablas del esquema `catalog` con llave foránea desde cada columna que guarda un código. Sus registros están en `alembic/seed/catalogs.json`: la migración `0020` los carga y las pruebas usan el mismo archivo (`app/models/catalog_seed.py`).
- Cambiar un catálogo: edita `catalogs.json` y agrega una migración que lleve las bases existentes al mismo contenido. Un cambio de estructura: nueva migración y el modelo igual.

## Pruebas y calidad

```bash
pip install -r requirements-dev.txt
pytest --cov                    # SQLite + pipeline facial simulado; coverage mínimo 85 %
TEST_DATABASE_URL=postgresql+psycopg://u:p@localhost/timeclock_test pytest   # contra PostgreSQL real
python scripts/quality.py       # anomalías: tipado (mypy/ruff ANN), obsoletos, duplicidad,
                                # dependencias desactualizadas/vulnerables, coverage, marcadores
```

Sin Python local: `docker build --target dev -t time-clock-backend-dev .` y `docker run --rm -v "$PWD:/app" time-clock-backend-dev`.

## Capas

| Capa | Responsabilidad |
|---|---|
| `routers/` | Reciben la petición HTTP, inyectan dependencias y devuelven schemas. Sin lógica de negocio. |
| `services/` | Casos de uso: alta de empleado + QR, registro facial, verificación con prueba de vida. |
| `repositories/` | Consultas a la base de datos con SQLAlchemy. |
| `facial_recognition/` | `pipeline.py` (orquestación), `opencv_engine.py` (YuNet/SFace), `accessories.py` (CLIP), `pose.py`, `matcher.py`. |
| `core/` | Configuración centralizada, BD, `tokens.py` (JWT ES256/JWKS), `passwords.py` (Argon2 acotado), `responses.py` (contrato único), cifrado Fernet y errores. |

### Cambiar el motor de embeddings

Implementa `FaceEngine` (`detect`, `align`, `embed`) y úsalo en `get_face_pipeline()`.

Cada embedding guarda su `model_name`, así que los de otro modelo se ignoran. Al cambiar de motor, los empleados deben volver a registrar su rostro.

### Ajustar los prompts de accesorios

Edita `scripts/build_clip_prompts.py` y regenera `app/facial_recognition/data/clip_prompts.json`. Las instrucciones están en el propio script.
