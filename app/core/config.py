"""Configuración centralizada de la API.

Única fuente de configuración: variables de entorno / `backend/.env` (el único archivo de
configuración del backend; solo variables con valor). Lo que no aparece ahí usa el valor por
defecto de este módulo. Ningún otro módulo lee `os.environ` directamente.
"""

import base64
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


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=ENV_FILE, env_file_encoding="utf-8", extra="ignore")

    # --- Aplicación ---
    APP_NAME: str = "Employee Time Clock API"
    ENVIRONMENT: str = "development"
    DOCS_ENABLED: bool = True
    API_PREFIX: str = "/api"
    LOG_LEVEL: str = "INFO"
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
    CORS_ORIGINS: Annotated[list[str], NoDecode] = ["http://localhost:5173"]

    # --- Cifrado de datos sensibles (embeddings y tokens QR) ---
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
    FACE_MODELS_DIR: str = "./models"
    FACE_MODELS_AUTO_DOWNLOAD: bool = True
    FACE_DETECTION_MIN_SCORE: float = Field(default=0.80, ge=0.1, le=1.0)
    FACE_SECONDARY_DETECTION_SCORE: float = Field(default=0.60, ge=0.1, le=1.0)
    FACE_MIN_SIZE_PX: int = Field(default=80, ge=32)
    FACE_MIN_SHARPNESS: float = Field(default=20.0, ge=0.0)
    FACE_MIN_BRIGHTNESS: float = Field(default=40.0, ge=0.0, le=255.0)
    FACE_MAX_BRIGHTNESS: float = Field(default=225.0, ge=0.0, le=255.0)
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

    # Pose: el rostro debe estar de frente (ratio de giro ≈ 0.4·tan(ángulo)).
    FACE_MAX_YAW_RATIO: float = Field(default=0.15, gt=0.0, le=1.0)
    FACE_MAX_ROLL_DEGREES: float = Field(default=15.0, gt=0.0, le=45.0)

    # Registro: las muestras de un mismo registro deben ser consistentes entre sí.
    FACE_ENROLL_CONSISTENCY_THRESHOLD: float = Field(default=0.55, ge=0.0, le=1.0)

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

    # Prueba de vida: reto aleatorio emitido por el servidor (girar la cabeza).
    FACE_LIVENESS_ENABLED: bool = True
    FACE_LIVENESS_MIN_YAW_RATIO: float = Field(default=0.18, gt=0.0, le=1.0)
    FACE_LIVENESS_CONSISTENCY_THRESHOLD: float = Field(default=0.30, ge=0.0, le=1.0)
    FACE_CHALLENGE_TTL_SECONDS: int = Field(default=90, ge=15, le=600)

    # --- Protección contra engaños: parámetros técnicos de los candados ---
    # Cada empresa activa o desactiva cada candado en su política de verificación
    # (tenancy.verification_policy); aquí solo se ajusta cómo mide cada uno.
    # Diferencia mínima (niveles de gris 0-255) entre capturas de un mismo intento: una persona
    # real nunca da dos fotogramas iguales (ruido del sensor, respiración); una foto fija sí.
    FACE_STATIC_MIN_DIFFERENCE: float = Field(default=0.6, ge=0.0, le=50.0)
    # Segundos mínimos entre el reto y la captura del giro: menos es imposible para una persona.
    FACE_CHALLENGE_MIN_SECONDS: float = Field(default=0.8, ge=0.0, le=30.0)
    # Días que se recuerda la huella de cada captura para rechazar su reenvío.
    FACE_REPLAY_RETENTION_DAYS: int = Field(default=30, ge=1, le=365)
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

    # --- Tolerancia a fallas ---
    # Conexiones persistentes: las de "overflow" se abren y cierran en cada uso (autenticación
    # SCRAM incluida), lo que bajo carga es mucho más lento que esperar una del pool.
    # Valores POR PROCESO: con API_WORKERS procesos se usan hasta
    # API_WORKERS x (DB_POOL_SIZE + DB_MAX_OVERFLOW) conexiones (ajustar max_connections).
    DB_POOL_SIZE: int = Field(default=20, ge=1)
    DB_MAX_OVERFLOW: int = Field(default=5, ge=0)
    DB_POOL_TIMEOUT_SECONDS: int = Field(default=10, ge=1)
    DB_CONNECT_TIMEOUT_SECONDS: int = Field(default=5, ge=1)
    DB_STATEMENT_TIMEOUT_MS: int = Field(default=15000, ge=1000)
    DB_STARTUP_RETRIES: int = Field(default=30, ge=1)
    # Cola del procesamiento facial (intensivo en CPU): 1 worker por núcleo.
    FACE_WORKERS: int = Field(default=0, ge=0, le=128)  # 0 = automático (núcleos / API_WORKERS)
    FACE_QUEUE_MAX_WAITING: int = Field(default=0, ge=0)  # 0 = automático (workers x 4)
    FACE_QUEUE_TIMEOUT_SECONDS: float = Field(default=15.0, gt=0)
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
    REQUEST_QUEUE_MAX: int = Field(default=0, ge=0)
    # Vida media de la demanda reciente de cada API (s): qué tan rápido "olvida" un pico.
    ADMISSION_DEMAND_HALF_LIFE_SECONDS: float = Field(default=60.0, gt=0)
    # Argon2id usa ~64 MB por hash: se limita cuántos se calculan a la vez (0 = núcleos).
    PASSWORD_HASH_CONCURRENCY: int = Field(default=0, ge=0)
    PASSWORD_HASH_WAIT_SECONDS: float = Field(default=10.0, gt=0)
    # Hilos para endpoints síncronos (0 = automático: workers faciales + cola + pool de BD).
    THREADPOOL_SIZE: int = Field(default=0, ge=0)
    # Tras un fallo al cargar los modelos, esperar antes de reintentar (circuit breaker).
    FACE_ENGINE_RETRY_SECONDS: int = Field(default=30, ge=1)

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

    # --- Listados paginados ---
    # Elementos por página si el cliente no indica `size`, y el máximo que acepta (el paginador
    # de la webapp ofrece 10, 20, 30, 40 y 50).
    PAGE_SIZE_DEFAULT: int = Field(default=10, ge=1, le=100)
    PAGE_SIZE_MAX: int = Field(default=50, ge=1, le=100)
    # Catálogos (esquema catalog): segundos que cada proceso los conserva en memoria antes de releerlos.
    CATALOG_CACHE_SECONDS: float = Field(default=60, ge=0, le=3600)

    # --- Protección contra abuso (peticiones por minuto) ---
    # memory = por proceso; database = compartido por todos los procesos/instancias (PostgreSQL).
    RATE_LIMIT_BACKEND: Literal["memory", "database"] = "database"
    RATE_LIMIT_REFRESH_PER_MINUTE: int = Field(default=30, ge=1)
    RATE_LIMIT_LOGIN_PER_MINUTE: int = Field(default=10, ge=1)
    # Por IP es alto a propósito: oficinas enteras salen a Internet con una sola IP (NAT);
    # la protección contra fuerza bruta real es el límite por cuenta.
    RATE_LIMIT_LOGIN_IP_PER_MINUTE: int = Field(default=600, ge=1)
    RATE_LIMIT_VERIFY_PER_MINUTE: int = Field(default=30, ge=1)
    # Un validador atiende a muchos empleados seguidos desde el mismo dispositivo.
    RATE_LIMIT_CHECKPOINT_PER_MINUTE: int = Field(default=120, ge=1)
    RATE_LIMIT_FACE_CHECK_PER_MINUTE: int = Field(default=60, ge=1)
    # Validación en vivo por HTTP (respaldo del WebSocket): consultas por usuario y minuto.
    RATE_LIMIT_VALIDATION_PER_MINUTE: int = Field(default=120, ge=1)
    # API de integración: peticiones por llave y minuto.
    RATE_LIMIT_API_KEY_PER_MINUTE: int = Field(default=120, ge=1)

    # --- Registro de errores del sistema (ops.error_reports, pantalla "Errores del sistema") ---
    # Se acumulan en memoria y un hilo los guarda en lotes cada ERROR_REPORT_FLUSH_SECONDS (0 = solo
    # al apagar o a mano: pruebas). Nunca frenan una petición.
    ERROR_REPORT_FLUSH_SECONDS: float = Field(default=2.0, ge=0)
    ERROR_REPORT_BUFFER: int = Field(default=10_000, ge=100, le=1_000_000)
    # Ocurrencias que se guardan por error en cada vuelta (el total siempre se cuenta).
    ERROR_REPORT_OCCURRENCES_PER_FLUSH: int = Field(default=5, ge=1, le=100)
    ERROR_OCCURRENCE_RETENTION_DAYS: int = Field(default=30, ge=1, le=3650)
    # Un error SOLUCIONADO que no ha vuelto a ocurrir en este tiempo se depura (si vuelve, se crea
    # de nuevo como pendiente): la bandeja no crece sin fin.
    ERROR_RESOLVED_RETENTION_DAYS: int = Field(default=180, ge=1, le=3650)

    # --- Asistente de reportes (pantalla "Reportes" de cada empresa; app/services/reporting) ---
    # Filas que muestra el asistente en pantalla (el resto se ve al exportar a Excel).
    REPORT_PREVIEW_ROWS: int = Field(default=20, ge=1, le=200)
    # Tope de filas de un archivo de Excel: un reporte mayor se corta y el archivo lo dice.
    REPORT_EXPORT_MAX_ROWS: int = Field(default=100_000, ge=100, le=1_000_000)
    # Tope de grupos de un reporte agrupado (p. ej. por empleado o por día).
    REPORT_GROUPS_MAX: int = Field(default=500, ge=10, le=10_000)
    # Cuánto recuerda las preguntas de cada empresa (sugerencias y aprendizaje).
    REPORT_QUERY_RETENTION_DAYS: int = Field(default=180, ge=7, le=3650)

    # --- Mantenimiento (depuración de lo vencido, fuera de las peticiones) ---
    # Cada cuántos segundos depura cada instancia (solo una a la vez trabaja); 0 = desactivado
    # (p. ej. si lo ejecuta un cron externo con `python -m app.cli purge`).
    MAINTENANCE_INTERVAL_SECONDS: int = Field(default=300, ge=0)
    MAINTENANCE_BATCH_SIZE: int = Field(default=5000, ge=100, le=100_000)

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

    @field_validator("CORS_ORIGINS", mode="before")
    @classmethod
    def _split_origins(cls, value: object) -> object:
        if isinstance(value, str):
            return [origin.strip() for origin in value.split(",") if origin.strip()]
        return value

    @field_validator("DATABASE_URL")
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

    @model_validator(mode="after")
    def _complete(self) -> Settings:
        if self.API_WORKERS == 0:
            self.API_WORKERS = default_api_workers()
        if self.PAGE_SIZE_DEFAULT > self.PAGE_SIZE_MAX:
            raise ValueError("PAGE_SIZE_DEFAULT no puede ser mayor que PAGE_SIZE_MAX")
        if not self.DATABASE_URL:
            if not self.POSTGRES_PASSWORD:
                raise ValueError("Define DATABASE_URL o POSTGRES_PASSWORD (y POSTGRES_HOST/DB/USER)")
            self.DATABASE_URL = (
                f"postgresql+psycopg://{quote(self.POSTGRES_USER)}:{quote(self.POSTGRES_PASSWORD)}"
                f"@{self.POSTGRES_HOST}:{self.POSTGRES_PORT}/{self.POSTGRES_DB}"
            )
        if self.JWT_ALGORITHM == "ES256" and "PRIVATE KEY" not in self.JWT_PRIVATE_KEY:
            raise ValueError(
                "JWT_ALGORITHM=ES256 requiere JWT_PRIVATE_KEY (genérala con: python scripts/generate_secrets.py)"
            )
        if self.JWT_ALGORITHM == "HS256" and len(self.JWT_SECRET_KEY) < 32:
            raise ValueError("JWT_ALGORITHM=HS256 requiere JWT_SECRET_KEY de al menos 32 caracteres")
        return self

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


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
