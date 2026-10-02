"""Pruebas unitarias de módulos internos: decodificación de imágenes, pipeline facial (con un
motor simulado), validadores, cifrado, recursos del sistema y tolerancia del rate limit."""

import io
from datetime import date, timedelta

import numpy as np
import pytest
from PIL import Image

from app.core.crypto import decrypt_bytes, encrypt_bytes
from app.core.system import available_cpus, default_api_workers
from app.facial_recognition import FaceValidationError, TurnDirection
from app.facial_recognition.accessories import AccessoryScores
from app.facial_recognition.engine import DetectedFace, FaceEngine, FaceLandmarks
from app.facial_recognition.image_utils import decode_image
from app.facial_recognition.pipeline import FacePipeline, FacePolicy, QualityThresholds
from app.schemas import validators


def image_bytes(size=(320, 320), fmt="JPEG", color=(120, 120, 120)) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", size, color).save(buffer, format=fmt)
    return buffer.getvalue()


# ---------------------------------------------------------------- decodificación


def test_decode_image_validates_format_and_size():
    bgr = decode_image(image_bytes(), min_dimension=160, max_dimension=4096)
    assert bgr.shape == (320, 320, 3) and bgr.dtype == np.uint8
    assert decode_image(image_bytes(fmt="PNG"), min_dimension=160, max_dimension=4096).shape[2] == 3

    cases = [
        (b"", "EMPTY_IMAGE"),
        (b"no es una imagen", "INVALID_IMAGE"),
        (image_bytes(fmt="GIF"), "INVALID_IMAGE_FORMAT"),
        (image_bytes(size=(100, 100)), "IMAGE_TOO_SMALL"),
        (image_bytes(size=(5000, 200)), "IMAGE_TOO_LARGE"),
    ]
    for data, code in cases:
        with pytest.raises(FaceValidationError) as exc:
            decode_image(data, min_dimension=160, max_dimension=4096)
        assert exc.value.code == code


# ---------------------------------------------------------------- pipeline


def landmarks(yaw_shift: float = 0.0, roll_dy: float = 0.0, nose_y: float = 170.0) -> FaceLandmarks:
    return FaceLandmarks(
        eye_a=(130.0, 140.0),
        eye_b=(190.0, 140.0 + roll_dy),
        nose=(160.0 + yaw_shift, nose_y),
        mouth_a=(138.0, 200.0),
        mouth_b=(182.0, 200.0),
    )


def face(score=0.95, size=150, x=85, lm=None) -> DetectedFace:
    return DetectedFace(x=x, y=85, width=size, height=size, score=score, landmarks=lm or landmarks(), raw=np.zeros(15))


class StubEngine(FaceEngine):
    model_name = "stub"
    embedding_dim = 4

    def __init__(self, faces, aligned=None):
        self.faces = faces
        rng = np.random.default_rng(0)
        self.aligned = aligned if aligned is not None else rng.integers(40, 220, (112, 112, 3), dtype=np.uint8)

    def detect(self, image_bgr, min_score):
        return [f for f in self.faces if f.score >= min_score]

    def align(self, image_bgr, detected):
        return self.aligned

    def embed(self, aligned_face_bgr):
        return np.array([1, 0, 0, 0], dtype=np.float32)


class StubAccessories:
    def __init__(self, glasses=0.0, headwear=0.0, mask=0.0):
        self.scores = AccessoryScores(glasses=glasses, headwear=headwear, mask=mask)

    def score(self, image, detected):
        return self.scores


THRESHOLDS = QualityThresholds(
    min_detection_score=0.8,
    secondary_detection_score=0.6,
    min_face_size_px=80,
    min_sharpness=20,
    min_brightness=40,
    max_brightness=225,
    min_image_dimension=160,
    max_image_dimension=4096,
)


def pipeline(faces, aligned=None, accessories=None) -> FacePipeline:
    return FacePipeline(StubEngine(faces, aligned), THRESHOLDS, accessories)


def code_of(fn) -> str:
    with pytest.raises(FaceValidationError) as exc:
        fn()
    return exc.value.code


def test_pipeline_accepts_a_good_frontal_capture():
    result = pipeline([face()], accessories=StubAccessories()).analyze_frontal(image_bytes())
    assert result.detection_score == 0.95 and 0 < result.quality_score <= 1
    assert result.pose is not None and abs(result.pose.yaw_ratio) < 0.01
    assert pipeline([face()]).model_name == "stub"


