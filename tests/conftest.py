"""Pruebas con SQLite y un pipeline facial falso (no requiere modelos ONNX)."""

import base64
import hashlib
import json
import os
import re
import tempfile
import uuid
from dataclasses import replace

import cv2
import numpy as np
import pytest
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature

_tmpdir = tempfile.mkdtemp()
#: SQLite por defecto; TEST_DATABASE_URL=postgresql+psycopg://<dueño>@... ejecuta la suite contra PostgreSQL real
#: (diferencias de dialecto: bloqueos, tipos, zonas horarias) Y con la seguridad por fila de verdad: el dueño crea
#: las tablas y los roles; la API se conecta con su usuario de mínimo privilegio (sin BYPASSRLS), así que una
#: consulta que olvidó declarar su empresa no ve nada y la prueba falla (`app/core/row_security.py`).
OWNER_URL = os.environ.get("TEST_DATABASE_URL", "")
APP_ROLE, APP_PASSWORD, PLATFORM_ROLE = "timeclock_test_app", "test-app-password", "timeclock_test_platform"


def _app_url(owner_url: str) -> str:
    from sqlalchemy.engine import make_url

    return make_url(owner_url).set(username=APP_ROLE, password=APP_PASSWORD).render_as_string(hide_password=False)


os.environ.update(
    {
        "DATABASE_URL": _app_url(OWNER_URL) if OWNER_URL else f"sqlite:///{_tmpdir}/test.db",
        "DATABASE_DIRECT_URL": OWNER_URL,
        # Con PostgreSQL, el usuario de la API y el rol de la plataforma de las pruebas (los crea `_roles` abajo);
        # con SQLite no hay roles (la guarda `_guard_tenant_tables` revisa el alcance de cada consulta).
        "DB_APP_USER": APP_ROLE,
        "DB_APP_PASSWORD": APP_PASSWORD if OWNER_URL else "",
        "DB_PLATFORM_ROLE": PLATFORM_ROLE,
        "DB_READONLY_PASSWORD": "",
        # Los usuarios iniciales del .env real no se crean en la BD de pruebas.
        "FIRST_ADMIN_EMAIL": "",
        "JWT_ALGORITHM": "ES256",
        "JWT_PRIVATE_KEY": ec.generate_private_key(ec.SECP256R1())
        .private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
        .decode(),
        "JWT_PREVIOUS_KEYS": "",
        "RATE_LIMIT_BACKEND": "memory",
        "API_WORKERS": "1",
        "DATA_ENCRYPTION_KEY": Fernet.generate_key().decode(),
        "FIRST_COMPANY_EMAIL": "",
        "FIRST_COMPANY_PASSWORD": "",
        "FACE_MATCH_THRESHOLD": "0.38",
        "FACE_MODELS_AUTO_DOWNLOAD": "false",
        # Las pruebas responden al reto al instante; test_capture_security lo vuelve a exigir.
        "FACE_CHALLENGE_MIN_SECONDS": "0",
        "FACE_FLASH_MIN_SECONDS": "0",
        # La depuración se prueba llamándola directamente (sin el hilo en segundo plano).
        "MAINTENANCE_INTERVAL_SECONDS": "0",
        # Los errores registrados se guardan llamando a error_reporter.flush() (sin hilo).
        "ERROR_REPORT_FLUSH_SECONDS": "0",
        # El consumo se guarda llamando a usage_meter.flush() (sin hilo).
        "USAGE_FLUSH_SECONDS": "0",
        # El rendimiento y las peticiones lentas se guardan llamando a perf_store.flush() (sin hilo).
        "PERF_FLUSH_SECONDS": "0",
        # Sin el bucket real (aunque el .env lo tenga): las pruebas usan uno falso (tests/test_image_storage.py).
        "GCS_BUCKET": "",
        "GCS_CREDENTIALS_FILE": "",
        "GCS_PREFIX": "test",
        # Base local de IP: sin red en las pruebas (nunca se descarga) y sin archivos salvo los que genera la prueba que
        # los necesita (tests/mmdb_support.py); sin ellos, las señales de red no se miden.
        "IP_DB_REFRESH_ENABLED": "false",
        "IP_COUNTRY_DB_PATH": f"{_tmpdir}/ipdb/country.mmdb",
        "IP_ASN_DB_PATH": f"{_tmpdir}/ipdb/asn.mmdb",
        # Respaldos y PITR como los deja una instalación nueva (aunque el .env de trabajo los encienda): cada prueba de
        # tests/test_db_operations.py y tests/test_pitr.py los enciende y los apunta a su carpeta temporal.
        "BACKUP_UPLOAD": "false",
        "BACKUP_DIR": f"{_tmpdir}/backups",
        "PITR_ENABLED": "false",
        "PITR_STATUS_DIR": f"{_tmpdir}/pitr",
    }
)

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import event  # noqa: E402

