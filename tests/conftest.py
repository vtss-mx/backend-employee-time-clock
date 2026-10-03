"""Pruebas con SQLite y un pipeline facial falso (no requiere modelos ONNX)."""

import base64
import hashlib
import os
import tempfile
import uuid
from dataclasses import replace

import numpy as np
import pytest
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature

_tmpdir = tempfile.mkdtemp()
os.environ.update(
    {
        # SQLite por defecto; TEST_DATABASE_URL=postgresql+psycopg://... ejecuta la suite contra
        # PostgreSQL real (detecta diferencias de dialecto: bloqueos, tipos, zonas horarias).
        "DATABASE_URL": os.environ.get("TEST_DATABASE_URL") or f"sqlite:///{_tmpdir}/test.db",
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
    }
)

from fastapi.testclient import TestClient  # noqa: E402

from app.core.database import Base, SessionLocal, engine  # noqa: E402
from app.dependencies import get_pipeline  # noqa: E402
from app.facial_recognition import FaceAnalysis, FaceValidationError, TurnDirection  # noqa: E402
from app.facial_recognition.pipeline import DEFAULT_POLICY, Accessory, FacePolicy, accessories_error  # noqa: E402
from app.main import app  # noqa: E402
from app.middleware.rate_limit import limiter  # noqa: E402
from app.models import DeviceStatus, Employee, ValidatorDevice  # noqa: E402
from app.models.catalog_seed import create_schema  # noqa: E402
from app.services.bootstrap import create_admin_user, create_company_user  # noqa: E402
from app.services.catalog_service import clear_catalog_cache  # noqa: E402
from app.services.policy_service import clear_policy_cache  # noqa: E402
from app.services.qr_service import QrService  # noqa: E402

COMPANY_EMAIL = "admin@empresa.com"
COMPANY_PASSWORD = "Admin1234"
ADMIN_EMAIL = "superadmin@plataforma.com"
ADMIN_PASSWORD = "Plataforma1234"


def _embedding(name: str) -> np.ndarray:
    seed = int(hashlib.sha256(name.encode()).hexdigest()[:8], 16)
    vector = np.random.default_rng(seed).standard_normal(128).astype(np.float32)
    return vector / np.linalg.norm(vector)


def _analysis(name: str) -> FaceAnalysis:
    return FaceAnalysis(
        embedding=_embedding(name),
        detection_score=0.95,
        quality_score=0.9,
        sharpness=100.0,
        brightness=120.0,
        face_box=(0, 0, 100, 100),
    )


def _parse(image_bytes: bytes) -> tuple[str, str]:
    """(tipo, persona) de una imagen simulada; lo que sigue a "#" identifica el fotograma."""
    kind, _, rest = image_bytes.decode(errors="ignore").partition(":")
    return kind, rest.split("#")[0]


def _traits(image_bytes: bytes, kind: str) -> dict:
    """Huella y tamaño de la captura simulada.

    Cada captura es única (como el ruido del sensor de una cámara real) salvo que traiga "#<id>":
    entonces su huella es la de su contenido, para simular fotos fijas y reenvíos. `wide:` simula
    una captura de otra resolución (otra cámara o un archivo).
    """
    digest = hashlib.sha256(image_bytes).hexdigest() if b"#" in image_bytes else uuid.uuid4().hex
    return {"capture_digest": digest, "image_size": (1280, 720) if kind == "wide" else (640, 480)}


