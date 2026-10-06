"""Configuración centralizada de la API.

Única fuente de configuración: variables de entorno / `backend/.env` (el único archivo de
configuración del backend). Decisión del dueño del producto: el `.env` tiene TODAS las variables con su
valor, a la vista; este módulo define cada una (tipo, valor por defecto, límites) y su comentario, y
`scripts/generate_secrets.py` arma el `.env` completo leyendo este archivo. Un campo nuevo lleva su
comentario (qué hace) y su línea en `.env` en el mismo cambio: `tests/test_env_files.py` falla si no.
Ningún otro módulo lee `os.environ` directamente.
"""

import base64
import re
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Literal
from urllib.parse import quote
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from cryptography.fernet import Fernet
from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

from app.core.system import default_api_workers

#: `backend/.env` sin depender del directorio actual. En Docker la API recibe las variables por
#: `env_file` de docker compose (la imagen no incluye el archivo; si falta, se ignora).
ENV_FILE = Path(__file__).resolve().parents[2] / ".env"
#: Carpeta raíz de un entorno dentro del bucket de imágenes (GCS_PREFIX).
_PREFIX = re.compile(r"[a-z0-9][a-z0-9._-]{0,39}")
#: Carpeta de la API (`backend-employee-time-clock/`, `/app` en la imagen): base de las rutas relativas de archivos.
API_DIR = ENV_FILE.parent