from app.core.clock import epoch_ms  # noqa: E402
from app.core.config import settings  # noqa: E402
from app.core.database import Base, SessionLocal, build_engine, engine  # noqa: E402
from app.core.db_roles import RolePlan, grant, provision  # noqa: E402
from app.core.db_schemas import ALL_SCHEMAS  # noqa: E402
from app.core.ip_intel import ip_intel  # noqa: E402
from app.core.object_storage import get_storage, use_storage  # noqa: E402
from app.core.perf_meter import perf_meter, slow_requests  # noqa: E402
from app.core.row_security import PLATFORM, SCOPE_KEY, TENANT_TABLES  # noqa: E402
from app.dependencies import get_pipeline  # noqa: E402
from app.facial_recognition import (  # noqa: E402
    FaceAnalysis,
    FaceValidationError,
    LivenessAction,
    StepTarget,
    phash,
)
from app.facial_recognition.burst import BurstLayout, BurstMalformed, Pulse  # noqa: E402
from app.facial_recognition.photometry import FLASH_PALETTE, FlashSample, emitted_chroma  # noqa: E402
from app.facial_recognition.pipeline import (  # noqa: E402
    DEFAULT_POLICY,
    Accessory,
    BurstAnalysis,
    BurstRules,
    FacePolicy,
    FlashCapture,
    accessories_error,
)
from app.i18n import strict  # noqa: E402
from app.main import app  # noqa: E402
from app.middleware.rate_limit import limiter  # noqa: E402
from app.models import DeviceStatus, Employee, ValidatorDevice  # noqa: E402
from app.models.catalog_seed import create_schema  # noqa: E402
from app.ocr import use_backend as use_ocr_backend  # noqa: E402
from app.services import flash_pacing  # noqa: E402
from app.services.attack_signatures import signature_cache  # noqa: E402
from app.services.bootstrap import create_admin_user, create_company_user  # noqa: E402
from app.services.catalog_service import clear_catalog_cache  # noqa: E402
from app.services.error_reporter import error_reporter  # noqa: E402
from app.services.face_gallery import face_galleries  # noqa: E402
from app.services.face_security import clear_threshold_cache  # noqa: E402
from app.services.face_service import clear_migration_blocks  # noqa: E402
from app.services.policy_service import clear_policy_cache  # noqa: E402
from app.services.qr_service import QrService  # noqa: E402
from app.services.usage_meter import usage_meter  # noqa: E402
from app.speech import use_backend  # noqa: E402
from tests.ocr_support import FakeOcr  # noqa: E402
from tests.speech_support import FakeSpeech, voice_clip  # noqa: E402
from tests.storage_support import FakeStorage  # noqa: E402

# Un texto que el catálogo de mensajes no tiene (o un parámetro que falta) hace fallar la prueba que lo provoca: en
# producción solo se registra (`app/i18n/render.py`). Con la cobertura del 100 %, cada mensaje se arma en alguna prueba.
strict(True)

COMPANY_EMAIL = "admin@empresa.com"
COMPANY_PASSWORD = "Admin1234"
ADMIN_EMAIL = "superadmin@plataforma.com"
ADMIN_PASSWORD = "Plataforma1234"
#: Validadores activos que permite la empresa de las pruebas (las del límite lo cambian).
TEST_VALIDATOR_LIMIT = 10


def _seeded(name: str) -> np.ndarray:
    seed = int(hashlib.sha256(name.encode()).hexdigest()[:8], 16)
    return np.random.default_rng(seed).standard_normal(128)


def face_vector(name: str) -> np.ndarray:
    """Embedding simulado de una persona.

    "juan~0.80~luz" es otra captura de juan con similitud EXACTA 0.80 a su rostro ("luz" es la
    variación: otra dirección). Se encadena: "juan~0.75~luz~0.85~noche" se parece 0.85 a
    "juan~0.75~luz" y 0.85 × 0.75 a juan (la variación es perpendicular a todo su linaje).
    """
    if "~" not in name:
        vector = _seeded(name)
        return (vector / np.linalg.norm(vector)).astype(np.float32)
    base, similarity, _ = name.rsplit("~", 2)
    lineage = [base]
    while "~" in lineage[-1]:
        lineage.append(lineage[-1].rsplit("~", 2)[0])
    basis, _ = np.linalg.qr(np.stack([face_vector(n).astype(np.float64) for n in lineage]).T)
    direction = _seeded(name)
    direction -= basis @ (basis.T @ direction)
    direction /= np.linalg.norm(direction)
    s = float(similarity)
    return (s * face_vector(base).astype(np.float64) + np.sqrt(1 - s * s) * direction).astype(np.float32)


def _analysis(name: str) -> FaceAnalysis:
    return FaceAnalysis(
        embedding=face_vector(name),
        detection_score=0.95,
        quality_score=0.9,
        sharpness=100.0,
        brightness=120.0,
        face_box=(0, 0, 100, 100),
    )


def _parse(image_bytes: bytes) -> tuple[str, str]:
    """(tipo, persona) de una imagen simulada; lo que sigue a "#" identifica el fotograma y lo que sigue a "@" su
    aspecto (huellas perceptuales)."""
    kind, _, rest = image_bytes.decode(errors="ignore").partition(":")
    return kind, rest.split("#")[0].split("@")[0]


def _phash(seed: str) -> int:
    return phash.signed(int(hashlib.sha256(seed.encode()).hexdigest()[:16], 16))


def _look(image_bytes: bytes) -> tuple[int, int]:
    """Huellas perceptuales (rostro, cuadro) de la captura simulada. Sin "@", cada captura se ve distinta (la luz y
    la pose nunca se repiten). "@<aspecto>" fija su aspecto: dos capturas con el mismo se ven iguales (reenvío) y
    "@<aspecto>^<n>" es la misma imagen modificada (recomprimida, con brillo): n bits distintos en las huellas."""
    text = image_bytes.decode(errors="ignore")
    if "@" not in text:
        return _phash(uuid.uuid4().hex), _phash(uuid.uuid4().hex)
    look, _, flips = text.split("@", 1)[1].split("#")[0].partition("^")
    mask = (1 << int(flips or 0)) - 1
    return phash.signed(_phash(look) ^ mask), phash.signed(_phash(look + ":cuadro") ^ mask)