class FakePipeline:
    """Imágenes simuladas como texto:
    b"face:<persona>"           captura frontal válida (b"wide:<persona>": de otra resolución)
    b"glasses:<persona>"        con lentes      b"hat:<persona>"  con gorra
    b"spoof:<persona>"          foto o pantalla frente a la cámara
    b"exif:<persona>"           archivo con metadatos de cámara (no es una captura de la app)
    b"noface" / b"multi"        sin rostro / varias personas
    b"turn-left:<persona>"      cabeza girada a la izquierda (b"turn-right:..." derecha); con
                                "spoof-" delante es una pantalla y con "-moved" el rostro saltó de lugar
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
        found = (detected,) if detected is not None and policy.blocks(detected) else ()
        if found and enforce_accessories:
            raise accessories_error(found)
        real = (0.01 if kind == "spoof" else 0.98) if policy.anti_spoofing else None
        return replace(_analysis(name), accessories_found=found, real_probability=real, **_traits(image_bytes, kind))

    def analyze_turn(
        self, image_bytes: bytes, direction: TurnDirection, *, policy: FacePolicy = DEFAULT_POLICY
    ) -> FaceAnalysis:
        kind, name = _parse(image_bytes)
        spoofed, moved = kind.startswith("spoof-"), kind.endswith("-moved")
        kind = kind.removeprefix("spoof-").removesuffix("-moved")
        if kind == "exif" and policy.reject_foreign_images:
            raise FaceValidationError("IMAGE_NOT_FROM_CAMERA")
        expected = "turn-left" if direction == TurnDirection.LEFT else "turn-right"
        if kind != expected:
            raise FaceValidationError("LIVENESS_TURN_NOT_DETECTED")
        real = (0.01 if spoofed else 0.98) if policy.anti_spoofing else None
        return replace(
            _analysis(name),
            face_box=(600, 400, 100, 100) if moved else (0, 0, 100, 100),
            real_probability=real,
            **_traits(image_bytes, kind),
        )


@pytest.fixture(autouse=True)
def _db():
    Base.metadata.drop_all(engine)
    create_schema(engine)
    limiter.reset()
    clear_policy_cache()
    clear_catalog_cache()
    with SessionLocal() as db:
        create_company_user(db, COMPANY_EMAIL, COMPANY_PASSWORD)
        create_admin_user(db, ADMIN_EMAIL, ADMIN_PASSWORD)
    yield


# Los empleados solo pueden usar la app desde un teléfono (política por defecto): el cliente de
# pruebas se presenta como iPhone. Las pruebas de dispositivo cambian la cabecera explícitamente.
IPHONE_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/18.0 Mobile/15E148 Safari/604.1"
)
DESKTOP_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36"
)


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
    """Una captura con la cabeza girada por cada giro del reto, en orden.

    `image` es la captura simulada con "turn" en lugar del lado (p. ej. "spoof-turn:{person}");
    `wrong=True` gira al lado contrario del pedido.
    """
    files = []
    for i, action in enumerate(challenge["actions"] or [challenge["action"]]):
        left = (action == "TURN_LEFT") != wrong
        content = image.format(person=person).replace("turn", "turn-left" if left else "turn-right", 1)
        files.append(("challenge_image", (f"t{i}.jpg", content.encode(), "image/jpeg")))
    return files


def submit_enrollment(client, headers, *, frontal=(b"face:juan", b"face:juan", b"face:juan"), turn_person="juan"):
    challenge = client.post("/api/face/challenge", headers=headers).json()["data"]
    files = [("images", (f"f{i}.jpg", f, "image/jpeg")) for i, f in enumerate(frontal)]
    files += turn_files(challenge, turn_person)
    return client.post(
        "/api/enrollment/face", data={"challenge_id": challenge["challenge_id"]}, files=files, headers=headers
    )


def qr_content(employee_id: int, lifetime_seconds: int = 30) -> str:
    """Emite un QR dinámico del empleado (como su teléfono) y devuelve lo que lee el escáner."""
    with SessionLocal() as db:
        employee = db.get(Employee, employee_id)
        assert employee is not None
        _, content = QrService(db).issue(employee, lifetime_seconds)
        db.commit()
        return content


def approved_employee(client, company_headers, **kwargs) -> dict[str, str]:
    """Crea un empleado, registra su rostro y COMPANY lo aprueba. Devuelve headers del empleado."""
    assert create_employee(client, company_headers, **kwargs).status_code == 201
    headers = login(client, kwargs.get("email", "juan@empresa.com"), "Empleado123")
    enrollment = submit_enrollment(client, headers)
    assert enrollment.status_code == 201, enrollment.text
    approve = client.post(
        f"/api/enrollments/{enrollment.json()['data']['enrollment_id']}/approve", headers=company_headers
    )
    assert approve.status_code == 200, approve.text
    return headers