@pytest.mark.parametrize(
    ("faces", "aligned", "expected"),
    [
        ([], None, "NO_FACE"),
        ([face(), face(x=10)], None, "MULTIPLE_FACES"),
        ([face(score=0.7)], None, "LOW_DETECTION_SCORE"),
        ([face(size=50)], None, "FACE_TOO_SMALL"),
        ([face(x=250)], None, "FACE_CUT_OFF"),
        ([face(lm=landmarks(yaw_shift=20))], None, "POSE_NOT_FRONTAL"),
        ([face(lm=landmarks(roll_dy=30))], None, "POSE_TILTED"),
        ([face(lm=landmarks(nose_y=145))], None, "POSE_PITCH"),
        ([face()], np.full((112, 112, 3), 10, np.uint8), "TOO_DARK"),
        ([face()], np.full((112, 112, 3), 250, np.uint8), "TOO_BRIGHT"),
        ([face()], np.full((112, 112, 3), 120, np.uint8), "TOO_BLURRY"),
    ],
)
def test_pipeline_rejects_bad_captures(faces, aligned, expected):
    assert code_of(lambda: pipeline(faces, aligned).analyze_frontal(image_bytes())) == expected


def test_occlusion_measures_visible_skin_against_forehead():
    from app.facial_recognition.occlusion import lower_face_skin_ratio

    skin_bgr = (110, 140, 200)  # tono de piel (BGR)
    image = np.full((320, 320, 3), skin_bgr, np.uint8)
    detected = face()
    assert lower_face_skin_ratio(image, detected) == 1.0  # rostro descubierto

    masked = image.copy()
    masked[160:215, 100:225] = (200, 160, 90)  # cubrebocas azul sobre nariz y mejillas
    assert lower_face_skin_ratio(masked, detected) < 0.2

    dim = (image * 0.55).astype(np.uint8)  # misma persona con poca luz: sigue siendo piel
    assert lower_face_skin_ratio(dim, detected) > 0.9

    covered_forehead = image.copy()
    covered_forehead[90:130, 120:200] = (30, 30, 30)  # cabello/gorra sobre la frente: sin referencia
    assert lower_face_skin_ratio(covered_forehead, detected) is None


def test_mask_requires_clip_and_physical_occlusion():
    p = pipeline([face()])
    assert p._mask_confirmed(0.95, skin_ratio=0.95) is False  # CLIP lo sospecha pero hay piel: NO bloquea
    assert p._mask_confirmed(0.40, skin_ratio=0.10) is True  # sospecha + nariz cubierta
    assert p._mask_confirmed(0.10, skin_ratio=0.00) is False  # CLIP no lo sospecha
    assert p._mask_confirmed(0.80, skin_ratio=None) is False  # sin referencia: exige casi certeza
    assert p._mask_confirmed(0.95, skin_ratio=None) is True


def test_accessory_consensus_requires_majority():
    from app.facial_recognition.pipeline import Accessory, accessory_consensus

    mask, glasses = Accessory.MASK, Accessory.GLASSES
    assert accessory_consensus([(mask,)]) == (mask,)
    assert accessory_consensus([(mask,), ()]) == ()
    assert accessory_consensus([(mask,), (), ()]) == ()
    assert accessory_consensus([(mask, glasses), (mask,), ()]) == (mask,)
    assert accessory_consensus([(glasses,), (glasses,), (glasses,), (), ()]) == (glasses,)


def test_pipeline_accessories_and_headwear_exemption():
    accessories = StubAccessories(glasses=0.9, headwear=0.9, mask=0.99)
    with pytest.raises(FaceValidationError) as exc:
        pipeline([face()], accessories=accessories).analyze_frontal(image_bytes(color=(40, 40, 40)))
    assert exc.value.details == {"accessories": ["GLASSES", "HEADWEAR", "MASK"]}
    assert "los lentes, la gorra o sombrero y el cubrebocas" in exc.value.message

    exempt = pipeline([face()], accessories=StubAccessories(headwear=0.9))
    no_headwear_rule = FacePolicy(block_headwear=False)
    assert exempt.analyze_frontal(image_bytes(), policy=no_headwear_rule).accessories is not None
    # Si la empresa permite todos los accesorios, CLIP ni siquiera se ejecuta.
    permissive = FacePolicy(block_glasses=False, block_headwear=False, block_mask=False)
    assert (
        pipeline([face()], accessories=accessories).analyze_frontal(image_bytes(), policy=permissive).accessories
        is None
    )
    # Sin aplicar (consenso): se reporta en vez de bloquear.
    reported = pipeline([face()], accessories=StubAccessories(glasses=0.9))
    assert reported.analyze_frontal(image_bytes(), enforce_accessories=False).accessories_found == ("GLASSES",)