def _traits(image_bytes: bytes, kind: str) -> dict:
    """Huellas y tamaño de la captura simulada.

    Cada captura es única (como el ruido del sensor de una cámara real) salvo que traiga "#<id>":
    entonces su huella es la de su contenido, para simular fotos fijas y reenvíos. `wide:` simula
    una captura de otra resolución (otra cámara o un archivo). "@" fija su aspecto (ver `_look`).
    """
    digest = hashlib.sha256(image_bytes).hexdigest() if b"#" in image_bytes else uuid.uuid4().hex
    face, frame = _look(image_bytes)
    return {
        "capture_digest": digest,
        "image_size": (1280, 720) if kind == "wide" else (640, 480),
        "face_phash": face,
        "frame_phash": frame,
    }


class FakePipeline:
    """Imágenes simuladas como texto:
    b"face:<persona>"           captura frontal válida (b"wide:<persona>": de otra resolución)
    b"glasses:<persona>"        con lentes      b"hat:<persona>"  con gorra
    b"spoof:<persona>"          foto o pantalla frente a la cámara
    b"exif:<persona>"           archivo con metadatos de cámara (no es una captura de la app)
    b"dim:<persona>"            captura justa (calidad 0.45: poca luz o algo desenfocada)
    b"noface" / b"multi"        sin rostro / varias personas
    b"turn-left:<persona>"      paso del reto: cabeza girada a la izquierda (también "turn-right",
                                "look-up", "look-down" y "closer"); con "spoof-" delante es una
                                pantalla y con "-moved" el rostro saltó de lugar
    b"flash-RED:<persona>"      fotograma del destello que refleja el color pedido; "flash-none" no
                                refleja nada (mucha luz) y "flash-wrong" refleja otros colores
    Antifraude 2a: b"screen:<persona>" frontal con patrón de pantalla (moiré) y b"smooth:<persona>" con el rostro más
    liso que el fondo; "flat-turn-left" (y los demás giros) se mueve como una superficie plana (sin paralaje);
    b"burst:<persona>" hoja de la ráfaga de una persona real ("burst-frozen", "burst-loop", "burst-cut",
    "burst-faceless", "burst-nopulse"; "burst-broken" mal formada, "burst-crash" el motor falla); sus recortes quietos
    son de <persona> (el consenso de identidad).
    `accessories_of` (CLIP de las referencias del registro) ve los mismos accesorios que `analyze_frontal`.
    """

    model_name = "fake-model"

    def analyze_frontal(
        self, image_bytes: bytes, *, policy: FacePolicy = DEFAULT_POLICY, enforce_accessories: bool = True
    ) -> FaceAnalysis:
        text = image_bytes.decode(errors="ignore")
        if text == "noface":
            raise FaceValidationError("NO_FACE")
        if text == "multi":
            raise FaceValidationError("MULTIPLE_FACES")
        kind, name = _parse(image_bytes)
        if kind == "exif" and policy.reject_foreign_images:
            raise FaceValidationError("IMAGE_NOT_FROM_CAMERA")
        detected = {"glasses": Accessory.GLASSES, "mask": Accessory.MASK, "hat": Accessory.HEADWEAR}.get(kind)
        # Como el motor real: lo detectado se informa si CLIP corre (alguna regla encendida o `report_accessories`) y
        # solo lo que la empresa bloquea rechaza.
        seen = (detected,) if detected is not None and policy.any_accessory else ()
        found = tuple(a for a in seen if policy.blocks(a))
        if found and enforce_accessories:
            raise accessories_error(found)
        quality = 0.45 if kind == "dim" else 0.9
        if quality < policy.min_quality:
            raise FaceValidationError("LOW_QUALITY", {"quality": quality, "required": policy.min_quality})
        # "lowreal": pasa el anti-spoofing por poco (señal SPOOF_PROB_LOW del motor de riesgo).
        real = {"spoof": 0.01, "lowreal": 0.07}.get(kind, 0.98) if policy.anti_spoofing else None
        return replace(
            _analysis(name),
            quality_score=quality,
            accessories_found=found,
            accessories_detected=seen,
            real_probability=real,
            landmarks=FRONTAL_POINTS,
            moire=30.0 if kind == "screen" else 10.0,
            noise_ratio=0.2 if kind == "smooth" else 1.0,
            **_traits(image_bytes, kind),
        )

    def analyze_step(
        self, image_bytes: bytes, action: LivenessAction, target: StepTarget, *, policy: FacePolicy = DEFAULT_POLICY
    ) -> FaceAnalysis:
        kind, name = _parse(image_bytes)
        if kind.startswith("broken-"):  # una captura que el motor no puede leer (cv2.error)
            raise cv2.error("captura ilegible")
        spoofed, moved, flat = kind.startswith("spoof-"), kind.endswith("-moved"), kind.startswith("flat-")
        kind = kind.removeprefix("spoof-").removeprefix("flat-").removesuffix("-moved")
        if kind == "exif" and policy.reject_foreign_images:
            raise FaceValidationError("IMAGE_NOT_FROM_CAMERA")
        if kind == "noface":
            raise FaceValidationError("NO_FACE")
        measured = STEP_VALUES.get(kind, 0.0) if kind == STEP_KINDS[action] else 0.0
        if measured < target.required(action):
            raise FaceValidationError("LIVENESS_STEP_NOT_DETECTED", {"measured": measured, "expected": action.value})
        real = (0.01 if spoofed else 0.98) if policy.anti_spoofing else None
        side = 140 if action == LivenessAction.MOVE_CLOSER else 100
        return replace(
            _analysis(name),
            face_box=(600, 400, side, side) if moved else (0, 0, side, side),
            real_probability=real,
            step_value=measured,
            landmarks=_step_points(action, flat=flat),
            **_traits(image_bytes, kind),
        )

    def analyze_flash(self, image_bytes: bytes, *, policy: FacePolicy = DEFAULT_POLICY) -> FlashCapture:
        kind, _ = _parse(image_bytes)
        if kind == "flash-crash":  # falla inesperada del motor
            raise RuntimeError("el motor se cayó")
        if kind == "flash-broken":
            raise cv2.error("captura ilegible")
        if kind == "flash-noface":
            raise FaceValidationError("NO_FACE")
        if kind == "flash-exif" and policy.reject_foreign_images:
            raise FaceValidationError("IMAGE_NOT_FROM_CAMERA")
        moved = kind.endswith("-moved")
        color = kind.removeprefix("flash-").removesuffix("-moved")
        return FlashCapture(
            sample=_flash_sample(color),
            face_box=(600, 400, 100, 100) if moved else (0, 0, 100, 100),
            image_size=_traits(image_bytes, kind)["image_size"],
            capture_digest=_traits(image_bytes, kind)["capture_digest"],
        )

    def accessories_of(
        self, image_bytes: bytes, *, policy: FacePolicy = DEFAULT_POLICY
    ) -> tuple[None, None, tuple[Accessory, ...]]:
        """CLIP de una referencia del registro: los mismos accesorios que `analyze_frontal` con la política dada."""
        kind, _ = _parse(image_bytes)
        detected = {"glasses": Accessory.GLASSES, "mask": Accessory.MASK, "hat": Accessory.HEADWEAR}.get(kind)
        return None, None, (detected,) if detected is not None and policy.blocks(detected) else ()

    def identity_of(self, image_bytes: bytes) -> np.ndarray | None:
        """Un fotograma del video de la verificación por voz: su vector, None sin rostro (o con varios); `crash` es una
        falla del motor y `broken` una imagen que OpenCV no puede leer."""
        text = image_bytes.decode(errors="ignore")
        if text in ("noface", "multi"):
            return None
        if text == "crash":
            raise RuntimeError("el motor se cayó")
        if text == "broken":
            raise cv2.error("imagen ilegible")
        _, name = _parse(image_bytes)
        return face_vector(name)

    def analyze_burst(self, data: bytes, layout: BurstLayout, rules: BurstRules) -> BurstAnalysis:
        kind, name = _parse(data)
        if kind == "burst-broken":
            raise BurstMalformed("size")
        if kind == "burst-crash":
            raise RuntimeError("el motor se cayó")
        frames = layout.count
        faceless = kind == "burst-faceless"
        return BurstAnalysis(
            frames=frames,
            faceless=frames if faceless else 0,
            jumps=2 if kind == "burst-cut" else 0,
            motion=0.0 if kind == "burst-frozen" else 2.5,
            repeats=3 if kind == "burst-loop" else 0,
            pulse=Pulse(snr_db=-8.0 if kind == "burst-nopulse" else 1.5, bpm=72.0),
            anchors=(face_vector(name),),
            # El consenso de identidad: los mejores recortes quietos de la persona de la hoja (ninguno sin rostro).
            identity=() if faceless else (face_vector(name),) * min(rules.identity_frames, len(layout.indices("H"))),
        )


