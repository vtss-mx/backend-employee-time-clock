# Backend — FastAPI

API REST de Employee Time Clock. La documentación general (arquitectura, endpoints, ejemplos y seguridad) está en el [README principal](../README.md).

## Requisitos

- Python 3.12
- PostgreSQL 14 o superior (para pruebas se puede usar SQLite)

## Instalación y ejecución

```bash
python3.12 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
# TODA la configuración del backend vive en backend/.env, completa: cada variable con su valor (README principal, §7).
python scripts/generate_secrets.py > .env   # instalación nueva: el .env completo con secretos nuevos
python -m app.facial_recognition.model_store    # YuNet + SFace + CLIP (verificados con SHA-256)
alembic upgrade head
uvicorn app.main:app --reload --port 8000       # Swagger: http://localhost:8000/docs
```

Para crear el usuario COMPANY:

```bash
python -m app.cli create-company --email admin@empresa.com
```

Con Docker: `docker build --target runtime -t timeclock-backend .`. Un contenedor solo aplica las migraciones al iniciar (`RUN_MIGRATIONS=1`, por omisión); con varias réplicas (docker compose de la raíz) las aplica una sola vez el servicio `migrate` (`./entrypoint.sh migrate`) y las réplicas arrancan con `RUN_MIGRATIONS=0`, detrás del gateway y de PgBouncer (`pgbouncer/`). Ver "Escalar en cualquier momento" en el README principal.

## Base de datos y catálogos

- La estructura la crean las migraciones de `alembic/versions/` (`alembic upgrade head`); los modelos de `app/models/` deben coincidir con ellas (lo verifica `scripts/quality.sh --postgres`).
- Todas las listas de valores son tablas del esquema `catalog` con llave foránea desde cada columna que guarda un código. Sus registros están en `alembic/seed/catalogs.json`: la migración `0020` los carga y las pruebas usan el mismo archivo (`app/models/catalog_seed.py`).
- Cambiar un catálogo: edita `catalogs.json` (y su texto en inglés en `catalogs.en-US.json`) y agrega una migración que lleve las bases existentes al mismo contenido (también `catalog.translations`). Un cambio de estructura: nueva migración y el modelo igual.
- Idiomas (es-MX y en-US): los mensajes viven en `app/i18n/messages/` y los textos de los catálogos en inglés en `catalog.translations` (migración `0064`); cómo agregar uno en los dos idiomas y cómo revisar su ortografía (`cspell.json`): `AGENTS.md` §11.
- Aislamiento entre empresas y escala (README principal, "Aislamiento entre empresas", "Roles, privilegios y tiempos límite de la base", "Tablas particionadas por mes", "Parámetros de PostgreSQL", "Respaldos y restauración"; reglas en `AGENTS.md` §3.2-§3.4):
  - `python -m app.cli db roles` crea o pone al día los roles de mínimo privilegio (lo corre `./entrypoint.sh migrate` después de `alembic upgrade head`); la API se conecta con `DB_APP_USER`, sujeto a la seguridad por fila.
  - `python -m app.cli db backup` hace un respaldo verificado (`--loop`: el servicio `backup` de docker compose); `scripts/db_restore_check.sh <respaldo>` lo restaura en un PostgreSQL aislado y revisa que quedó completo.
  - `postgres/start.sh` es el entrypoint del servicio `db`: calcula los parámetros de PostgreSQL para el equipo.

## Pruebas y calidad

```bash
pip install -r requirements-dev.txt
pytest --cov                    # SQLite + pipeline facial simulado; coverage mínimo 85 %
TEST_DATABASE_URL=postgresql+psycopg://<dueño>@localhost/timeclock_test pytest   # PostgreSQL real: crea los roles y
                                # corre con el usuario de la API (seguridad por fila); ../scripts/quality.sh --postgres
                                # lo hace en un PostgreSQL aislado
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