def test_pipeline_liveness_turn():
    turned_left = face(score=0.7, lm=landmarks(yaw_shift=20))
    result = pipeline([turned_left]).analyze_turn(image_bytes(), TurnDirection.LEFT)
    assert result.pose is not None and result.pose.turned(TurnDirection.LEFT, 0.2)
    with pytest.raises(FaceValidationError) as exc:
        pipeline([turned_left]).analyze_turn(image_bytes(), TurnDirection.RIGHT)
    assert exc.value.code == "LIVENESS_TURN_NOT_DETECTED" and exc.value.details["expected"] == "TURN_RIGHT"


# ---------------------------------------------------------------- validadores


def test_password_and_name_validators():
    for bad in ["Corta1", "x" * 200 + "Aa1", "sinmayuscula1", "SINMINUSCULA1", "SinNumeros"]:
        with pytest.raises(ValueError):
            validators.validate_password_strength(bad)
    assert validators.validate_password_strength("Segura123") == "Segura123"
    assert validators.normalize_name("  ana   maría ") == "ana maría"
    for bad in ["", "x" * 101, "Ana123"]:
        with pytest.raises(ValueError):
            validators.normalize_name(bad)
    assert validators.normalize_employee_number(" emp-01 ") == "EMP-01"
    with pytest.raises(ValueError):
        validators.normalize_employee_number("con espacios")


def test_birth_date_uses_business_timezone():
    today = validators.business_today()
    adult = today.replace(year=today.year - 30)
    assert validators.validate_birth_date(adult) == adult
    for bad in [today, today + timedelta(days=1), today.replace(year=today.year - 10), date(1900, 1, 1)]:
        with pytest.raises(ValueError):
            validators.validate_birth_date(bad)


# ---------------------------------------------------------------- utilidades


def test_encryption_roundtrip_and_tamper_detection():
    token = encrypt_bytes(b"dato sensible")
    assert token != b"dato sensible" and decrypt_bytes(token) == b"dato sensible"
    with pytest.raises(ValueError):
        decrypt_bytes(token[:-2] + b"xx")


def test_system_resources():
    assert available_cpus() >= 1
    assert 1 <= default_api_workers() <= 4


def test_database_rate_limiter_fails_open(monkeypatch):
    from sqlalchemy.exc import OperationalError

    from app.middleware import rate_limit

    class BrokenEngine:
        dialect = rate_limit.engine.dialect

        def begin(self):
            raise OperationalError("SELECT", {}, Exception("bd caída"))

    monkeypatch.setattr(rate_limit, "engine", BrokenEngine())
    assert rate_limit.DatabaseRateLimiter().hit("k", 1, 60) is None


def test_project_env_file_is_clean():
    """`backend/.env`: solo variables con valor, que alguien lee y sin repetir.

    Las lee Settings o entrypoint.sh (al arrancar el contenedor). Sin comentarios al final de la
    línea ni comillas: `docker run --env-file`, Kubernetes y systemd los tomarían como parte del valor.
    """
    import re
    from pathlib import Path

    from app.core.config import ENV_FILE, Settings

    if not ENV_FILE.is_file():
        pytest.skip("Sin backend/.env en este entorno")
    lines = ENV_FILE.read_text(encoding="utf-8").splitlines()
    keys = [m.group(1) for line in lines if (m := re.match(r"^([A-Z][A-Z0-9_]*)=", line))]
    assert len(keys) == len(set(keys)), "Variables repetidas en .env"
    empty = [line.split("=")[0] for line in lines if re.match(r"^[A-Z][A-Z0-9_]*=[ \t]*$", line)]
    assert not empty, f"Variables vacías en .env: {empty}"
    entrypoint = (Path(__file__).resolve().parent.parent / "entrypoint.sh").read_text(encoding="utf-8")
    runtime = set(re.findall(r"\$\{?([A-Z][A-Z0-9_]*)", entrypoint))
    unused = [k for k in keys if k not in Settings.model_fields and k not in runtime]
    assert not unused, f"Variables que nadie lee: {unused}"
    inline = [line for line in lines if re.match(r"^[A-Z][A-Z0-9_]*=.*\s#", line)]
    quoted = [line for line in lines if re.match(r"^[A-Z][A-Z0-9_]*=[\"']", line)]
    assert not inline and not quoted, [line.split("=")[0] for line in inline + quoted]