def _project(points: np.ndarray, yaw: float = 0.0, pitch: float = 0.0) -> np.ndarray:
    """Puntos 3D (cm) de un rostro vistos por una cámara a 40 cm (perspectiva)."""
    a, b = np.radians([yaw, pitch])
    ry = np.array([[np.cos(a), 0, np.sin(a)], [0, 1, 0], [-np.sin(a), 0, np.cos(a)]])
    rx = np.array([[1, 0, 0], [0, np.cos(b), -np.sin(b)], [0, np.sin(b), np.cos(b)]])
    p = points @ (rx @ ry).T
    z = 40.0 - p[:, 2]
    return np.stack([600 * p[:, 0] / z + 320, 600 * p[:, 1] / z + 240], axis=1)


#: Ojos, nariz y comisuras de un rostro (cm, la nariz 2.4 cm al frente): de frente y al girar (paralaje real).
FACE_3D = np.array([[-3.15, 0, 0], [3.15, 0, 0], [0, 3.6, 2.4], [-2.5, 6.4, 0.6], [2.5, 6.4, 0.6]])
FRONTAL_POINTS = _project(FACE_3D)


def _step_points(action: LivenessAction, *, flat: bool) -> np.ndarray:
    """Los puntos de un paso: un rostro real gira en 3D; una foto plana inclinada sigue una afín (sin paralaje)."""
    if flat:
        return FRONTAL_POINTS @ np.array([[1.0, 0.0], [0.35, 1.0]]).T
    yaw = {LivenessAction.TURN_LEFT: 25.0, LivenessAction.TURN_RIGHT: -25.0}.get(action, 0.0)
    pitch = {LivenessAction.LOOK_UP: 15.0, LivenessAction.LOOK_DOWN: -15.0}.get(action, 0.0)
    return _project(FACE_3D, yaw=yaw, pitch=pitch)