def _api_path(value: str) -> Path:
    path = Path(value)
    return path if path.is_absolute() else API_DIR / path


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=ENV_FILE, env_file_encoding="utf-8", extra="ignore")

    # --- Aplicación ---
    APP_NAME: str = "Employee Time Clock API"  # título de la documentación (/docs)
    # Entorno: development | production. En producción: HSTS y RATE_LIMIT_BACKEND=memory prohibido. También
    # nombra la carpeta del entorno en el bucket si GCS_PREFIX está vacío.
    ENVIRONMENT: str = "development"
    DOCS_ENABLED: bool = True  # Swagger en /docs, ReDoc en /redoc y /openapi.json
    API_PREFIX: str = "/api"  # prefijo de todas las rutas (el gateway y la webapp usan /api)
    LOG_LEVEL: str = "INFO"  # detalle del registro: DEBUG | INFO | WARNING | ERROR
    # Zona horaria del negocio: hora del Centro de México (UTC−6, sin horario de verano desde 2022).
    # En ella se cuentan los días ("hoy", edad mínima) y la webapp muestra todas las fechas y horas,
    # sin importar la zona del servidor ni la del dispositivo.
    APP_TIMEZONE: str = "America/Mexico_City"

    # --- Base de datos ---
    # DATABASE_URL tiene prioridad; si está vacía se construye con POSTGRES_* (las mismas
    # variables que usa el contenedor de PostgreSQL, así la contraseña se define una sola vez).
    DATABASE_URL: str = ""
    POSTGRES_HOST: str = "localhost"
    POSTGRES_PORT: int = 5432
    POSTGRES_DB: str = "timeclock"
    POSTGRES_USER: str = "timeclock"
    POSTGRES_PASSWORD: str = ""
    # Escalar: con varias réplicas la API habla con PostgreSQL a través de PgBouncer en modo transacción
    # (DB_POOLER=pgbouncer, POSTGRES_HOST/PORT apuntan a PgBouncer). En ese modo una conexión del servidor
    # cambia de cliente en cada transacción, así que la API no manda parámetros de sesión al conectar
    # (PgBouncer los rechaza: `statement_timeout` lo fija PgBouncer con su `connect_query`).
    DB_POOLER: Literal["none", "pgbouncer"] = "none"
    # Conexión DIRECTA a PostgreSQL (sin PgBouncer) para lo que necesita una sesión propia: las migraciones
    # (candado de sesión y `CREATE INDEX CONCURRENTLY`). DATABASE_DIRECT_URL tiene prioridad; si está vacía
    # se arma con POSTGRES_DIRECT_HOST/PORT y, sin host, es la misma DATABASE_URL (sin PgBouncer).
    DATABASE_DIRECT_URL: str = ""
    POSTGRES_DIRECT_HOST: str = ""
    POSTGRES_DIRECT_PORT: int = Field(default=5432, ge=1, le=65_535)

    # --- Roles de la base de datos (mínimo privilegio; README "Roles, privilegios y seguridad por fila") ---
    # POSTGRES_USER / POSTGRES_PASSWORD son el DUEÑO de la base (DDL): solo los usan las migraciones (servicio
    # `migrate`), el aprovisionamiento de roles (`python -m app.cli db roles`) y los respaldos. La API se conecta
    # con este usuario: solo SELECT/INSERT/UPDATE/DELETE en los esquemas de dominio, sin DDL, sin superusuario y
    # sujeto a la seguridad por fila. Contraseña vacía = la API usa al dueño (solo para desarrollo: el arranque lo
    # registra como error del sistema si así queda la seguridad por fila sin efecto).
    DB_APP_USER: str = "timeclock_app"
    DB_APP_PASSWORD: str = ""
    # Rol SIN login y con BYPASSRLS al que la API cambia (solo dentro de una transacción) en el código de la
    # plataforma que cruza empresas a propósito: ADMIN, autenticación, mantenimiento (app/core/row_security.py).
    DB_PLATFORM_ROLE: str = "timeclock_platform"
    # Usuario de solo lectura (consultas a mano, reportes): SELECT, sujeto a la seguridad por fila (para ver una
    # empresa: SET app.company_id = N). Contraseña vacía = no se crea.
    DB_READONLY_USER: str = "timeclock_readonly"
    DB_READONLY_PASSWORD: str = ""
    # Para el usuario de la API y el de solo lectura: espera máxima de un candado y de una transacción abierta
    # sin actividad (ms; 0 = sin límite). Una espera larga falla con su código en lugar de formar una fila de
    # peticiones detrás (y una transacción olvidada no frena al autovacuum ni retiene candados).
    DB_LOCK_TIMEOUT_MS: int = Field(default=10_000, ge=0, le=600_000)
    DB_IDLE_IN_TRANSACTION_TIMEOUT_MS: int = Field(default=120_000, ge=0, le=3_600_000)

    # --- JWT (tokens de acceso) ---
    # ES256 (ECDSA P-256, asimétrico): la API firma con la llave privada y cualquiera puede
    # verificar con la pública publicada en /api/auth/jwks. HS256 queda como alternativa simple.
    JWT_ALGORITHM: Literal["ES256", "HS256"] = "ES256"
    # Llave privada EC P-256 en PEM (con "\n" escapados o en base64). Requerida con ES256.
    JWT_PRIVATE_KEY: str = ""
    # Rotación: llaves anteriores (PEM privadas o públicas, separadas por "||") que aún
    # deben validar tokens emitidos antes del cambio.
    JWT_PREVIOUS_KEYS: str = ""
    # Secreto para HS256 (>= 32 caracteres).
    JWT_SECRET_KEY: str = ""
    # Emisor (iss) y destinatario (aud) de cada token: un token con otros valores se rechaza.
    JWT_ISSUER: str = "time-clock-api"
    JWT_AUDIENCE: str = "time-clock-web"
    # Duración de la sesión: el access token y la sesión vencen juntos (12 h desde el login).
    # Al vencer, el frontend envía al usuario al inicio de sesión.
    JWT_ACCESS_TTL_MINUTES: int = Field(default=720, ge=5, le=1440)  # 12 horas
    JWT_LEEWAY_SECONDS: int = Field(default=30, ge=0, le=300)  # tolerancia de reloj

    # --- Sesiones / refresh token (cookie HttpOnly, rotación con detección de reutilización) ---
    # El refresh token solo restaura la sesión al recargar la página: nunca la extiende más
    # allá de JWT_ACCESS_TTL_MINUTES desde el inicio de sesión.
    REFRESH_COOKIE_NAME: str = "tc_refresh"
    # auto = Secure solo si la petición llegó por HTTPS (las cookies Secure no se guardan en
    # http://IP-de-la-LAN). true/false fuerzan el valor.
    COOKIE_SECURE: Literal["auto", "true", "false"] = "auto"
    # Ventana en la que dos pestañas pueden refrescar a la vez sin considerarse robo.
    REFRESH_REUSE_GRACE_SECONDS: int = Field(default=30, ge=0, le=300)
    # 1 = una sola sesión activa por usuario: iniciar sesión en otro dispositivo cierra la anterior.
    MAX_SESSIONS_PER_USER: int = Field(default=1, ge=1, le=100)
    # "Recordar mi cuenta": cuánto tiempo el login de ese dispositivo muestra el correo ya escrito
    # (el dato vive en la BD; el dispositivo solo guarda una cookie HttpOnly opaca).
    REMEMBER_COOKIE_NAME: str = "tc_remember"
    REMEMBER_ACCOUNT_DAYS: int = Field(default=30, ge=1, le=365)

    # --- CORS ---
    # Orígenes que pueden llamar a la API desde otro dominio (la webapp detrás del gateway es del mismo origen
    # y no lo necesita; sí el servidor de desarrollo de Vite si llama directo a la API).
    CORS_ORIGINS: Annotated[list[str], NoDecode] = ["http://localhost:5173"]

    # --- Cifrado de datos sensibles (plantillas faciales e imágenes del bucket) ---
    # Llave Fernet que cifra las plantillas faciales y cada imagen o archivo del bucket. NO la pierdas: sin ella
    # nada de lo cifrado se puede leer (para cambiarla, la anterior va en DATA_ENCRYPTION_PREVIOUS_KEYS).
    DATA_ENCRYPTION_KEY: str
    # Llaves anteriores (separadas por comas) para LEER datos cifrados antes de rotar la llave.
    DATA_ENCRYPTION_PREVIOUS_KEYS: str = ""

    # --- Reconocimiento facial ---
    # Piso técnico de similitud coseno: ninguna configuración de la empresa puede aceptar por
    # debajo. El criterio que se ajusta desde el frontend es el NIVEL DE CONFIANZA (política,
    # 80 %-99.999 %); 0.38 deja disponible todo el rango (80 % ≈ 0.386, 99.999 % ≈ 0.653).
    # Ver app/facial_recognition/calibration.py.
    FACE_MATCH_THRESHOLD: float = Field(default=0.38, ge=0.0, le=1.0)
    # Identificación 1:N del validador: la persona identificada debe superar a la segunda más
    # parecida por al menos este margen de similitud (si dos personas se parecen, no se adivina).
    FACE_IDENTIFY_MARGIN: float = Field(default=0.05, ge=0.0, le=0.5)
    # Galerías faciales por empresa que cada proceso guarda en memoria (LRU) para identificar 1:N:
    # se acota por MEMORIA (FACE_GALLERY_CACHE_MB) y, además, por número de empresas.
    FACE_GALLERY_CACHE_COMPANIES: int = Field(default=512, ge=1, le=100_000)
    FACE_GALLERY_CACHE_MB: int = Field(default=256, ge=8, le=65_536)
    # Muestras nuevas que la galería en memoria descifra sin reconstruirse completa (con más, se
    # reconstruye): el aprendizaje la cambia seguido, una muestra a la vez.
    FACE_GALLERY_INCREMENTAL_LIMIT: int = Field(default=500, ge=1, le=100_000)
    # Empleados aprobados sin muestras del modelo actual que se migran por identificación 1:N.
    FACE_GALLERY_MIGRATION_BATCH: int = Field(default=10, ge=1, le=500)
    # fusion = SFace + FaceNet-512 (recomendado: 2.7x menos rechazos erróneos en LFW); sface = solo SFace.
    FACE_RECOGNITION_MODEL: Literal["fusion", "sface"] = "fusion"
    FACE_FUSION_SFACE_WEIGHT: float = Field(default=0.5, gt=0.0, lt=1.0)
    # Anti-spoofing pasivo (MiniFASNet). La empresa puede activarlo/desactivarlo desde el frontend;
    # este interruptor solo controla si el modelo se carga.
    FACE_ANTISPOOF_ENABLED: bool = True
    # En verificación se bloquea si la MAYORÍA de las capturas tiene probabilidad de rostro real
    # menor a este valor (en LFW, 1.8 % de rostros reales queda por debajo de 0.05 por captura).
    FACE_ANTISPOOF_THRESHOLD: float = Field(default=0.05, ge=0.0, le=1.0)

    # Modelos ONNX: carpeta (la imagen los trae en /app/models) y si se descargan (y verifican) al faltar.
    FACE_MODELS_DIR: str = "./models"
    FACE_MODELS_AUTO_DOWNLOAD: bool = True
    # Calidad de cada captura: confianza mínima del detector en la frontal; en giros y para ver si hay una
    # segunda persona (un rostro girado puntúa menos); lado mínimo del rostro (px); nitidez mínima (varianza del
    # Laplaciano) y brillo medio aceptado (0-255).
    FACE_DETECTION_MIN_SCORE: float = Field(default=0.80, ge=0.1, le=1.0)
    FACE_SECONDARY_DETECTION_SCORE: float = Field(default=0.60, ge=0.1, le=1.0)
    FACE_MIN_SIZE_PX: int = Field(default=80, ge=32)
    FACE_MIN_SHARPNESS: float = Field(default=20.0, ge=0.0)
    FACE_MIN_BRIGHTNESS: float = Field(default=40.0, ge=0.0, le=255.0)
    FACE_MAX_BRIGHTNESS: float = Field(default=225.0, ge=0.0, le=255.0)
    # Capturas del registro facial que se guardan como plantillas del empleado.
    FACE_MAX_SAMPLES_PER_EMPLOYEE: int = Field(default=5, ge=1, le=20)

    # --- Aprendizaje continuo: la galería de cada empleado mejora con el uso (face_learning) ---
    # Muestras aprendidas por empleado, además de las de su registro aprobado (que nunca se
    # reemplazan). 0 = no se aprende. Cada muestra son 2.5 KB en la galería en memoria del validador.
    FACE_LEARNING_MAX_SAMPLES: int = Field(default=5, ge=0, le=20)
    # Holgura sobre la similitud que exige la empresa para aprender de una captura: no se aprende de
    # identificaciones que pasaron justas.
    FACE_LEARNING_MARGIN: float = Field(default=0.05, ge=0.0, le=0.5)
    # Una captura así de parecida a una muestra que ya se tiene no enseña nada nuevo.
    FACE_LEARNING_REDUNDANCY: float = Field(default=0.95, gt=0.0, le=1.0)
    # Como máximo una muestra aprendida por empleado en este lapso: variedad de días, luz y cámaras.
    FACE_LEARNING_INTERVAL_HOURS: float = Field(default=12.0, ge=0.0, le=720.0)
    # Validador (1:N): ventaja mínima sobre la segunda persona más parecida para aprender.
    FACE_LEARNING_IDENTIFY_MARGIN: float = Field(default=0.10, ge=0.0, le=0.5)
    # Una muestra aprendida que lleva estos días sin ser la más parecida en ninguna identificación
    # la retira el mantenimiento: la galería se renueva con cómo luce hoy la persona (el registro
    # aprobado nunca se retira).
    FACE_LEARNING_STALE_DAYS: int = Field(default=180, ge=1, le=3650)

    # --- Captura facial: pose, registro y accesorios ---
    # Pose: el rostro debe estar de frente (ratio de giro ≈ 0.4·tan(ángulo)).
    FACE_MAX_YAW_RATIO: float = Field(default=0.15, gt=0.0, le=1.0)
    FACE_MAX_ROLL_DEGREES: float = Field(default=15.0, gt=0.0, le=45.0)  # inclinación lateral máxima

    # Registro: las muestras de un mismo registro deben ser consistentes entre sí.
    FACE_ENROLL_CONSISTENCY_THRESHOLD: float = Field(default=0.55, ge=0.0, le=1.0)
    # Registro con muchas fotos (decisión del dueño, 2026-10-06; `enrollment_selection`): fotos completas que acepta un
    # registro (propio o en persona; más es 422 TOO_MANY_IMAGES), fotos útiles mínimas para armar la plantilla (con
    # menos, 422 con el motivo de descarte más frecuente) y lo más que el registro espera (s) la parte que analizan los
    # workers de repuesto (lo que no terminen lo analiza el de la petición). La plantilla sigue siendo de
    # FACE_MAX_SAMPLES_PER_EMPLOYEE muestras: el servidor elige las mejores.
    FACE_ENROLL_MAX_PHOTOS: int = Field(default=36, ge=1, le=60)
    FACE_ENROLL_MIN_USABLE: int = Field(default=3, ge=1, le=36)
    FACE_ENROLL_SPARE_WAIT_SECONDS: float = Field(default=5.0, gt=0, le=60)

    # Accesorios (lentes, gorra/sombrero, cubrebocas) detectados con CLIP zero-shot.
    FACE_ACCESSORY_CHECK_ENABLED: bool = True
    FACE_GLASSES_THRESHOLD: float = Field(default=0.55, ge=0.0, le=1.0)
    FACE_HEADWEAR_THRESHOLD: float = Field(default=0.60, ge=0.0, le=1.0)
    FACE_MASK_THRESHOLD: float = Field(default=0.30, ge=0.0, le=1.0)
    # Verificación física del cubrebocas: se bloquea solo si la nariz/mejillas muestran menos de
    # esta fracción de piel (comparada con la frente). Si no se puede medir, CLIP debe superar
    # FACE_MASK_STRICT_THRESHOLD.
    FACE_MASK_MAX_SKIN_RATIO: float = Field(default=0.55, ge=0.0, le=1.0)
    FACE_MASK_STRICT_THRESHOLD: float = Field(default=0.90, ge=0.0, le=1.0)

    # --- Prueba de vida y destello de colores ---
    # Prueba de vida: reto aleatorio emitido por el servidor (girar, mirar arriba o abajo, acercarse).
    # La duración del reto y cuántos movimientos pide la decide cada empresa en su política. Estos
    # mínimos son el PISO: la autocalibración (face_security) solo puede subirlos, hasta su tope.
    FACE_LIVENESS_ENABLED: bool = True
    FACE_LIVENESS_MIN_YAW_RATIO: float = Field(default=0.18, gt=0.0, le=1.0)
    FACE_LIVENESS_MAX_YAW_RATIO: float = Field(default=0.28, gt=0.0, le=1.0)
    # Mirar arriba/abajo: cambio del pitch_ratio respecto a las frontales (≈ 13° con 0.08).
    FACE_LIVENESS_MIN_PITCH_DELTA: float = Field(default=0.08, gt=0.0, le=1.0)
    FACE_LIVENESS_MAX_PITCH_DELTA: float = Field(default=0.14, gt=0.0, le=1.0)
    # Acercarse: cuántas veces crece el ancho del rostro respecto a las frontales.
    FACE_LIVENESS_MIN_CLOSER_SCALE: float = Field(default=1.25, gt=1.0, le=3.0)
    FACE_LIVENESS_MAX_CLOSER_SCALE: float = Field(default=1.45, gt=1.0, le=3.0)
    # Similitud mínima entre cada movimiento y las frontales: debe ser la misma persona (si no, LIVENESS_MISMATCH).
    FACE_LIVENESS_CONSISTENCY_THRESHOLD: float = Field(default=0.30, ge=0.0, le=1.0)

    # Destello de colores (photometry): cuántos colores por reto, la respuesta mínima del rostro para
    # considerar la medición concluyente y, ya concluyente, qué tanto debe seguir los colores emitidos.
    # Con el modo OBSERVE solo se mide (la autocalibración usa esas mediciones).
    FACE_FLASH_COLORS: int = Field(default=3, ge=2, le=6)
    FACE_FLASH_MIN_MAGNITUDE: float = Field(default=0.004, ge=0.0, le=1.0)
    FACE_FLASH_MIN_SCORE: float = Field(default=0.35, ge=-1.0, le=1.0)
    FACE_FLASH_MAX_SCORE: float = Field(default=0.75, ge=-1.0, le=1.0)
    # Cociente rostro/fondo de la respuesta al destello (prototipo P1 de docs/rd/antifraude-identidad.md): un rostro
    # real, cerca de la pantalla, responde más que el fondo (lejos); una pantalla o un papel frente a la cámara
    # tiñen igual a los dos (≈ 1.0). Piso y tope de la autocalibración; solo decide con el destello obligatorio.
    FACE_FLASH_MIN_FACE_RATIO: float = Field(default=1.25, ge=1.0, le=20.0)
    FACE_FLASH_MAX_FACE_RATIO: float = Field(default=2.5, ge=1.0, le=50.0)
    # Tiempo humano mínimo por color del destello (se suma al de cada movimiento).
    FACE_FLASH_MIN_SECONDS: float = Field(default=0.15, ge=0.0, le=5.0)

    # --- Seguridad facial que se mejora sola ---
    # Autocalibración y refuerzo automático (face_security): la plataforma se mide y se endurece sola,
    # tenga o no la empresa el aprendizaje de la galería. Solo endurece (nunca baja de los mínimos).
    FACE_AUTOCALIBRATION_ENABLED: bool = True
    # Cada cuánto se recalcula (el mantenimiento lo hace en segundo plano).
    FACE_AUTOCALIBRATION_INTERVAL_HOURS: float = Field(default=6.0, gt=0.0, le=168.0)
    # Intentos exitosos mínimos para mover un umbral, los días que se miran y cuántos como máximo.
    FACE_AUTOCALIBRATION_MIN_SAMPLES: int = Field(default=300, ge=20, le=1_000_000)
    FACE_AUTOCALIBRATION_WINDOW_DAYS: int = Field(default=30, ge=1, le=365)
    FACE_AUTOCALIBRATION_MAX_SAMPLES: int = Field(default=20_000, ge=100, le=1_000_000)
    # El umbral queda en este percentil de las personas reales por el margen (2 % y 0.85: casi todas
    # pasan con holgura y quien imita apenas el movimiento ya no).
    FACE_AUTOCALIBRATION_PERCENTILE: float = Field(default=0.02, gt=0.0, lt=0.5)
    FACE_AUTOCALIBRATION_MARGIN: float = Field(default=0.85, gt=0.0, le=1.0)
    # Tope de la probabilidad de rostro real que la autocalibración puede exigir (nivel Máximo).
    FACE_AUTOCALIBRATION_MAX_REAL: float = Field(default=0.5, ge=0.0, le=1.0)
    # Segundos que cada proceso conserva en memoria los umbrales vigentes antes de releerlos de la BD (lo que
    # tarda un umbral recalibrado en aplicarse en todas las réplicas).
    FACE_THRESHOLDS_CACHE_SECONDS: float = Field(default=60.0, ge=0, le=3600)
    # Refuerzo: con tantos intentos sospechosos contra una empresa en la ventana, sus retos piden el
    # máximo de movimientos hasta que la ventana quede limpia.
    FACE_ESCALATION_MIN_ATTACKS: int = Field(default=5, ge=1, le=1000)
    FACE_ESCALATION_WINDOW_MINUTES: int = Field(default=60, ge=1, le=10_080)
    # Días que se guardan los números de cada intento facial.
    FACE_METRICS_RETENTION_DAYS: int = Field(default=90, ge=7, le=730)

    # --- Protección contra engaños: parámetros técnicos de los candados ---
    # Cada empresa activa o desactiva cada candado en su política de verificación
    # (tenancy.verification_policy); aquí solo se ajusta cómo mide cada uno.
    # Diferencia mínima (niveles de gris 0-255) entre capturas de un mismo intento: una persona
    # real nunca da dos fotogramas iguales (ruido del sensor, respiración); una foto fija sí.
    FACE_STATIC_MIN_DIFFERENCE: float = Field(default=0.6, ge=0.0, le=50.0)
    # Segundos mínimos por movimiento entre el reto y su captura: menos es imposible para una persona.
    FACE_CHALLENGE_MIN_SECONDS: float = Field(default=0.8, ge=0.0, le=30.0)
    # Días que se recuerda la huella de cada captura para rechazar su reenvío.
    FACE_REPLAY_RETENTION_DAYS: int = Field(default=30, ge=1, le=365)
    # Reenvío perceptual (prototipo P2): una captura de OTRO intento del mismo empleado a lo más a tantos bits de
    # pHash (rostro alineado, 64 bits) y con al menos esa similitud de embedding es la misma imagen modificada
    # (recomprimida, con brillo o recortada). Se compara con sus capturas de los últimos FACE_REPLAY_RETENTION_DAYS
    # días (hasta FACE_PERCEPTUAL_MAX_CAPTURES, las más recientes).
    FACE_PHASH_MAX_DISTANCE: int = Field(default=8, ge=0, le=32)
    FACE_PERCEPTUAL_MIN_COSINE: float = Field(default=0.95, ge=0.5, le=1.0)
    FACE_PERCEPTUAL_MAX_CAPTURES: int = Field(default=60, ge=1, le=1000)
    # Empleados aprobados más parecidos que se muestran al revisar un registro marcado como posible duplicado.
    FACE_DUPLICATE_SIMILAR_SHOWN: int = Field(default=3, ge=1, le=10)
    # Continuidad entre capturas frontales y la del giro (misma toma): cambio máximo del tamaño del
    # rostro (proporción) y desplazamiento de su centro (en anchos de rostro).
    FACE_CONTINUITY_MAX_SCALE: float = Field(default=1.8, ge=1.0, le=5.0)
    FACE_CONTINUITY_MAX_SHIFT: float = Field(default=1.2, ge=0.1, le=5.0)
    # Cambio máximo de luz (brillo medio del rostro, 0-255) entre la frontal y el giro: una imagen
    # de otra toma suele venir con otra iluminación.
    FACE_CONTINUITY_MAX_BRIGHTNESS_DELTA: float = Field(default=70.0, ge=5.0, le=255.0)
    # Cámaras virtuales (programas que fingen ser una cámara para inyectar video), por su nombre.
    FACE_BLOCKED_CAMERAS: Annotated[list[str], NoDecode] = [
        "virtual",
        "manycam",
        "xsplit",
        "snap camera",
        "e2esoft",
        "vcam",
        "splitcam",
        "camtwist",
        "mmhmm",
        "youcam",
        "chromacam",
        "droidcam",
        "iriun",
        "epoccam",
        "camo",
        "nvidia broadcast",
    ]

    # --- Motor de riesgo y casos de fraude (docs/rd/antifraude-identidad.md §3-§5) ---
    # Puntos máximos que suma una familia de señales (presentación, inyección, lugar...): una sola fuente
    # ruidosa no decide sola.
    RISK_FAMILY_MAX_POINTS: int = Field(default=60, ge=1, le=100)
    # MATCH_MARGIN_LOW: la similitud promedio quedó a menos de este margen de la que exige la empresa.
    RISK_MATCH_MARGIN: float = Field(default=0.03, ge=0.0, le=0.5)
    # SPOOF_PROB_LOW: la probabilidad de rostro real quedó por debajo de este múltiplo del umbral de la empresa.
    RISK_SPOOF_LOW_FACTOR: float = Field(default=2.0, ge=1.0, le=20.0)
    # LOCATION_EDGE: el registro quedó más allá de esta fracción del radio del sitio (siempre en el límite).
    RISK_LOCATION_EDGE_RATIO: float = Field(default=0.9, gt=0.0, le=1.0)
    # LOCATION_JUMP: entre dos registros consecutivos del empleado, una velocidad mayor a esta fracción de la
    # velocidad creíble de su política (max_travel_kmh). El rechazo por viaje imposible sigue con la política completa.
    RISK_LOCATION_JUMP_RATIO: float = Field(default=0.5, gt=0.0, le=1.0)
    # LOCATION_STATIC: lecturas idénticas (coordenadas y precisión) en al menos estas muestras de un registro.
    RISK_LOCATION_STATIC_MIN_SAMPLES: int = Field(default=3, ge=2, le=50)
    # Muestras de ubicación que acepta un registro (la app toma varias en una ventana corta); más: 422.
    LOCATION_MAX_SAMPLES: int = Field(default=10, ge=1, le=50)
    # DEVICE_SHARED: el mismo dispositivo (su llave) lo usaron al menos estos empleados de la empresa, contando al que
    # checa, dentro de esta ventana (minutos): un teléfono que checa por varios compañeros.
    RISK_DEVICE_SHARED_MIN_EMPLOYEES: int = Field(default=2, ge=2, le=50)
    RISK_DEVICE_SHARED_WINDOW_MINUTES: int = Field(default=60, ge=1, le=10_080)
    # IDENTITY_MISMATCH (1:N en cada 1:1): el rostro se parece a OTRO empleado de la empresa al menos tanto como al
    # que se verifica, más este margen de similitud. La galería en memoria se usa sin revalidarla contra la base
    # durante estos segundos (0 = en cada uso) y solo si cabe en su caché (FACE_GALLERY_CACHE_MB).
    RISK_IDENTITY_MISMATCH_MARGIN: float = Field(default=0.0, ge=-0.2, le=0.5)
    RISK_IDENTITY_GALLERY_MAX_AGE_SECONDS: int = Field(default=300, ge=0, le=86_400)
    # NETWORK_JUMP: la IP cambió de país, o de sistema autónomo, respecto del registro anterior en menos de estos
    # minutos (un cambio de red celular a Wi-Fi tarda; una VPN que se enciende, no).
    RISK_NETWORK_COUNTRY_JUMP_MINUTES: int = Field(default=120, ge=1, le=10_080)
    RISK_NETWORK_ASN_JUMP_MINUTES: int = Field(default=10, ge=1, le=1440)
    # FRAME_TIMING_SYNTHETIC: el intervalo entre fotogramas (requestVideoFrameCallback) varía menos que este
    # coeficiente de variación con al menos estos fotogramas medidos (una cámara virtual o un video son exactos).
    RISK_FRAME_TIMING_MIN_CV: float = Field(default=0.01, ge=0.0, le=1.0)
    RISK_FRAME_TIMING_MIN_FRAMES: int = Field(default=20, ge=2, le=1000)
    # Telemetría que la app manda con las capturas (JSON): tamaño máximo en bytes. Más grande o mal formada cuenta
    # como ausente (señal TELEMETRY_MISSING), nunca como un rechazo.
    CAPTURE_TELEMETRY_MAX_BYTES: int = Field(default=4096, ge=256, le=65_536)
    # JPEG_TABLE_UNKNOWN: calidad JPEG con que la app captura (`useCamera.captureFrame`: canvas.toBlob con 0.92, que
    # en Chrome, Edge, Firefox y Android son las tablas IJG de calidad 92).
    FACE_CAPTURE_JPEG_QUALITY: int = Field(default=92, ge=1, le=100)

    # --- Protocolo de captura de frontera (antifraude fase 2a; docs/rd §2.2-§2.3, decisión D11) ---
    # Ráfaga corta de recortes del rostro que la app manda con las capturas, en UNA hoja JPEG (el servidor se la pide
    # en el reto): lado de cada recorte (px, múltiplo de 16 como la cuadrícula del JPEG), recortes del tramo quieto
    # (de frente) y del de movimiento (el primer giro o cabeceo), cuadros por segundo, calidad JPEG de la hoja (0-1) y
    # cuánto más grande que el rostro es la zona que se recorta. Con 160 px, 26 + 10 recortes y 0.85 pesa ≈ 0.21 MB
    # (36 fotos ligeras; decisión del dueño, 2026-10-06: el rostro queda de ≈100 px, la resolución de LFW).
    FACE_BURST_TILE_PX: int = Field(default=160, ge=96, le=160, multiple_of=16)
    FACE_BURST_HOLD_FRAMES: int = Field(default=26, ge=4, le=40)
    FACE_BURST_MOVE_FRAMES: int = Field(default=10, ge=0, le=20)
    FACE_BURST_FPS: int = Field(default=10, ge=4, le=30)
    FACE_BURST_JPEG_QUALITY: float = Field(default=0.85, ge=0.5, le=0.95)
    FACE_BURST_MARGIN: float = Field(default=1.6, ge=1.2, le=3.0)
    # La hoja se valida estricta (lo que no cumple es la señal BURST_MISSING, nunca un error): tamaño máximo (MB),
    # recortes mínimos para analizarla, separación mínima entre recortes (ms) y duración máxima de cada tramo (ms). El
    # tope deja 2.4 veces la hoja medida (0.21 MB con fotogramas reales).
    FACE_BURST_MAX_MB: float = Field(default=0.5, gt=0, le=4)
    FACE_BURST_MIN_FRAMES: int = Field(default=6, ge=3, le=40)
    FACE_BURST_MIN_INTERVAL_MS: int = Field(default=25, ge=1, le=1000)
    FACE_BURST_MAX_SPAN_MS: int = Field(default=8000, ge=1000, le=60_000)
    # Con un worker facial libre, la hoja se analiza en OTRO núcleo mientras la petición sigue con el destello y los
    # movimientos: lo más que el intento espera ese resultado (s). Si no llega, el intento sigue sin medirla y queda
    # registrado; sin un worker libre se analiza con el de la petición, como antes.
    FACE_BURST_WAIT_SECONDS: float = Field(default=5.0, gt=0, le=60)
    # Continuidad (BURST_DISCONTINUOUS): fracción de recortes sin un único rostro que se tolera y salto máximo de los
    # puntos entre dos recortes seguidos (en distancias entre ojos): más es un montaje de fuentes distintas.
    FACE_BURST_MAX_FACELESS: float = Field(default=0.25, ge=0, le=1)
    FACE_BURST_MAX_JUMP: float = Field(default=0.8, gt=0, le=5)
    # Consenso de la ráfaga (SOLO ENDURECE; decisión del dueño, 2026-10-06): de los recortes quietos con un solo
    # rostro se toman estos (los de mayor confianza del detector; 0 = apagado) y se comparan con las muestras de la
    # persona; con menos de FACE_CONSENSUS_MIN_FRAMES no se juzga. La mediana de su parecido debe llegar a lo que exige
    # la empresa menos FACE_CONSENSUS_MARGIN (medido: un recorte de 160 px se parece 0.03 menos que otra frontal del
    # mismo rostro, 0.04 en el peor caso; el margen deja más del doble); si no, el intento es "Rostro no reconocido"
    # aunque las frontales coincidan (inyectadas).
    FACE_CONSENSUS_FRAMES: int = Field(default=7, ge=0, le=20)
    FACE_CONSENSUS_MIN_FRAMES: int = Field(default=4, ge=1, le=20)
    FACE_CONSENSUS_MARGIN: float = Field(default=0.10, ge=0, le=0.5)
    # Micromovimiento (BURST_FROZEN): piso y tope del umbral autocalibrado (diferencia media entre recortes seguidos,
    # niveles de gris 0-255; un video congelado da 0). Dos recortes no contiguos a menos de FACE_BURST_IDENTICAL niveles
    # son el mismo fotograma: un video en bucle (BURST_LOOP).
    FACE_BURST_MIN_MOTION: float = Field(default=0.3, ge=0, le=20)
    FACE_BURST_MAX_MOTION: float = Field(default=1.5, ge=0, le=50)
    FACE_BURST_IDENTICAL: float = Field(default=0.05, ge=0, le=5)
    # Pulso por video (rPPG; PULSE_ABSENT solo se mide, nunca decide): tramo quieto mínimo (s) para medirlo, frecuencia
    # a la que se lleva la serie (Hz), banda cardiaca (Hz: 42-180 latidos/min) y SNR mínima (dB) para darlo por visto.
    FACE_PULSE_MIN_SECONDS: float = Field(default=2.0, ge=1, le=30)
    FACE_PULSE_RATE_HZ: float = Field(default=15.0, ge=5, le=60)
    FACE_PULSE_MIN_HZ: float = Field(default=0.7, gt=0, le=5)
    FACE_PULSE_MAX_HZ: float = Field(default=3.0, gt=0, le=10)
    FACE_PULSE_MIN_SNR: float = Field(default=-1.0, ge=-30, le=30)
    # Moiré (MOIRE_HIGH, en las frontales a resolución completa): el máximo de partida (dB) y lo más estricto a que lo
    # puede llevar la autocalibración (solo baja: un umbral máximo se endurece bajándolo).
    FACE_MOIRE_MAX_DB: float = Field(default=20.0, ge=0, le=60)
    FACE_MOIRE_TIGHTEST_DB: float = Field(default=14.0, ge=0, le=60)
    # Ruido del sensor (NOISE_MISMATCH, en las frontales): piso y tope del cociente rostro/fondo autocalibrado y el
    # ruido mínimo del fondo (σ, niveles 0-255) para medirlo (un fondo saturado o plano no dice nada).
    FACE_NOISE_MIN_RATIO: float = Field(default=0.4, ge=0, le=5)
    FACE_NOISE_MAX_RATIO: float = Field(default=0.85, ge=0, le=5)
    FACE_NOISE_MIN_BACKGROUND: float = Field(default=0.2, ge=0, le=20)
    # Perspectiva (PERSPECTIVE_FLAT): piso y tope del paralaje autocalibrado (residuo de la nariz al girar o cabecear,
    # en distancias entre ojos; prototipo: un rostro real 0.10-0.19, una foto plana ≤ 0.02).
    FACE_PARALLAX_MIN: float = Field(default=0.05, ge=0, le=1)
    FACE_PARALLAX_MAX: float = Field(default=0.10, ge=0, le=1)
    # Destello dictado por el servidor (por el canal en vivo): ventana para responder cada color (ms, desde que el
    # servidor lo revela hasta que llega la huella de su captura) y lo mínimo creíble (la app espera ≥ 340 ms con cada
    # color, WCAG 2.3.1: nunca más de 3 destellos por segundo).
    FACE_FLASH_PACE_WINDOW_MS: int = Field(default=2000, ge=300, le=10_000)
    FACE_FLASH_PACE_MIN_MS: int = Field(default=250, ge=0, le=5000)

    # --- Base local de IP: DB-IP Lite (CC BY 4.0; decisión D8, la IP nunca sale del servidor) ---
    # Archivos MMDB de país y de sistema autónomo (relativos a la carpeta de la API o absolutos). Sin archivo o con
    # uno dañado, las señales de red no se miden: nunca es un error para la persona.
    IP_COUNTRY_DB_PATH: str = "data/ipdb/dbip-country-lite.mmdb"
    IP_ASN_DB_PATH: str = "data/ipdb/dbip-asn-lite.mmdb"
    # Actualización (mantenimiento de cada proceso, con candado de archivo): encendida, cada cuántos días se descarga
    # el archivo nuevo y cuántas horas se espera tras una falla antes de reintentar.
    IP_DB_REFRESH_ENABLED: bool = True
    IP_DB_REFRESH_DAYS: int = Field(default=30, ge=1, le=365)
    IP_DB_RETRY_HOURS: int = Field(default=12, ge=1, le=720)
    # De dónde se descarga cada archivo ({year} y {month} del mes en curso; si aún no se publica, el anterior) y la
    # página que publica su SHA-1 (el archivo descomprimido debe coincidir con ella o se descarta).
    IP_DB_COUNTRY_URL: str = "https://download.db-ip.com/free/dbip-country-lite-{year}-{month}.mmdb.gz"
    IP_DB_ASN_URL: str = "https://download.db-ip.com/free/dbip-asn-lite-{year}-{month}.mmdb.gz"
    IP_DB_COUNTRY_CHECKSUM_URL: str = "https://db-ip.com/db/download/ip-to-country-lite"
    IP_DB_ASN_CHECKSUM_URL: str = "https://db-ip.com/db/download/ip-to-asn-lite"
    # Tiempo límite de cada descarga (segundos) y tamaño máximo de cada archivo, comprimido o no (MB).
    IP_DB_DOWNLOAD_TIMEOUT_SECONDS: int = Field(default=120, ge=5, le=1800)
    IP_DB_MAX_MB: int = Field(default=200, ge=1, le=2048)
    # Cada cuántos segundos cada proceso revisa si otro ya dejó un archivo nuevo (y lo vuelve a abrir).
    IP_DB_RELOAD_CHECK_SECONDS: int = Field(default=300, ge=1, le=86_400)
    # NETWORK_HOSTING: sistemas autónomos de nubes y centros de datos (VPN, proxys, programas que llaman a la API) y
    # palabras de su organización que lo delatan (minúsculas).
    IP_HOSTING_ASNS: Annotated[list[int], NoDecode] = [
        16509,
        14618,
        396982,
        8075,
        14061,
        16276,
        24940,
        63949,
        20473,
        31898,
        45102,
        132203,
        51167,
        12876,
        60781,
        9009,
        60068,
        212238,
        40676,
        36352,
        53667,
        13335,
    ]
    IP_HOSTING_KEYWORDS: Annotated[list[str], NoDecode] = [
        "hosting",
        "datacenter",
        "data center",
        "colocation",
        "vps",
        "dedicated server",
        "cloud",
    ]
    # Simulación de una política ("¿qué habría pasado?"): días hacia atrás y tope de intentos que se leen.
    RISK_SIMULATION_DAYS: int = Field(default=30, ge=1, le=90)
    RISK_SIMULATION_MAX_ATTEMPTS: int = Field(default=20_000, ge=100, le=200_000)
    # Casos: intentos que se guardan por caso (el contador sigue) y días que se conservan los ya decididos.
    FRAUD_CASE_MAX_ATTEMPTS: int = Field(default=50, ge=1, le=1000)
    FRAUD_CASE_RETENTION_DAYS: int = Field(default=730, ge=30, le=3650)
    # Fotogramas de evidencia (decisión D1: solo de intentos sospechosos, cifrados en el bucket): cuántos por
    # intento, tope por caso y días que se conservan.
    FRAUD_EVIDENCE_FRAMES_PER_ATTEMPT: int = Field(default=3, ge=0, le=12)
    FRAUD_EVIDENCE_MAX_PER_CASE: int = Field(default=12, ge=0, le=200)
    FRAUD_EVIDENCE_RETENTION_DAYS: int = Field(default=90, ge=1, le=365)
    # Lista de bloqueo (huellas de ataques confirmados, decisión D6): días de vigencia, en cuántas empresas debe
    # confirmarse para valer en toda la plataforma, segundos que cada proceso la conserva en memoria y cuántas firmas
    # carga como máximo (las más antiguas primero; las demás esperan a que venzan otras).
    ATTACK_SIGNATURE_DAYS: int = Field(default=365, ge=1, le=3650)
    ATTACK_SIGNATURE_PLATFORM_COMPANIES: int = Field(default=2, ge=1, le=100)
    ATTACK_SIGNATURE_CACHE_SECONDS: float = Field(default=30.0, ge=0, le=3600)
    ATTACK_SIGNATURE_MAX_LOADED: int = Field(default=50_000, ge=100, le=1_000_000)
    # Regla de dos personas (decisión D12): un cambio que relaja la seguridad de una empresa espera la aprobación
    # de OTRO ADMIN hasta estas horas. Apagarla (false) solo tiene sentido en una plataforma con un único ADMIN.
    POLICY_TWO_PERSON_RULE: bool = True
    POLICY_CHANGE_APPROVAL_HOURS: int = Field(default=72, ge=1, le=720)

    # --- Tolerancia a fallas ---
    # Conexiones persistentes: las de "overflow" se abren y cierran en cada uso (autenticación
    # SCRAM incluida), lo que bajo carga es mucho más lento que esperar una del pool.
    # Valores POR PROCESO: cada réplica abre hasta API_WORKERS x (DB_POOL_SIZE + DB_MAX_OVERFLOW)
    # conexiones. Sin PgBouncer, la suma de todas las réplicas debe caber en max_connections de PostgreSQL;
    # con PgBouncer debe caber en su max_client_conn (README, "Escalar en cualquier momento").
    DB_POOL_SIZE: int = Field(default=20, ge=1)
    DB_MAX_OVERFLOW: int = Field(default=5, ge=0)
    # Tiempos límite: esperar una conexión libre del pool, abrir una conexión nueva y cada sentencia SQL
    # (DB_CONNECT_TIMEOUT_SECONDS y DB_STATEMENT_TIMEOUT_MS también los aplica PgBouncer).
    DB_POOL_TIMEOUT_SECONDS: int = Field(default=10, ge=1)
    DB_CONNECT_TIMEOUT_SECONDS: int = Field(default=5, ge=1)
    DB_STATEMENT_TIMEOUT_MS: int = Field(default=15000, ge=1000)
    # Una conexión del pool se renueva tras estos segundos: no se usa una que un firewall o la red cortaron
    # sin avisar (además de pool_pre_ping).
    DB_POOL_RECYCLE_SECONDS: int = Field(default=1800, ge=60, le=86_400)
    DB_STARTUP_RETRIES: int = Field(default=30, ge=1)  # intentos (cada 2 s) de conectar al arrancar
    # Cola del procesamiento facial (intensivo en CPU): 1 worker por núcleo.
    FACE_WORKERS: int = Field(default=0, ge=0, le=128)  # 0 = automático (núcleos / API_WORKERS)
    FACE_QUEUE_MAX_WAITING: int = Field(default=0, ge=0)  # 0 = automático (workers x 4)
    FACE_QUEUE_TIMEOUT_SECONDS: float = Field(default=15.0, gt=0)  # espera máxima en la cola facial → 503
    # --- Capacidad / alta concurrencia ---
    # Procesos de la API (uvicorn --workers). Lo fija entrypoint.sh (auto: núcleos / 2, máx. 4).
    # Cada proceso tiene su propio pool de BD, threadpool y FACE_WORKERS = núcleos / procesos.
    API_WORKERS: int = Field(default=0, ge=0, le=64)  # 0 = automático
    # Peticiones procesándose a la vez en la API. Las que exceden esperan hasta
    # REQUEST_QUEUE_TIMEOUT_SECONDS y después reciben 503 SERVER_BUSY (reintentable).
    MAX_CONCURRENT_REQUESTS: int = Field(default=100, ge=1)  # por proceso: techo del límite adaptativo
    REQUEST_QUEUE_TIMEOUT_SECONDS: float = Field(default=10.0, gt=0)
    # Control de admisión adaptativo (app/core/admission.py): el límite se mueve solo entre
    # MIN_CONCURRENT_REQUESTS y MAX_CONCURRENT_REQUESTS según la latencia real; la fila atiende
    # primero lo crítico y lo más demandado. REQUEST_QUEUE_MAX = 0: el doble del límite vigente.
    # Piso alto a propósito: recortar de más baja lo que se atiende sin aliviar nada. Medido con
    # perf/run.sh a 1 000 pet/s (4 procesos): piso 32 → ~600 respuestas 200/s; piso 25 (el tamaño del
    # pool) → ~200/s: con el límite al mínimo, rechazar consume el CPU que dejaría de usar lo admitido.
    MIN_CONCURRENT_REQUESTS: int = Field(default=32, ge=1)
    REQUEST_QUEUE_MAX: int = Field(default=0, ge=0)  # peticiones en espera; 0 = el doble del límite vigente
    # Vida media de la demanda reciente de cada API (s): qué tan rápido "olvida" un pico.
    ADMISSION_DEMAND_HALF_LIFE_SECONDS: float = Field(default=60.0, gt=0)
    # Argon2id usa ~64 MB por hash: se limita cuántos se calculan a la vez (0 = núcleos).
    PASSWORD_HASH_CONCURRENCY: int = Field(default=0, ge=0)
    PASSWORD_HASH_WAIT_SECONDS: float = Field(default=10.0, gt=0)  # espera máxima de un turno de Argon2 → 503
    # Hilos para endpoints síncronos (0 = automático: workers faciales + cola + pool de BD).
    THREADPOOL_SIZE: int = Field(default=0, ge=0)
    # Tras un fallo al cargar los modelos, esperar antes de reintentar (circuit breaker).
    FACE_ENGINE_RETRY_SECONDS: int = Field(default=30, ge=1)
    # Apagado ordenado (escalar hacia abajo, desplegar o reiniciar una réplica sin cortar a nadie): al
    # recibir SIGTERM el proceso sigue atendiendo estos segundos pero su readiness responde 503
    # `SHUTTING_DOWN` (el balanceador deja de mandarle peticiones) y cada respuesta cierra su conexión
    # (el proxy no reutiliza una que está por cerrarse). Después uvicorn deja de aceptar conexiones y
    # termina lo que lleva (`--timeout-graceful-shutdown`). 0 = apagar de inmediato.
    SHUTDOWN_DRAIN_SECONDS: float = Field(default=5.0, ge=0, le=120)

    # --- Tiempo real (WebSocket de validación) ---
    WS_MAX_CONNECTIONS: int = Field(default=500, ge=1)  # por proceso; el exceso se cierra con 1013
    WS_AUTH_TIMEOUT_SECONDS: float = Field(default=10.0, gt=0)  # tiempo para enviar el mensaje "auth"
    WS_IDLE_TIMEOUT_SECONDS: float = Field(default=300.0, gt=0)  # sin mensajes → se cierra
    WS_MAX_MESSAGES_PER_10S: int = Field(default=60, ge=1)  # por conexión
    WS_MAX_MESSAGE_BYTES: int = Field(default=4096, ge=256)
    # Validaciones simultáneas del canal por proceso (cada una usa una conexión de la BD): menos
    # que DB_POOL_SIZE para que la API HTTP siempre tenga conexiones libres.
    WS_MAX_CONCURRENT_VALIDATIONS: int = Field(default=8, ge=1)

    # --- Imágenes ---
    # Cada captura facial que se recibe: tamaño máximo del archivo (MB) y lado mayor máximo y menor mínimo (px).
    MAX_IMAGE_SIZE_MB: float = Field(default=5.0, gt=0, le=20)
    MAX_IMAGE_DIMENSION: int = Field(default=4096, ge=640)
    MIN_IMAGE_DIMENSION: int = Field(default=160, ge=64)

    # --- QR dinámico (un solo uso; su vigencia la decide cada empresa: qr_lifetime_seconds) ---
    # QR + rostro: segundos para confirmar el rostro tras escanear el QR (el QR ya quedó usado).
    QR_FACE_WINDOW_SECONDS: int = Field(default=120, ge=30, le=600)
    # Los QR vencidos se depuran tras estos días (un QR depurado tampoco se acepta: no existe).
    QR_TOKEN_RETENTION_DAYS: int = Field(default=30, ge=1)
    # Códigos nuevos por empleado y minuto (rotación automática + "Generar otro").
    RATE_LIMIT_QR_PER_MINUTE: int = Field(default=30, ge=1)

    # --- Ubicación de los validadores (inicio de sesión solo dentro del radio permitido) ---
    # Precisión mínima exigida al GPS del dispositivo (m): con una lectura más imprecisa no se puede
    # asegurar que esté dentro del radio y se pide activar la ubicación precisa.
    VALIDATOR_LOCATION_MAX_ACCURACY_M: int = Field(default=200, ge=10, le=5000)
    # Margen por la imprecisión del GPS (m): se descuenta de la distancia, hasta este máximo.
    VALIDATOR_LOCATION_TOLERANCE_M: int = Field(default=50, ge=0, le=1000)

    # --- Antifraude 2b: código de sitio y su kiosco (decisión D9; app/services/site_codes.py) ---
    # Cada cuántos segundos cambia el código de un sitio (como TOTP): más corto deja menos tiempo para pasárselo a
    # alguien que no está en el sitio; más largo, más tiempo para escribirlo.
    SITE_CODE_PERIOD_SECONDS: int = Field(default=30, ge=10, le=300)
    # Periodos anteriores que todavía se aceptan (el tiempo de leer el código y escribirlo); el siguiente también se
    # acepta por la diferencia de reloj entre réplicas.
    SITE_CODE_GRACE_WINDOWS: int = Field(default=1, ge=0, le=10)
    # Horas que vale el código para vincular la tableta de un kiosco (de un solo uso).
    SITE_KIOSK_PAIRING_HOURS: int = Field(default=24, ge=1, le=168)
    # Cada cuántos segundos, como mucho, se anota la última vez que un kiosco pidió su código (una escritura cada
    # tanto en lugar de una por consulta; la empresa ve si su tableta sigue encendida).
    SITE_KIOSK_SEEN_SECONDS: int = Field(default=300, ge=10, le=86_400)

    # --- Listados paginados ---
    # Elementos por página si el cliente no indica `size`, y el máximo que acepta (el paginador
    # de la webapp ofrece 10, 20, 30, 40 y 50).
    PAGE_SIZE_DEFAULT: int = Field(default=10, ge=1, le=100)
    PAGE_SIZE_MAX: int = Field(default=50, ge=1, le=100)
    # Catálogos (esquema catalog): segundos que cada proceso los conserva en memoria antes de releerlos.
    CATALOG_CACHE_SECONDS: float = Field(default=60, ge=0, le=3600)
    # Política de verificación de cada empresa en memoria de cada proceso (se lee en cada operación facial):
    # segundos antes de releerla (un cambio del ADMIN se ve en todas las réplicas en ese tiempo) y cuántas
    # empresas caben (LRU).
    POLICY_CACHE_SECONDS: float = Field(default=5.0, ge=0, le=300)
    POLICY_CACHE_COMPANIES: int = Field(default=10_000, ge=1, le=1_000_000)

    # --- Protección contra abuso (peticiones por minuto) ---
    # memory = por proceso; database = compartido por todos los procesos/instancias (PostgreSQL).
    RATE_LIMIT_BACKEND: Literal["memory", "database"] = "database"
    RATE_LIMIT_REFRESH_PER_MINUTE: int = Field(default=30, ge=1)  # renovaciones por sesión
    RATE_LIMIT_LOGIN_PER_MINUTE: int = Field(default=10, ge=1)  # intentos por cuenta
    # Por IP es alto a propósito: oficinas enteras salen a Internet con una sola IP (NAT);
    # la protección contra fuerza bruta real es el límite por cuenta.
    RATE_LIMIT_LOGIN_IP_PER_MINUTE: int = Field(default=600, ge=1)
    RATE_LIMIT_VERIFY_PER_MINUTE: int = Field(default=30, ge=1)  # verificaciones faciales por usuario
    # Un validador atiende a muchos empleados seguidos desde el mismo dispositivo.
    RATE_LIMIT_CHECKPOINT_PER_MINUTE: int = Field(default=120, ge=1)
    # Kioscos de los sitios (públicos: los autentica la llave de su tableta): vincular y pedir el código, por IP y
    # minuto. Un kiosco pide un código por periodo; varias tabletas detrás de la misma IP caben con holgura.
    RATE_LIMIT_KIOSK_PER_MINUTE: int = Field(default=120, ge=1)
    # Revisiones previas de una captura (POST /face/check: calidad, pose y accesorios) por usuario.
    RATE_LIMIT_FACE_CHECK_PER_MINUTE: int = Field(default=60, ge=1)
    # Validación en vivo por HTTP (respaldo del WebSocket): consultas por usuario y minuto.
    RATE_LIMIT_VALIDATION_PER_MINUTE: int = Field(default=120, ge=1)
    # API de integración: peticiones por llave y minuto.
    RATE_LIMIT_API_KEY_PER_MINUTE: int = Field(default=120, ge=1)
    # Fallas de la aplicación web que reporta el navegador (público): reportes por IP y minuto. La app
    # no repite el mismo reporte en una carga de la página; esto frena a quien lo use para inundar.
    RATE_LIMIT_CLIENT_ERRORS_PER_MINUTE: int = Field(default=20, ge=1)
    # Lotes de rendimiento que manda el navegador (POST /api/telemetry/web): por sesión con un token válido y, sin
    # él (solo la pantalla de inicio de sesión), por IP. La app manda uno cada 30 s y otro al ocultarse la pestaña.
    RATE_LIMIT_WEB_PERF_PER_MINUTE: int = Field(default=12, ge=1)
    RATE_LIMIT_WEB_PERF_ANONYMOUS_PER_MINUTE: int = Field(default=4, ge=1)

    # --- Registro de errores del sistema (ops.error_reports, pantalla "Errores del sistema") ---
    # Se acumulan en memoria y un hilo los guarda en lotes cada ERROR_REPORT_FLUSH_SECONDS (0 = solo
    # al apagar o a mano: pruebas). Nunca frenan una petición.
    ERROR_REPORT_FLUSH_SECONDS: float = Field(default=2.0, ge=0)
    ERROR_REPORT_BUFFER: int = Field(default=10_000, ge=100, le=1_000_000)  # errores en memoria entre lotes
    # Ocurrencias que se guardan por error en cada vuelta (el total siempre se cuenta).
    ERROR_REPORT_OCCURRENCES_PER_FLUSH: int = Field(default=5, ge=1, le=100)
    # Días que se conserva el detalle de cada ocurrencia (contexto de la petición).
    ERROR_OCCURRENCE_RETENTION_DAYS: int = Field(default=30, ge=1, le=3650)
    # Un error SOLUCIONADO que no ha vuelto a ocurrir en este tiempo se depura (si vuelve, se crea
    # de nuevo como pendiente): la bandeja no crece sin fin.
    ERROR_RESOLVED_RETENTION_DAYS: int = Field(default=180, ge=1, le=3650)

    # --- Mantenimiento (depuración de lo vencido, fuera de las peticiones) ---
    # Cada cuántos segundos depura cada instancia (solo una a la vez trabaja); 0 = desactivado
    # (p. ej. si lo ejecuta un cron externo con `python -m app.cli purge`).
    MAINTENANCE_INTERVAL_SECONDS: int = Field(default=300, ge=0)
    # Filas que se borran por sentencia y tope de lotes por tabla en cada vuelta (el resto sigue en la
    # siguiente: una tabla muy atrasada no acapara la vuelta).
    MAINTENANCE_BATCH_SIZE: int = Field(default=5000, ge=100, le=100_000)
    MAINTENANCE_MAX_BATCHES_PER_TABLE: int = Field(default=200, ge=1, le=10_000)
    # Borrado lógico («Eliminados», regla 20 de la raíz; app/core/soft_delete.py): días que un registro eliminado se
    # puede restaurar; después el mantenimiento lo borra de verdad con lo que dependía de él (la Ley Federal del
    # Trabajo pide conservar la asistencia hasta un año después de terminar la relación laboral).
    SOFT_DELETE_RETENTION_DAYS: int = Field(default=365, ge=30, le=3650)
    # Registros eliminados que se depuran por sentencia (cada uno borra en cascada su historial: un empleado, un año
    # de asistencia), con el tope de lotes por vuelta de MAINTENANCE_MAX_BATCHES_PER_TABLE.
    SOFT_DELETE_PURGE_BATCH_SIZE: int = Field(default=50, ge=1, le=5000)
    # Meses que el mantenimiento deja creados por adelantado en las tablas particionadas por mes (además del
    # actual; app/core/partitions.py). Con varios meses de margen, una semana sin mantenimiento no deja ninguna
    # fila en la partición `_default`.
    PARTITION_MONTHS_AHEAD: int = Field(default=3, ge=1, le=24)

    # --- Cobranza (esquema billing; tareas en billing_jobs, dentro del mantenimiento) ---
    # Días hacia atrás en que el mantenimiento repone la plantilla diaria (empleados activos por día)
    # que no pudo cerrar (instancias caídas): base del cobro prorrateado.
    BILLING_HEADCOUNT_BACKFILL_DAYS: int = Field(default=400, ge=1, le=3650)
    # Topes por vuelta del mantenimiento (lo demás sigue en la siguiente): días de plantilla que se
    # cierran, cargos que se emiten y pronósticos que se refrescan.
    BILLING_DAYS_PER_ROUND: int = Field(default=31, ge=1, le=400)
    BILLING_CHARGES_PER_ROUND: int = Field(default=500, ge=1, le=100_000)
    BILLING_FORECASTS_PER_ROUND: int = Field(default=2000, ge=1, le=100_000)
    # Comprobante de un pago (PDF o imagen): tamaño máximo.
    BILLING_RECEIPT_MAX_MB: float = Field(default=5.0, gt=0, le=20)

    # --- Consumo (ops.usage_*; medidor en usage_meter) ---
    # El medidor suma cada petición en memoria y un hilo guarda en lotes cada USAGE_FLUSH_SECONDS
    # (0 = solo al apagar o a mano: pruebas). Nunca frena una petición.
    USAGE_METER_ENABLED: bool = True
    USAGE_FLUSH_SECONDS: float = Field(default=5.0, ge=0)
    # Llaves distintas (empresa/día, empresa/día/ruta, empresa/día/usuario) que caben en memoria entre
    # lotes: las rutas y usuarios que no caben se suman a OTHER / usuario 0 de su empresa (nada se pierde).
    USAGE_METER_MAX_KEYS: int = Field(default=50_000, ge=100, le=5_000_000)
    # Días que se conservan: totales por empresa y fotos del almacenamiento; detalle por ruta y por usuario.
    USAGE_RETENTION_DAYS: int = Field(default=400, ge=31, le=3650)
    USAGE_DETAIL_RETENTION_DAYS: int = Field(default=90, ge=7, le=3650)

    # --- Rendimiento (ops.perf_*, pantalla "Rendimiento"; observadores en app/core/observability.py) ---
    # Cada petición, cada función medida (`observed`) y cada muestra del navegador se suma en memoria por minuto
    # (contadores e histograma de tiempos con cubetas fijas) y un hilo guarda en lotes cada PERF_FLUSH_SECONDS
    # (0 = solo al apagar o a mano: pruebas). Nunca frena una petición ni le agrega consultas.
    PERF_METER_ENABLED: bool = True
    PERF_FLUSH_SECONDS: float = Field(default=15.0, ge=0)
    # Llaves distintas (minuto, tipo, nombre) que caben en memoria entre lotes: lo que no cabe se suma a OTHER de su
    # minuto y tipo (el total se conserva; solo se pierde el desglose).
    PERF_METER_MAX_KEYS: int = Field(default=20_000, ge=100, le=1_000_000)
    # Días que se conserva cada grano: por minuto (particionado por mes; lo vencido sale por lotes y el mes completo
    # con su partición), por hora y por día (los resúmenes que arma el mantenimiento).
    PERF_MINUTE_RETENTION_DAYS: int = Field(default=7, ge=1, le=62)
    PERF_HOUR_RETENTION_DAYS: int = Field(default=90, ge=7, le=730)
    PERF_DAY_RETENTION_DAYS: int = Field(default=730, ge=31, le=3650)
    # Horas hacia atrás que el mantenimiento vuelve a resumir en cada vuelta (minuto → hora → día): cubre lo que un
    # lote guardó tarde (BD caída un rato) sin volver a leer toda la historia.
    PERF_ROLLUP_LOOKBACK_HOURS: int = Field(default=3, ge=1, le=72)
    # Muestras que acepta cada lote del navegador (POST /api/telemetry/web; lo demás es un 422).
    PERF_WEB_MAX_SAMPLES: int = Field(default=500, ge=10, le=5000)
    # Regla 18: toda petición más lenta que esto (ms) alerta al ADMIN, agrupada por ruta (ops.slow_request_alerts).
    SLOW_REQUEST_THRESHOLD_MS: int = Field(default=1000, ge=50, le=600_000)
    # Excepción de la regla 18 (decisión del dueño, 2026-10-06): las rutas faciales (`admission.FACE_PREFIXES`:
    # registro, verificación, identificación, reto y registro de asistencia) analizan varias capturas con el motor y
    # alertan desde este umbral (ms); las demás siguen con SLOW_REQUEST_THRESHOLD_MS.
    SLOW_REQUEST_FACE_THRESHOLD_MS: int = Field(default=2500, ge=50, le=600_000)
    # Rutas distintas con peticiones lentas que caben en memoria entre lotes (las que no caben se suman a OTHER).
    SLOW_REQUEST_MAX_ROUTES: int = Field(default=1000, ge=10, le=100_000)
    # Una alerta RESUELTA que no ha vuelto a ocurrir en este tiempo se depura (si vuelve, se crea de nuevo abierta).
    SLOW_ALERT_RESOLVED_RETENTION_DAYS: int = Field(default=180, ge=1, le=3650)

    # --- Respaldos de la base (servicio `backup` de docker compose; README "Respaldos y restauración") ---
    # Carpeta (un volumen) donde quedan los respaldos `pg_dump` en formato custom.
    BACKUP_DIR: str = "/backups"
    # Cada cuántas horas respalda el servicio `backup` (0 = no respalda solo; a mano: python -m app.cli db backup).
    BACKUP_INTERVAL_HOURS: float = Field(default=24.0, ge=0, le=168)
    # Días que se conservan los respaldos en la carpeta y en el bucket.
    BACKUP_RETENTION_DAYS: int = Field(default=14, ge=1, le=3650)
    # Subir además cada respaldo al bucket (GCS_BUCKET, bajo <GCS_PREFIX>/backups/) CIFRADO con
    # DATA_ENCRYPTION_KEY antes de salir del servidor: nunca un respaldo legible fuera de él.
    BACKUP_UPLOAD: bool = False
    # Tamaño de cada parte cifrada (MB): el respaldo se cifra y sube por partes, sin cargarlo completo en memoria.
    BACKUP_CHUNK_MB: int = Field(default=64, ge=1, le=1024)
    # Tiempo máximo de pg_dump (segundos): un respaldo atorado falla y se registra en lugar de esperar para siempre.
    BACKUP_TIMEOUT_SECONDS: int = Field(default=3600, ge=60, le=86_400)
    # Si un respaldo o su copia al bucket fallan (base o bucket caídos), se reintenta cada tantos minutos: el respaldo
    # local se conserva y la subida es idempotente (lo que ya estaba en el bucket se reconoce y no se repite).
    BACKUP_RETRY_MINUTES: int = Field(default=15, ge=1, le=1440)
    # Regla de ciclo de vida del bucket (python -m app.cli storage lifecycle --apply): Google borra sola cada copia
    # cifrada de <GCS_PREFIX>/backups/ con más de BACKUP_RETENTION_DAYS + estos días. Red de seguridad si la carpeta
    # local se pierde (la depuración normal la hace el servicio backup con sus manifiestos).
    BACKUP_LIFECYCLE_MARGIN_DAYS: int = Field(default=7, ge=1, le=365)

    # --- Recuperación a un punto en el tiempo (PITR con pgBackRest) ---
    # README "Respaldos y restauración". Archivo CONTINUO del WAL y respaldos base al bucket (GCS_BUCKET, bajo
    # <GCS_PREFIX>/pitr/), cifrados con PITR_CIPHER_PASS antes de salir del servidor. Lo leen los servicios db y pitr
    # (postgres/pgbackrest.sh) y el monitor del servicio backup (app/services/pitr_monitor.py), que avisa al ADMIN en
    # "Errores del sistema".
    # Cambiarlo se aplica recreando db y pitr (archive_mode solo cambia al reiniciar PostgreSQL).
    PITR_ENABLED: bool = False
    # Llave del repositorio de pgBackRest (aes-256-cbc), PROPIA y no derivada de DATA_ENCRYPTION_KEY: un repositorio
    # de pgBackRest no se puede volver a cifrar, así que rotar DATA_ENCRYPTION_KEY no debe dejarlo ilegible. Sin ella
    # no hay restauración: se guarda fuera del equipo junto con el resto de este .env. Mínimo 32 caracteres.
    PITR_CIPHER_PASS: str = ""
    # archive_timeout de PostgreSQL (s): un cambio confirmado espera a lo más esto para salir al bucket (el RPO).
    PITR_ARCHIVE_TIMEOUT_SECONDS: int = Field(default=60, ge=15, le=3600)
    # Respaldo base completo cada tantas horas y diferencial cada tantas (0 = solo completos). Restaurar es el último
    # respaldo base más reproducir el WAL desde él: más seguido = recuperación más rápida.
    PITR_FULL_BACKUP_HOURS: int = Field(default=168, ge=1, le=8760)
    PITR_DIFF_BACKUP_HOURS: int = Field(default=24, ge=0, le=720)
    # Días hacia atrás a los que siempre se puede volver (pgBackRest borra solo lo que ya no hace falta para eso).
    PITR_RETENTION_DAYS: int = Field(default=14, ge=1, le=3650)
    # Guardián del disco (MB): WAL esperando subir si el bucket no responde. Al pasarlo se DESCARTA: la base sigue
    # viva y la recuperación continua se interrumpe hasta el siguiente respaldo completo (lo toma solo el servicio
    # pitr). Debe ser menor que el espacio libre del disco de la base menos un margen. El límite se revisa cada
    # PITR_PUSH_BATCH_MB de WAL subido (lo más que puede pasarse).
    PITR_WAL_MAX_MB: int = Field(default=8192, ge=256, le=1_048_576)
    PITR_PUSH_BATCH_MB: int = Field(default=1024, ge=16, le=65_536)
    # Procesos de pgBackRest en paralelo (subir WAL, respaldar, restaurar) y tiempo límite de cada operación de red (s).
    PITR_PROCESS_MAX: int = Field(default=2, ge=1, le=16)
    PITR_IO_TIMEOUT_SECONDS: int = Field(default=60, ge=10, le=3600)
    # Tiempo máximo de un respaldo base (s) y espera tras una falla antes de reintentar (min: sin martillar al bucket).
    PITR_BACKUP_TIMEOUT_SECONDS: int = Field(default=21_600, ge=600, le=172_800)
    PITR_RETRY_MINUTES: int = Field(default=15, ge=1, le=1440)
    # Volumen compartido `pitr`: el servicio pitr escribe su estado y el db el aviso de WAL descartado; backup lo lee.
    PITR_STATUS_DIR: str = "/var/lib/timeclock-pitr"
    # Cada cuántos segundos revisan el servicio pitr (si toca un respaldo base) y el monitor del servicio backup.
    PITR_CHECK_SECONDS: int = Field(default=60, ge=5, le=3600)
    # Avisos al ADMIN: el WAL más viejo lleva más que esto esperando subir (s) o hay más que esto esperando (MB). Un
    # aviso que sigue activo se repite cada tantos minutos (una sola fila en "Errores del sistema" con su contador).
    PITR_LAG_ALERT_SECONDS: int = Field(default=600, ge=30, le=86_400)
    PITR_WAL_ALERT_MB: int = Field(default=1024, ge=16, le=1_048_576)
    PITR_ALERT_REPEAT_MINUTES: int = Field(default=60, ge=1, le=10_080)

    # --- Imágenes y archivos: bucket de Google Cloud Storage (Firebase Storage) ---
    # Decisión del dueño del producto: NINGUNA imagen ni archivo se guarda en la base de datos. La foto de
    # referencia del registro facial, el comprobante de un pago y la foto de perfil se cifran con
    # DATA_ENCRYPTION_KEY y se suben a este bucket privado DURANTE la petición; la BD solo guarda la referencia
    # (`app/services/image_storage.py`). Sin bucket o sin llave, guardar una imagen responde 503
    # STORAGE_UNAVAILABLE (no hay respaldo en la BD ni en disco).
    # Nombre del bucket, sin "gs://" (p. ej. employee-time-clock-fb8ba.firebasestorage.app).
    GCS_BUCKET: str = ""
    # Ruta, DENTRO del contenedor, del JSON de la cuenta de servicio: el archivo vive fuera del
    # repositorio y docker compose lo monta de solo lectura (ver README, "Almacenamiento de imágenes").
    GCS_CREDENTIALS_FILE: str = ""
    # Carpeta raíz de este entorno dentro del bucket (local, staging, production); vacío = ENVIRONMENT.
    GCS_PREFIX: str = ""
    # Tiempo límite de cada llamada al bucket; tiempo total (con reintentos de lo idempotente) de una
    # operación dentro de una petición y en segundo plano (el mantenimiento).
    GCS_TIMEOUT_SECONDS: float = Field(default=10.0, gt=0, le=300)
    GCS_REQUEST_SECONDS: float = Field(default=20.0, gt=0, le=120)
    GCS_RETRY_SECONDS: float = Field(default=60.0, ge=0, le=900)
    # Por vuelta del mantenimiento (borrar del bucket lo que ya no se conserva): objetos por consulta y
    # tiempo máximo de la vuelta.
    GCS_BATCH_SIZE: int = Field(default=200, ge=1, le=10_000)
    GCS_ROUND_SECONDS: float = Field(default=60.0, gt=0, le=3600)

    # --- Foto de perfil (de la persona; cifrada en el bucket, nunca en la BD) ---
    # Archivo que se recibe (JPG, PNG o WEBP): tamaño máximo. Más grande: 413 AVATAR_TOO_LARGE.
    AVATAR_MAX_MB: float = Field(default=5.0, gt=0, le=20)
    # Megapíxeles máximos de la imagen recibida (defensa contra "bombas" de descompresión: una imagen
    # pequeña en bytes que ocupa gigas al abrirse). Más: 422 AVATAR_INVALID.
    AVATAR_MAX_MEGAPIXELS: float = Field(default=40.0, gt=0, le=100)
    # Lado mínimo (px) del recorte cuadrado: menos se vería borroso al ampliarse. Menos: 422.
    AVATAR_MIN_SIDE_PX: int = Field(default=128, ge=64, le=512)
    # Calidad WebP de los tamaños que se guardan (512 y 96 px).
    AVATAR_WEBP_QUALITY: int = Field(default=82, ge=50, le=100)
    # Segundos que el navegador conserva una foto (la URL lleva su versión: al cambiarla, la URL cambia).
    # 0 = no se guarda en el navegador (Cache-Control: no-store), solo en la memoria de la página.
    AVATAR_CACHE_SECONDS: int = Field(default=86_400, ge=0, le=31_536_000)
    # Fotos nuevas (subir o reemplazar) por persona y minuto.
    RATE_LIMIT_AVATAR_PER_MINUTE: int = Field(default=10, ge=1)

    # --- Documentos de la empresa (para su facturación; cifrados en el bucket, nunca en la BD) ---
    # Archivo que se recibe (PDF, Word, Excel, XML, JPG o PNG): tamaño máximo. Más grande: 413 DOCUMENT_TOO_LARGE.
    # Debe caber en el límite del cuerpo del gateway (client_max_body_size de docker/nginx.conf, 27 MB).
    COMPANY_DOCUMENT_MAX_MB: float = Field(default=20.0, gt=0, le=25)
    # Megapíxeles máximos de una imagen (JPG o PNG) recibida (defensa contra "bombas" de descompresión: una imagen
    # pequeña en bytes que ocupa gigas al abrirse). Más: 422 DOCUMENT_IMAGE_TOO_LARGE.
    COMPANY_DOCUMENT_MAX_MEGAPIXELS: float = Field(default=60.0, gt=0, le=200)
    # Calidad JPEG con que se vuelve a codificar una foto recibida (sin sus metadatos: EXIF con la ubicación GPS, XMP).
    COMPANY_DOCUMENT_JPEG_QUALITY: int = Field(default=92, ge=50, le=100)
    # Documentos nuevos (subir) por persona y minuto (cada uno cuesta revisarlo, cifrarlo y subirlo al bucket).
    RATE_LIMIT_DOCUMENT_UPLOADS_PER_MINUTE: int = Field(default=10, ge=1)

    # --- API de integración (llaves por empresa) ---
    API_KEYS_MAX_ACTIVE: int = Field(default=10, ge=1, le=100)  # llaves activas por empresa
    # El último uso de una llave se guarda a lo más cada estos segundos (sin escribir en cada petición).
    API_KEY_TOUCH_SECONDS: int = Field(default=60, ge=0)

    # --- Usuarios iniciales (opcionales, se crean al iniciar si no existen) ---
    # Administrador de la plataforma: da de alta y administra empresas.
    FIRST_ADMIN_EMAIL: str | None = None
    FIRST_ADMIN_PASSWORD: str | None = None
    # Administrador de la primera empresa.
    FIRST_COMPANY_EMAIL: str | None = None
    FIRST_COMPANY_PASSWORD: str | None = None

    @field_validator("APP_TIMEZONE")
    @classmethod
    def _known_timezone(cls, value: str) -> str:
        """Una zona IANA que exista (p. ej. America/Mexico_City): un error de escritura no debe
        cambiar en silencio la hora del negocio."""
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(f"APP_TIMEZONE desconocida: {value}") from exc
        return value

    @field_validator("FACE_BLOCKED_CAMERAS", mode="before")
    @classmethod
    def _split_cameras(cls, value: object) -> object:
        if isinstance(value, str):
            return [name.strip().lower() for name in value.split(",") if name.strip()]
        return value

    @field_validator("IP_HOSTING_KEYWORDS", mode="before")
    @classmethod
    def _split_keywords(cls, value: object) -> object:
        if isinstance(value, str):
            return [word.strip().lower() for word in value.split(",") if word.strip()]
        return value

    @field_validator("IP_HOSTING_ASNS", mode="before")
    @classmethod
    def _split_asns(cls, value: object) -> object:
        if isinstance(value, str):
            return [int(number) for number in value.split(",") if number.strip()]
        return value

    @property
    def ip_country_db(self) -> Path:
        """El archivo MMDB de país (una ruta relativa parte de la carpeta de la API, como en la imagen: /app)."""
        return _api_path(self.IP_COUNTRY_DB_PATH)

    @property
    def ip_asn_db(self) -> Path:
        return _api_path(self.IP_ASN_DB_PATH)

    @field_validator("CORS_ORIGINS", mode="before")
    @classmethod
    def _split_origins(cls, value: object) -> object:
        if isinstance(value, str):
            return [origin.strip() for origin in value.split(",") if origin.strip()]
        return value

    @field_validator("DATABASE_URL", "DATABASE_DIRECT_URL")
    @classmethod
    def _normalize_db_url(cls, value: str) -> str:
        # SQLAlchemy necesita el driver explícito para psycopg 3.
        for prefix in ("postgres://", "postgresql://"):
            if value.startswith(prefix):
                return "postgresql+psycopg://" + value[len(prefix) :]
        return value

    @field_validator("JWT_PRIVATE_KEY")
    @classmethod
    def _decode_pem(cls, value: str) -> str:
        return decode_pem(value)

    @field_validator("GCS_BUCKET")
    @classmethod
    def _bucket_name(cls, value: str) -> str:
        """Acepta el nombre con o sin "gs://" (como lo muestra la consola de Firebase)."""
        return value.strip().removeprefix("gs://").strip("/")

    @field_validator("GCS_PREFIX")
    @classmethod
    def _storage_prefix(cls, value: str) -> str:
        """Una sola carpeta en minúsculas: es parte del nombre de cada objeto (nunca datos personales)."""
        value = value.strip().strip("/")
        if value and not _PREFIX.fullmatch(value):
            raise ValueError("GCS_PREFIX: solo minúsculas, números, '.', '_' y '-' (hasta 40), p. ej. production")
        return value

    @field_validator("PITR_CIPHER_PASS")
    @classmethod
    def _cipher_pass(cls, value: str) -> str:
        """La llave del repositorio de pgBackRest va a su archivo de configuración: larga y en un solo renglón."""
        if value and (len(value) < 32 or any(char.isspace() for char in value)):
            raise ValueError("PITR_CIPHER_PASS: al menos 32 caracteres y sin espacios (la genera generate_secrets.py)")
        return value

    @model_validator(mode="after")
    def _complete(self) -> Settings:
        if self.API_WORKERS == 0:
            self.API_WORKERS = default_api_workers()
        if self.PAGE_SIZE_DEFAULT > self.PAGE_SIZE_MAX:
            raise ValueError("PAGE_SIZE_DEFAULT no puede ser mayor que PAGE_SIZE_MAX")
        for low, high in _ORDERED_PAIRS:
            if getattr(self, low) > getattr(self, high):
                raise ValueError(f"{low} no puede ser mayor que {high}")
        self._database_urls()
        if self.is_production and self.RATE_LIMIT_BACKEND == "memory":
            raise ValueError(
                "RATE_LIMIT_BACKEND=memory no se permite en producción: cada réplica contaría por su lado "
                "(usa database, compartido por todas)"
            )
        if self.JWT_ALGORITHM == "ES256" and "PRIVATE KEY" not in self.JWT_PRIVATE_KEY:
            raise ValueError(
                "JWT_ALGORITHM=ES256 requiere JWT_PRIVATE_KEY (genérala con: python scripts/generate_secrets.py)"
            )
        if self.JWT_ALGORITHM == "HS256" and len(self.JWT_SECRET_KEY) < 32:
            raise ValueError("JWT_ALGORITHM=HS256 requiere JWT_SECRET_KEY de al menos 32 caracteres")
        return self

    def _database_urls(self) -> None:
        """La API se conecta con su usuario de mínimo privilegio (DB_APP_*, si tiene contraseña; si no, con el
        dueño) y las migraciones SIEMPRE con el dueño (POSTGRES_*: hacen DDL), directo a PostgreSQL."""
        owner = (self.POSTGRES_USER, self.POSTGRES_PASSWORD)
        api = (self.DB_APP_USER, self.DB_APP_PASSWORD) if self.DB_APP_PASSWORD else owner
        explicit = bool(self.DATABASE_URL)
        if not explicit:
            if not api[1]:
                raise ValueError(
                    "Define DATABASE_URL o la contraseña de la base: DB_APP_PASSWORD (usuario de la API) o "
                    "POSTGRES_PASSWORD (y POSTGRES_HOST/DB/USER)"
                )
            self.DATABASE_URL = self._postgres_url(self.POSTGRES_HOST, self.POSTGRES_PORT, *api)
        if self.DATABASE_DIRECT_URL:
            return
        if self.POSTGRES_DIRECT_HOST:
            self.DATABASE_DIRECT_URL = self._postgres_url(self.POSTGRES_DIRECT_HOST, self.POSTGRES_DIRECT_PORT, *owner)
        elif self.DB_POOLER == "pgbouncer":
            raise ValueError(
                "Con DB_POOLER=pgbouncer define POSTGRES_DIRECT_HOST (o DATABASE_DIRECT_URL): las migraciones "
                "necesitan una conexión directa a PostgreSQL, no a PgBouncer"
            )
        else:
            self.DATABASE_DIRECT_URL = (
                self.DATABASE_URL if explicit else self._postgres_url(self.POSTGRES_HOST, self.POSTGRES_PORT, *owner)
            )

    def _postgres_url(self, host: str, port: int, user: str, password: str) -> str:
        """URL de psycopg 3 con esas credenciales (la contraseña se define una sola vez, en `.env`)."""
        return f"postgresql+psycopg://{quote(user)}:{quote(password)}@{host}:{port}/{self.POSTGRES_DB}"

    @property
    def platform_role(self) -> str:
        """Rol al que cambia el código de la plataforma (`row_security.use_platform`); vacío si la API se conecta
        con el dueño (desarrollo): ese ya no está sujeto a la seguridad por fila y el rol podría no existir."""
        return self.DB_PLATFORM_ROLE if self.DB_APP_PASSWORD else ""

    @field_validator("DATA_ENCRYPTION_KEY", "DATA_ENCRYPTION_PREVIOUS_KEYS")
    @classmethod
    def _validate_fernet_key(cls, value: str) -> str:
        try:
            for key in (k.strip() for k in value.split(",")):
                if key:
                    Fernet(key.encode())
        except Exception as exc:
            raise ValueError(
                "DATA_ENCRYPTION_KEY debe ser una clave Fernet válida "
                '(genérala con: python -c "from cryptography.fernet import Fernet; '
                'print(Fernet.generate_key().decode())")'
            ) from exc
        return value

    @property
    def data_encryption_keys(self) -> list[str]:
        """La llave vigente primero (cifra) y después las anteriores (solo descifran)."""
        previous = [k.strip() for k in self.DATA_ENCRYPTION_PREVIOUS_KEYS.split(",") if k.strip()]
        return [self.DATA_ENCRYPTION_KEY, *previous]

    @property
    def max_image_bytes(self) -> int:
        return int(self.MAX_IMAGE_SIZE_MB * 1024 * 1024)

    @property
    def is_production(self) -> bool:
        return self.ENVIRONMENT.lower() == "production"

    @property
    def storage_prefix(self) -> str:
        """Carpeta raíz de este entorno en el bucket: GCS_PREFIX o, si está vacía, ENVIRONMENT."""
        return self.GCS_PREFIX or re.sub(r"[^a-z0-9._-]+", "-", self.ENVIRONMENT.lower()).strip("-")[:40] or "default"