def burst_files(person: str = "juan", *, kind: str = "burst", meta: dict | None = None) -> list:
    """La hoja de la ráfaga (simulada) y su descripción válida para lo que pide el servidor (tramo quieto a 10 cps y
    luego el de movimiento)."""
    holds, moves = settings.FACE_BURST_HOLD_FRAMES, settings.FACE_BURST_MOVE_FRAMES
    description = meta or {
        "v": 1,
        "tile": settings.FACE_BURST_TILE_PX,
        "cols": 8,
        "t": [100 * i for i in range(holds)] + [5000 + 100 * i for i in range(moves)],
        "s": "H" * holds + "M" * moves,
    }
    return [
        ("burst", ("burst.jpg", f"{kind}:{person}".encode(), "image/jpeg")),
        ("burst_meta", (None, json.dumps(description))),
    ]


#: Imagen simulada de cada paso del reto y lo que mide (cómodo sobre el mínimo, bajo el tope).
STEP_KINDS = {
    LivenessAction.TURN_LEFT: "turn-left",
    LivenessAction.TURN_RIGHT: "turn-right",
    LivenessAction.LOOK_UP: "look-up",
    LivenessAction.LOOK_DOWN: "look-down",
    LivenessAction.MOVE_CLOSER: "closer",
}
STEP_VALUES = {"turn-left": 0.25, "turn-right": 0.25, "look-up": 0.12, "look-down": 0.12, "closer": 1.4}
#: Lo contrario de cada paso (para simular que la persona no hizo lo que se pidió).
WRONG_STEP = {
    "TURN_LEFT": "turn-right",
    "TURN_RIGHT": "turn-left",
    "LOOK_UP": "look-down",
    "LOOK_DOWN": "look-up",
    "MOVE_CLOSER": "turn-left",
}
_SKIN = np.array([0.45, 0.33, 0.22])


def _flash_sample(color: str) -> FlashSample:
    """Un rostro real refleja el color (el fondo, lejos, casi no); "none" no refleja nada; "wrong", lo
    contrario de lo que pinta la pantalla; "flat", igual en el rostro y el fondo (pantalla o papel)."""
    if color in FLASH_PALETTE:
        face, background = 0.7 * _SKIN + 0.3 * emitted_chroma(color), 0.95 * _SKIN + 0.05 * emitted_chroma(color)
    elif color.startswith("wrong-") and color.removeprefix("wrong-") in FLASH_PALETTE:
        opposite = 1 - emitted_chroma(color.removeprefix("wrong-"))
        face = background = 0.7 * _SKIN + 0.3 * opposite / opposite.sum()
    elif color.startswith("flat-") and color.removeprefix("flat-") in FLASH_PALETTE:
        # Una pantalla o un papel frente a la cámara: el destello tiñe IGUAL rostro y fondo (prototipo P1).
        face = background = 0.7 * _SKIN + 0.3 * emitted_chroma(color.removeprefix("flat-"))
    else:
        face = background = _SKIN
    r, g, b = (float(v) for v in face)
    br, bg, bb = (float(v) for v in background)
    return FlashSample(face=(r, g, b), background=(br, bg, bb))


#: Las sesiones que abren las PRUEBAS para preparar o revisar datos son de la plataforma (cruzan empresas). Las de
#: la API no: cada petición empieza sin alcance (`get_db`) y lo declara al autenticar, y el código en segundo
#: plano lo declara explícito (`platform_session`).
SessionLocal.configure(info={SCOPE_KEY: PLATFORM})
#: Dueño de la base: crea y borra las tablas de cada prueba (la API no tiene permisos de DDL en PostgreSQL).
owner_engine = build_engine(OWNER_URL) if OWNER_URL else engine
_TENANT_NAMES = "|".join(sorted(name.split(".")[1] for name in TENANT_TABLES))
_TOUCHES_TENANT = re.compile(rf"\b(?:FROM|JOIN|INTO|UPDATE)\s+(?:\w+\.)?\"?(?:{_TENANT_NAMES})\"?\b", re.IGNORECASE)


@event.listens_for(engine, "before_cursor_execute")
def _guard_tenant_tables(conn, _cursor, statement, _params, _context, _many):
    """SQLite no tiene seguridad por fila: esta guarda aplica su regla a cada consulta de la suite rápida. Una
    sentencia que toca una tabla de empresa en una transacción SIN alcance (ni empresa ni plataforma) es un camino
    de la API que olvidó declararlo: en PostgreSQL no vería nada; aquí la prueba falla."""
    unscoped = engine.dialect.name == "sqlite" and conn.info.get(SCOPE_KEY, PLATFORM) is None
    data = statement.lstrip()[:6].upper() in ("SELECT", "INSERT", "UPDATE", "DELETE")
    if unscoped and data and _TOUCHES_TENANT.search(statement):
        raise AssertionError(f"Consulta a una tabla de empresa sin alcance de seguridad por fila: {statement[:300]}")


def _roles() -> RolePlan:
    """PostgreSQL: los roles de las pruebas (los mismos que aprovisiona `migrate`)."""
    plan = RolePlan.from_settings()
    with owner_engine.begin() as conn:
        for schema in ALL_SCHEMAS:
            conn.exec_driver_sql(f"CREATE SCHEMA IF NOT EXISTS {schema}")
        provision(conn, plan)
    return plan


#: Con PostgreSQL, los roles; cada prueba vuelve a crear sus tablas y les da sus permisos como en producción
#: (tabla por tabla, nunca a las particiones: `db_roles.grant`).
ROLE_PLAN = _roles() if OWNER_URL else None


@pytest.fixture(autouse=True)
def _db():
    Base.metadata.drop_all(owner_engine)
    create_schema(owner_engine)
    if ROLE_PLAN is not None:
        with owner_engine.begin() as conn:
            grant(conn, ROLE_PLAN)
            # Solo en las pruebas: algunas simulan un cambio de catálogo (en producción los catálogos cambian solo
            # con migraciones y la API únicamente los lee).
            conn.exec_driver_sql(
                f"GRANT INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA catalog TO {ROLE_PLAN.app}, {ROLE_PLAN.platform}"
            )
    limiter.reset()
    clear_policy_cache()
    clear_catalog_cache()
    error_reporter.clear()  # sin errores pendientes de la prueba anterior
    usage_meter.clear()  # ni consumo pendiente
    perf_meter.clear()  # ni rendimiento ni peticiones lentas pendientes
    slow_requests.clear()
    clear_migration_blocks()
    clear_threshold_cache()
    signature_cache.clear()  # la lista de bloqueo en memoria de la prueba anterior
    # Galerías faciales en memoria (el 1:N en cada 1:1 las usa sin revalidar unos minutos: los ids se repiten entre
    # pruebas) y la base local de IP (solo existe en las pruebas que la generan).
    face_galleries.clear()
    for path in (settings.ip_country_db, settings.ip_asn_db):
        path.unlink(missing_ok=True)
    ip_intel.close()
    # Bucket de imágenes falso en memoria: ninguna prueba toca el real (las imágenes nunca van a la BD).
    use_storage(FakeStorage())
    # Motor de voz falso: ninguna prueba decodifica video ni carga el modelo (tests/speech_support.py).
    use_backend(FakeSpeech())
    # Motor de OCR falso: ninguna prueba ejecuta Tesseract (tests/ocr_support.py).
    use_ocr_backend(FakeOcr())
    with SessionLocal() as db:
        company = create_company_user(db, COMPANY_EMAIL, COMPANY_PASSWORD).company
        create_admin_user(db, ADMIN_EMAIL, ADMIN_PASSWORD)
        # La empresa de las pruebas tiene el módulo de Integraciones (API); las pruebas sin él lo apagan.
        company.api_enabled = True  # type: ignore[union-attr]
        # Y el de validadores (una empresa nueva empieza en 0: sin el módulo); las pruebas del límite lo cambian.
        company.max_validators = TEST_VALIDATOR_LIMIT  # type: ignore[union-attr]
        db.commit()
    yield


# Los validadores solo operan desde una tableta o un teléfono (política por defecto): el cliente de
# pruebas se presenta como iPhone. Las pruebas de dispositivo cambian la cabecera explícitamente.
IPHONE_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/18.0 Mobile/15E148 Safari/604.1"
)
DESKTOP_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36"
)


@pytest.fixture
def bucket():
    """El bucket falso de la prueba (en memoria, tests/storage_support.py): para revisar lo que se guardó
    o provocar sus fallas."""
    storage = get_storage()
    assert isinstance(storage, FakeStorage)
    return storage


@pytest.fixture
def speech() -> FakeSpeech:
    """El motor de voz falso de la prueba: para ver qué oyó o provocar sus fallas."""
    from app.speech import backend

    engine = backend()
    assert isinstance(engine, FakeSpeech)
    return engine


@pytest.fixture
def ocr() -> FakeOcr:
    """El motor de OCR falso de la prueba: para configurar lo que «lee» de un documento o provocar sus fallas."""
    from app.ocr import backend

    engine = backend()
    assert isinstance(engine, FakeOcr)
    return engine


@pytest.fixture
def client():
    app.dependency_overrides[get_pipeline] = FakePipeline
    with TestClient(app, headers={"User-Agent": IPHONE_UA}) as test_client:
        yield test_client
    app.dependency_overrides.clear()


#: Llave del dispositivo de pruebas (como la que genera la webapp con WebCrypto, no exportable).
TEST_DEVICE_KEY = ec.generate_private_key(ec.SECP256R1())


def device_proof(nonce: str, key: ec.EllipticCurvePrivateKey = TEST_DEVICE_KEY, name: str = "Safari · iPadOS") -> dict:
    """Prueba de posesión del dispositivo: llave pública (SPKI) y firma r||s del reto, en base64."""
    public = key.public_key().public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
    r, s = decode_dss_signature(key.sign(nonce.encode(), ec.ECDSA(hashes.SHA256())))
    raw = r.to_bytes(32, "big") + s.to_bytes(32, "big")
    return {
        "public_key": base64.b64encode(public).decode(),
        "nonce": nonce,
        "signature": base64.b64encode(raw).decode(),
        "name": name,
    }


def login(client: TestClient, email: str, password: str) -> dict[str, str]:
    """Inicia sesión. Un validador firma el reto con el dispositivo de pruebas, que se da por
    autorizado por su empresa (las pruebas de dispositivos recorren ese flujo completo)."""
    body = {"email": email, "password": password}
    response = client.post("/api/auth/login", json=body)
    if response.status_code == 403 and response.json()["code"] == "DEVICE_PROOF_REQUIRED":
        body["device"] = device_proof(response.json()["errors"][0]["details"]["nonce"])
        response = client.post("/api/auth/login", json=body)
        if response.status_code == 403 and response.json()["code"] == "DEVICE_PENDING_APPROVAL":
            with SessionLocal() as db:
                device = db.get(ValidatorDevice, response.json()["errors"][0]["details"]["device_id"])
                device.status = DeviceStatus.APPROVED
                db.commit()
            response = client.post("/api/auth/login", json=body)
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['data']['access_token']}"}