def decode_pem(value: str) -> str:
    """Acepta PEM con saltos de línea reales, escapados ("\\n") o codificado en base64."""
    value = value.strip().strip('"').strip("'")
    if not value:
        return ""
    if "-----BEGIN" not in value:
        try:
            value = base64.b64decode(value).decode()
        except ValueError:
            return value
    return value.replace("\\n", "\n")


#: Pares (mínimo, máximo) que deben ir en orden: un piso por encima de su tope dejaría la autocalibración sin rango.
_ORDERED_PAIRS = (
    ("FACE_BURST_MIN_MOTION", "FACE_BURST_MAX_MOTION"),
    ("FACE_NOISE_MIN_RATIO", "FACE_NOISE_MAX_RATIO"),
    ("FACE_PARALLAX_MIN", "FACE_PARALLAX_MAX"),
    ("FACE_MOIRE_TIGHTEST_DB", "FACE_MOIRE_MAX_DB"),
    ("FACE_PULSE_MIN_HZ", "FACE_PULSE_MAX_HZ"),
    ("FACE_FLASH_PACE_MIN_MS", "FACE_FLASH_PACE_WINDOW_MS"),
    # Un registro que pide más fotos útiles de las que acepta nunca podría completarse.
    ("FACE_ENROLL_MIN_USABLE", "FACE_ENROLL_MAX_PHOTOS"),
    # El aviso de WAL acumulado llega antes que el guardián que lo descarta.
    ("PITR_WAL_ALERT_MB", "PITR_WAL_MAX_MB"),
)


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