@pytest.fixture
def company_headers(client):
    return login(client, COMPANY_EMAIL, COMPANY_PASSWORD)


@pytest.fixture
def admin_headers(client):
    """Administrador de la plataforma (da de alta empresas)."""
    return login(client, ADMIN_EMAIL, ADMIN_PASSWORD)


#: Segundo ADMIN de la plataforma: aprueba lo que relaja la seguridad de una empresa (regla de dos personas).
SECOND_ADMIN_EMAIL = "segundo.admin@plataforma.com"


def second_admin(client: TestClient) -> dict[str, str]:
    """Sesión de un segundo ADMIN (se crea la primera vez)."""
    from app.repositories.user_repository import UserRepository
    from app.services.bootstrap import create_admin_user

    with SessionLocal() as db:
        if UserRepository(db).get_by_email(SECOND_ADMIN_EMAIL) is None:
            create_admin_user(db, SECOND_ADMIN_EMAIL, ADMIN_PASSWORD)
    return login(client, SECOND_ADMIN_EMAIL, ADMIN_PASSWORD)


def create_company(client, admin_headers, *, rfc="PNO120315AB1", admin_email="admin@panificadora.com", **extra):
    """Alta de empresa por el ADMIN con su primer administrador."""
    body = {
        "name": "Panificadora del Norte",
        "legal_name": "Panificadora del Norte, S.A. de C.V.",
        "rfc": rfc,
        "phone": "6621234567",
        "admin_email": admin_email,
        "admin_password": "Empresa1234",
        **extra,
    }
    return client.post("/api/admin/companies", json=body, headers=admin_headers)


def create_employee(
    client,
    headers,
    *,
    number="EMP-001",
    email="juan@empresa.com",
    headwear_exempt: bool = False,
    rfc: str | None = None,
    phone: str | None = None,
):
    return client.post(
        "/api/employees",
        json={
            "first_name": "Juan",
            "last_name": "Pérez",
            "birth_date": "1990-05-10",
            "employee_number": number,
            "rfc": rfc or rfc_for(number),
            "curp": curp_for(number),
            "nss": nss_for(number),
            "phone": phone or phone_for(number),
            "email": email,
            "password": "Empleado123",
            "headwear_exempt": headwear_exempt,
        },
        headers=headers,
    )


def phone_for(number: str) -> str:
    """Celular válido y distinto por número de empleado (el teléfono es único por persona)."""
    digits = int(hashlib.sha256(number.encode()).hexdigest(), 16) % 9_000_000 + 1_000_000
    return f"662{digits}"


def rfc_for(number: str) -> str:
    """RFC válido y distinto por número de empleado (fecha = 1990-05-10, la de create_employee)."""
    digest = hashlib.sha256(number.encode()).hexdigest().upper()
    return f"PEXJ900510{digest[:2]}{int(digest[2], 16) % 10}"


def curp_for(number: str) -> str:
    """CURP válida (dígito verificador correcto) y distinta por número; nacido el 1990-05-10."""
    from app.schemas.validators import curp_check_digit

    consonants = "BCDFGHJKLMNPQRSTVWXZ"
    digest = hashlib.sha256(number.encode()).digest()
    first17 = "PEXJ900510HSR" + "".join(consonants[b % len(consonants)] for b in digest[:3]) + "0"
    return first17 + curp_check_digit(first17)


def nss_for(number: str) -> str:
    """NSS válido (Luhn) y distinto por número."""
    from app.schemas.validators import luhn_valid

    base = str(int(hashlib.sha256(number.encode()).hexdigest(), 16))[:10]
    return next(base + d for d in "0123456789" if luhn_valid(base + d))


def turn_files(challenge: dict, person: str = "juan", *, image: str = "turn:{person}", wrong: bool = False) -> list:
    """Una captura por cada movimiento del reto, en orden.

    `image` es la captura simulada con "turn" en lugar del movimiento (p. ej. "spoof-turn:{person}");
    `wrong=True` hace lo contrario de lo pedido.
    """
    files = []
    for i, action in enumerate(challenge["actions"] or [challenge["action"]]):
        kind = WRONG_STEP[action] if wrong else STEP_KINDS[LivenessAction(action)]
        content = image.format(person=person).replace("turn", kind, 1)
        files.append(("challenge_image", (f"t{i}.jpg", content.encode(), "image/jpeg")))
    return files


#: Código de cada color del destello por su #RRGGBB (lo que envía el reto).
FLASH_CODES = {f"#{r:02X}{g:02X}{b:02X}": code for code, (r, g, b) in FLASH_PALETTE.items()}


def flash_files(
    challenge: dict, person: str = "juan", *, image: str = "flash-{color}:{person}", paced: bool = True
) -> list:
    """Una captura por cada color del destello, en orden (`{color}` es el código del color pedido).

    Con el destello dictado por el servidor (antifraude 2a: el reto trae `flash_pace` y no sus colores) se recorre el
    protocolo como la app por el canal en vivo (`flash_pacing.advance`, cada captura comprometida 400 ms después de
    revelarse su color) y va también el comprobante. `paced=False`: la app sin canal (los colores de respaldo, sin
    comprobante)."""
    pace = challenge.get("flash_pace")
    if not pace:
        colors = challenge["flash"]
        return [_flash_part(i, image.format(color=FLASH_CODES[c], person=person)) for i, c in enumerate(colors)]
    user_id = json.loads(flash_pacing._SEAL.decrypt(pace["token"].encode()))["u"]
    if not paced:
        colors = flash_pacing.fallback_colors(pace["token"], user_id, epoch_ms())
        return [_flash_part(i, image.format(color=FLASH_CODES[c], person=person)) for i, c in enumerate(colors)]
    now = epoch_ms()
    step = flash_pacing.advance(pace["token"], user_id, None, now)
    parts = []
    while not step.done:
        content = image.format(color=FLASH_CODES[step.color or ""], person=person).encode()
        parts.append(_flash_part(len(parts), content.decode()))
        now += 400
        step = flash_pacing.advance(step.token, user_id, hashlib.sha256(content).hexdigest(), now)
    return [*parts, ("flash_receipt", (None, step.token))]


def _flash_part(index: int, content: str) -> tuple:
    return ("flash_image", (f"c{index}.jpg", content.encode(), "image/jpeg"))


def enrollment_challenge(client, headers) -> dict:
    """El reto del REGISTRO facial como lo pide la app (`purpose=ENROLLMENT`): siempre los cuatro movimientos de la
    cabeza (decisión del dueño, 2026-10-07); un reto de verificación no sirve para registrarse."""
    return client.post("/api/face/challenge", params={"purpose": "ENROLLMENT"}, headers=headers).json()["data"]


def initial_photo(client, headers, image: bytes = b"face:juan"):
    """El paso 1 del registro facial como lo hace la app (`POST /enrollment/photo`, decisión del dueño, 2026-10-07): la
    foto inicial aceptada queda guardada como borrador; sin ella, las capturas (paso 2) responden 409."""
    return client.post("/api/enrollment/photo", files={"images": ("p.jpg", image, "image/jpeg")}, headers=headers)


def _person_of(frontal, fallback: str) -> str:
    """La persona de la primera captura con rostro de una toma simulada (`face:juan`, `mask:juan` → juan)."""
    named = [image.decode(errors="ignore").partition(":")[2] for image in frontal if b":" in image]
    return named[0] if named else fallback


def submit_enrollment(
    client,
    headers,
    *,
    frontal=(b"face:juan", b"face:juan", b"face:juan"),
    turn_person="juan",
    voice=True,
    photo: bytes | None = None,
):
    """El registro facial como lo hace la app: la foto inicial (paso 1; `photo`, por omisión un rostro limpio de la
    persona de las capturas), las fotos con la prueba de vida completa (paso 2) y, si las acepta y la política lo
    exige, las preguntas en video (paso 3, `complete_voice`, con la misma persona de la toma): el registro queda listo
    para la revisión. `voice=False` deja la sesión de voz abierta (las pruebas de la verificación por voz la recorren a
    mano). Devuelve la respuesta de las fotos o, si la foto inicial no pasó (409 de un registro ya enviado o aprobado,
    503 sin bucket), la de la foto: el mismo código que daría el envío."""
    taken = initial_photo(client, headers, photo or f"face:{_person_of(frontal, turn_person)}".encode())
    if taken.status_code != 201:
        return taken
    challenge = enrollment_challenge(client, headers)
    files = [("images", (f"f{i}.jpg", f, "image/jpeg")) for i, f in enumerate(frontal)]
    files += turn_files(challenge, turn_person)
    response = client.post(
        "/api/enrollment/face", data={"challenge_id": challenge["challenge_id"]}, files=files, headers=headers
    )
    if voice and response.status_code == 201:
        complete_voice(client, headers, response.json()["data"], person=turn_person)
    return response


def qr_content(employee_id: int, lifetime_seconds: int = 30) -> str:
    """Emite un QR dinámico del empleado (como su teléfono) y devuelve lo que lee el escáner."""
    with SessionLocal() as db:
        employee = db.get(Employee, employee_id)
        assert employee is not None
        _, content = QrService(db).issue(employee, lifetime_seconds)
        db.commit()
        return content


def complete_voice(client, headers, submitted: dict, person: str = "juan"):
    """Responde las preguntas en video de un registro recién enviado como lo haría la persona (con la verdad): la
    respuesta esperada sale del token sellado de la sesión (solo las pruebas lo abren). Devuelve la última respuesta
    (o None si el registro no llevó verificación por voz)."""
    from app.services.voice_questions import _open

    voice = submitted.get("voice")
    if voice is None:
        return None
    response = None
    token = voice["token"]
    for question in voice["questions"]:
        session = _open(token)
        assert session is not None
        expected = session.questions[question["position"]].answer.split("\n")[0]
        response = answer_voice(client, headers, token, question["position"], voice_clip(person, expected))
        assert response.status_code == 200, response.text
        token = response.json()["data"]["token"]
    return response


def answer_voice(client, headers, token: str, position: int, clip: bytes):
    """Una respuesta en video a la pregunta `position` de la sesión `token`."""
    files = [("clip", ("answer.webm", clip, "video/webm"))]
    data = {"token": token, "position": str(position)}
    return client.post("/api/enrollment/voice/answer", data=data, files=files, headers=headers)


def approved_employee(client, company_headers, **kwargs) -> dict[str, str]:
    """Crea un empleado, registra su rostro (fotos y, con la política, las preguntas en video) y COMPANY lo aprueba.
    Devuelve headers del empleado."""
    assert create_employee(client, company_headers, **kwargs).status_code == 201
    headers = login(client, kwargs.get("email", "juan@empresa.com"), "Empleado123")
    enrollment = submit_enrollment(client, headers)
    assert enrollment.status_code == 201, enrollment.text
    approve = client.post(
        f"/api/enrollments/{enrollment.json()['data']['enrollment_id']}/approve", headers=company_headers
    )
    assert approve.status_code == 200, approve.text
    return headers
