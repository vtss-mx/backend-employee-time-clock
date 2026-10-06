"""Registro facial con muchas fotos y consenso de la ráfaga (decisión del dueño del producto, 2026-10-06).

- La selección (`enrollment_selection`): las mejores referencias por calidad sin repetir lo que ya se tiene, en el
  orden de la toma; la foto del revisor es la mejor; repetidas, malas y la toma de dos personas; el motivo más
  frecuente cuando no alcanzan; un engaño nunca se descarta en silencio; la suplantación sobre todas las útiles; CLIP
  solo en las referencias; la toma única recuerda TODAS las útiles.
- El reparto entre el worker de la petición y los de repuesto (`map_on_spares`): repuestos que ayudan, que fallan o
  que no terminan, y sin repuestos.
- Los límites de la subida (36 sí, 37 no, una foto enorme, un cuerpo enorme).
- El consenso de identidad de la ráfaga (solo endurece) en la verificación 1:1, en el 1:N y con QR + rostro.
"""

import hashlib
import logging
import threading
import time
from dataclasses import replace

import cv2
import numpy as np
import pytest
from sqlalchemy import func, select

import app.facial_recognition as face_module
from app.core.config import settings
from app.core.database import SessionLocal
from app.core.exceptions import ServiceUnavailableError, UnprocessableError
from app.facial_recognition import map_on_spares
from app.facial_recognition.matcher import MatchRequirement
from app.facial_recognition.pipeline import DEFAULT_POLICY, BurstAnalysis, FacePolicy
from app.facial_recognition.worker_pool import WorkerPool
from app.models import FaceAttemptMetric, FaceEmbedding, FaceEnrollment
from app.services import face_signals, image_storage
from app.services.catalog_service import get_catalogs
from app.services.enrollment_selection import select_references
from app.services.face_service import SuspiciousCapture
from app.services.face_signals import BurstOutcome
from app.services.identity_core import burst_rejects, reason_message
from app.services.image_storage import FACE_ENROLLMENT_PHOTOS
from tests.conftest import (
    FakePipeline,
    burst_files,
    create_employee,
    face_vector,
    login,
    qr_content,
    turn_files,
)
from tests.test_burst_signals import TileEngine, jpeg, layout, sheet, tile
from tests.test_capture_protocol import metric, verify_with
from tests.test_capture_security import employee_id, history
from tests.test_units import THRESHOLDS, StubAccessories, face, image_bytes, pipeline
from tests.test_validators import approved, validator_headers

ENROLL = "/api/enrollment/face"


def photo(index: int, quality: float = 0.9, *, name: str | None = None, kind: str = "face") -> bytes:
    """Una foto de la toma: cada una se parece 0.9 a juan y ≈ 0.81 a las demás (no se repiten); `#` fija su huella."""
    return f"{kind}:{name or f'juan~0.9~v{index}'}#{index} q={quality}".encode()


def take(count: int = 36) -> list[bytes]:
    return [photo(i) for i in range(count)]


def digest(image: bytes) -> str:
    return hashlib.sha256(image).hexdigest()


def chosen(selection) -> list[str]:
    return [reference.capture_digest for reference in selection.references]


class Graded(FakePipeline):
    """La calidad de cada foto sale de su nombre (" q=0.83"); "broken:" no se puede leer (OpenCV) y "garbage:" no es
    una imagen (ValueError)."""

    def analyze_frontal(self, image_bytes, *, policy=DEFAULT_POLICY, enforce_accessories=True):
        if image_bytes.startswith(b"broken:"):
            raise cv2.error("captura ilegible")
        if image_bytes.startswith(b"garbage:"):
            raise ValueError("no es una imagen")
        analysis = super().analyze_frontal(image_bytes, policy=policy, enforce_accessories=enforce_accessories)
        _, _, grade = image_bytes.decode().partition(" q=")
        return replace(analysis, quality_score=float(grade)) if grade else analysis


class Busy(Graded):
    """El worker de la petición más lento que los repuestos: así los repuestos de verdad toman fotos."""

    def analyze_frontal(self, image_bytes, **kwargs):
        time.sleep(0.004)
        return super().analyze_frontal(image_bytes, **kwargs)


# ---------------------------------------------------------------- la selección


def test_the_best_photos_are_chosen_without_repeats_in_the_order_of_the_take():
    images = [photo(i, 0.50 + i / 100) for i in range(36)]  # la mejor es la última
    images[34] = photo(34, 0.84, name="juan~0.9~v35~0.97~dup")  # casi idéntica a la mejor: no aporta nada
    selection = select_references(Graded(), images, DEFAULT_POLICY)
    assert chosen(selection) == [digest(images[k]) for k in (30, 31, 32, 33, 35)]  # entra la 30 en lugar de la 34
    assert selection.photo == 35 and selection.received == 36 and len(selection.usable) == 36
    assert (selection.discarded, selection.duplicates, selection.flags) == ({}, 0, ())


def test_with_too_many_look_alikes_the_template_is_filled_with_the_best_ones():
    qualities = [0.6, 0.9, 0.7, 0.8, 0.5, 0.95]
    images = [f"face:juan#{i} q={q}".encode() for i, q in enumerate(qualities)]  # el mismo rostro en todas
    selection = select_references(Graded(), images, DEFAULT_POLICY)
    assert chosen(selection) == [digest(images[k]) for k in (0, 1, 2, 3, 5)] and selection.photo == 5


def test_bad_and_repeated_photos_are_discarded_and_counted(caplog):
    images = [*take(30), b"noface", b"multi", b"noface", b"dim:juan#d", b"broken:juan#b", b"garbage:juan#g"]
    images.append(images[0])  # la cámara entregó dos veces el mismo cuadro
    with caplog.at_level(logging.INFO, logger="app.services.enrollment_selection"):
        selection = select_references(Graded(), images, FacePolicy(min_quality=0.5))
    assert selection.discarded == {"NO_FACE": 2, "MULTIPLE_FACES": 1, "LOW_QUALITY": 1, "INVALID_IMAGE": 2}
    assert (selection.duplicates, len(selection.usable), selection.received) == (1, 30, 37)
    assert "37 fotos, 30 útiles, 1 repetidas" in caplog.text and "juan" not in caplog.text  # solo cuentas


@pytest.mark.parametrize(
    ("images", "code", "details"),
    [
        ([photo(0), photo(1), b"noface", b"noface", b"multi"], "NO_FACE", None),  # el más frecuente
        ([photo(0), photo(1), b"multi", b"noface"], "MULTIPLE_FACES", None),  # empate: el primero que apareció
        ([photo(0), b"dim:juan#1", b"dim:juan#2"], "LOW_QUALITY", {"quality": 0.45, "required": 0.5}),
    ],
)
def test_too_few_usable_photos_answer_with_the_most_frequent_reason(images, code, details):
    with pytest.raises(UnprocessableError) as error:
        select_references(Graded(), images, FacePolicy(min_quality=0.5))
    assert error.value.code == code and error.value.details == details
    assert error.value.message == get_catalogs().face_error_message(code, details)


def test_too_few_photos_without_a_bad_one_say_how_many_to_send():
    with pytest.raises(UnprocessableError) as error:
        select_references(Graded(), [photo(0), photo(1), photo(0)], DEFAULT_POLICY)
    assert error.value.code == "INVALID_FRAME_COUNT"
    assert error.value.message == f"Envía entre 3 y {settings.FACE_ENROLL_MAX_PHOTOS} capturas frontales"


def test_one_photo_suffices_when_the_platform_asks_for_one(monkeypatch):
    monkeypatch.setattr(settings, "FACE_ENROLL_MIN_USABLE", 1)
    selection = select_references(Graded(), [photo(0)], DEFAULT_POLICY)
    assert chosen(selection) == [digest(photo(0))] and selection.photo == 0


def test_a_security_reason_in_one_photo_rejects_the_whole_take():
    with pytest.raises(SuspiciousCapture) as error:
        select_references(Graded(), [*take(10), b"exif:juan#x"], DEFAULT_POLICY)
    assert error.value.code == "IMAGE_NOT_FROM_CAMERA"


def test_a_take_of_two_people_is_rejected_even_if_one_is_the_majority():
    pedro = [photo(i, name=f"pedro~0.9~p{i}") for i in range(30, 36)]
    for images in ([*take(30), *pedro], [*pedro, *take(30)]):
        with pytest.raises(UnprocessableError) as error:
            select_references(Graded(), images, DEFAULT_POLICY)
        assert error.value.code == "ENROLL_INCONSISTENT"


def test_the_chosen_references_must_also_look_alike_between_them():
    """Cada foto se parece lo suficiente a la del centro (el medoide), pero dos de las elegidas no entre sí."""
    vectors = {"m": [1.0, 0.0, 0.0], "a": [0.74, 0.6726, 0.0], "b": [0.74, -0.6726, 0.0]}

    class Shaped(FakePipeline):
        def analyze_frontal(self, image_bytes, **kwargs):
            analysis = super().analyze_frontal(image_bytes, **kwargs)
            name = image_bytes.decode().split(":")[1].split("#")[0]
            return replace(analysis, embedding=np.array(vectors[name], dtype=np.float32))

    with pytest.raises(UnprocessableError) as error:
        select_references(Shaped(), [b"face:m#1", b"face:a#2", b"face:b#3"], DEFAULT_POLICY)
    assert error.value.code == "ENROLL_INCONSISTENT"


def test_spoofing_is_decided_over_every_usable_photo():
    spoofs = [photo(i, kind="spoof") for i in range(20)]
    flagged = select_references(Graded(), [*spoofs, *(photo(i) for i in range(20, 36))], DEFAULT_POLICY)
    assert flagged.flags == ("SPOOF",)
    # Una minoría sospechosa que no es lo que se guarda (las referencias son las mejores, reales) no marca.
    few = select_references(Graded(), [*spoofs[:10], *(photo(i, 0.95) for i in range(10, 36))], DEFAULT_POLICY)
    assert few.flags == ()
    # La misma minoría como las mejores fotos (la mayoría de las referencias): la regla de siempre sobre lo guardado.
    best_spoofs = [photo(i, 0.95, kind="spoof") for i in range(10)]
    stored = select_references(Graded(), [*best_spoofs, *take(36)[10:]], DEFAULT_POLICY)
    assert stored.flags == ("SPOOF",)


def test_at_the_maximum_level_one_suspicious_photo_counts_only_among_the_references():
    strict = replace(DEFAULT_POLICY, spoof_any_frame=True)
    # Una foto sospechosa de baja calidad entre 36: no es referencia y la mayoría de la toma es real → sin marca (sobre
    # 36 fotos, «una sola basta» marcaría a casi la mitad de las personas reales).
    stray = [photo(0, quality=0.2, kind="spoof"), *(photo(i) for i in range(1, 36))]
    assert select_references(Graded(), stray, strict).flags == ()
    # La misma foto como la mejor de la toma: es referencia y, en el nivel Máximo, basta ella.
    best = [photo(0, quality=0.99, kind="spoof"), *(photo(i) for i in range(1, 36))]
    assert select_references(Graded(), best, strict).flags == ("SPOOF",)
    assert select_references(Graded(), best, DEFAULT_POLICY).flags == ()  # en los demás niveles decide la mayoría


class Counting(Graded):
    """Cuenta en qué fotos corrió CLIP."""

    def __init__(self) -> None:
        self.clip: list[bytes] = []

    def accessories_of(self, image_bytes, *, policy=DEFAULT_POLICY):
        self.clip.append(image_bytes)
        return super().accessories_of(image_bytes, policy=policy)


def test_clip_runs_only_on_the_references_and_decides_by_their_majority():
    glasses = [photo(i, 0.99, kind="glasses") for i in range(3)]
    images = [*glasses, photo(3, 0.98), photo(4, 0.98), *(photo(i, 0.5) for i in range(5, 36))]
    engine = Counting()
    selection = select_references(engine, images, DEFAULT_POLICY)
    assert sorted(engine.clip) == sorted(images[:5]) and selection.flags == ("GLASSES",)
    assert [r.accessories_found for r in selection.references][:3] == [("GLASSES",)] * 3
    # Lentes solo en fotos que no se eligieron: CLIP nunca las ve.
    unseen = [*(photo(i, 0.99) for i in range(6)), *(photo(i, 0.5, kind="glasses") for i in range(6, 36))]
    assert select_references(Counting(), unseen, DEFAULT_POLICY).flags == ()
    # Si la empresa no bloquea ningún accesorio, CLIP no corre.
    allowed = Counting()
    select_references(allowed, images, FacePolicy(block_glasses=False, block_headwear=False, block_mask=False))
    assert allowed.clip == []


def test_an_engine_failure_while_choosing_is_a_503():
    class Crashing(Graded):
        def analyze_frontal(self, image_bytes, **kwargs):
            if image_bytes.startswith(b"crash:"):
                raise RuntimeError("el motor se cayó")
            return super().analyze_frontal(image_bytes, **kwargs)

    class BlindClip(Graded):
        def accessories_of(self, image_bytes, *, policy=DEFAULT_POLICY):
            raise RuntimeError("CLIP se cayó")

    for engine, images in ((Crashing(), [b"crash:juan#1", *take(5)]), (BlindClip(), take(5))):
        with pytest.raises(ServiceUnavailableError) as error:
            select_references(engine, images, DEFAULT_POLICY)
        assert error.value.code == "FACE_PROCESSING_ERROR"


# ---------------------------------------------------------------- en el pipeline


def test_the_pipeline_checks_only_the_accessories_of_a_reference():
    blocked = pipeline([face()], accessories=StubAccessories(glasses=0.9))
    scores, _, found = blocked.accessories_of(image_bytes())
    assert scores is not None and scores.glasses == 0.9 and [a.value for a in found] == ["GLASSES"]
    allowed = FacePolicy(block_glasses=False, block_headwear=False, block_mask=False)
    assert blocked.accessories_of(b"no se decodifica", policy=allowed) == (None, None, ())
    assert pipeline([face()]).accessories_of(b"no se decodifica") == (None, None, ())  # sin CLIP cargado


def test_the_burst_embeds_its_best_still_crops_reusing_the_anchor():
    class Counted(TileEngine):
        embedded = 0

        def embed(self, aligned_face_bgr):
            Counted.embedded += 1
            return super().embed(aligned_face_bgr)

    from app.facial_recognition.pipeline import FacePipeline
    from app.services.capture_protocol import burst_rules

    engine = FacePipeline(Counted(), THRESHOLDS)
    tiles = [tile() for _ in range(30)] + [tile(level=230), tile(level=5)]
    data, shape = jpeg(sheet(tiles, cols=8)), layout(32, cols=8, holds=30)
    analysis = engine.analyze_burst(data, shape, replace(burst_rules(), identity_frames=7))
    assert len(analysis.identity) == 7 and len(analysis.anchors) == 2
    assert Counted.embedded == 8  # 2 anclas + 7 recortes, el primero es ancla: uno se reutiliza
    assert engine.analyze_burst(data, shape, replace(burst_rules(), identity_frames=0)).identity == ()


# ---------------------------------------------------------------- en paralelo


@pytest.fixture
def spares(monkeypatch):
    """Un proceso con tres workers de repuesto libres (como uno de cuatro núcleos sin carga)."""
    holder = face_module._PipelineHolder()
    holder._pool = WorkerPool(lambda _: Graded(), size=3, max_waiting=3, wait_timeout=1)
    monkeypatch.setattr(face_module, "_holder", holder)
    yield holder._pool
    holder.spare_threads(3).shutdown(wait=True)


def idle(pool: WorkerPool) -> None:
    deadline = time.monotonic() + 5
    while pool.stats().busy:
        assert time.monotonic() < deadline, "un worker de repuesto no regresó"
        time.sleep(0.005)


def test_spare_workers_analyse_part_of_the_photos_with_the_same_result(spares):
    images = [photo(i, 0.5 + i / 100) for i in range(36)]
    alone = select_references(Graded(), images, DEFAULT_POLICY)
    helped: list[bytes] = []

    class Helper(Graded):
        def analyze_frontal(self, image_bytes, **kwargs):
            helped.append(image_bytes)
            return super().analyze_frontal(image_bytes, **kwargs)

    spares._idle = [Helper(), Helper(), Helper()]
    together = select_references(Busy(), images, DEFAULT_POLICY)
    assert (chosen(together), together.photo, together.flags) == (chosen(alone), alone.photo, alone.flags)
    assert helped and len(helped) < 36  # los dos trabajaron
    idle(spares)


def test_a_spare_that_fails_leaves_its_photo_to_the_request_worker(spares, caplog):
    class Broken(Graded):
        def analyze_frontal(self, image_bytes, **kwargs):
            raise RuntimeError("worker dañado")

    spares._idle = [Broken(), Broken(), Broken()]
    with caplog.at_level(logging.ERROR):
        selection = select_references(Busy(), take(), DEFAULT_POLICY)
    assert len(selection.usable) == 36 and "Un worker de repuesto no terminó" in caplog.text
    idle(spares)


def test_a_spare_that_does_not_finish_in_time_is_not_waited_for(spares, monkeypatch, caplog):
    release = threading.Event()

    class Stuck(Graded):
        def analyze_frontal(self, image_bytes, **kwargs):
            release.wait(5)
            return super().analyze_frontal(image_bytes, **kwargs)

    spares._idle = [Stuck(), Stuck(), Stuck()]
    monkeypatch.setattr(settings, "FACE_ENROLL_SPARE_WAIT_SECONDS", 0.05)
    with caplog.at_level(logging.ERROR):
        selection = select_references(Busy(), take(), DEFAULT_POLICY)
    assert len(selection.usable) == 36 and "no terminó" in caplog.text
    release.set()
    idle(spares)


def test_without_a_free_spare_everything_is_sequential(spares):
    taken = [spares.try_acquire() for _ in range(3)]
    assert len(select_references(Graded(), take(), DEFAULT_POLICY).usable) == 36
    assert spares.stats().processed == 0
    for worker in taken:
        spares.release(worker)


def test_only_the_spares_that_are_needed_are_taken(spares):
    assert map_on_spares(Graded(), 2, lambda _, index: index * 10, wait=1) == [0, 10]
    idle(spares)
    assert spares.stats().processed == 1  # dos tareas: la petición y UN repuesto


def test_a_failure_of_the_request_worker_stops_the_spares(spares):
    mine = Graded()
    done: list[int] = []

    def task(worker, index: int) -> int:
        if worker is mine:
            raise RuntimeError("el motor de la petición se cayó")
        time.sleep(0.01)
        done.append(index)
        return index

    with pytest.raises(RuntimeError):
        map_on_spares(mine, 40, task, wait=1)
    idle(spares)
    assert len(done) < 10  # la fila se vació: los repuestos no siguieron con las demás


# ---------------------------------------------------------------- por la API


def enroll(client, headers, images, url=ENROLL, person="juan"):
    challenge = client.post("/api/face/challenge", headers=headers).json()["data"]
    files = [("images", (f"f{i}.jpg", image, "image/jpeg")) for i, image in enumerate(images)]
    files += turn_files(challenge, person)
    return client.post(url, data={"challenge_id": challenge["challenge_id"]}, files=files, headers=headers)


def _employee(client, company_headers) -> tuple[dict, dict[str, str]]:
    created = create_employee(client, company_headers).json()["data"]
    return created, login(client, "juan@empresa.com", "Empleado123")


def test_a_36_photo_enrollment_keeps_five_references_and_one_photo(client, company_headers, bucket):
    _, headers = _employee(client, company_headers)
    images = [*(photo(i, 0.45, kind="dim") for i in range(10)), *(photo(i) for i in range(10, 36))]
    response = enroll(client, headers, images)
    assert response.status_code == 201, response.text
    enrollment_id = response.json()["data"]["enrollment_id"]
    detail = client.get(f"/api/enrollments/{enrollment_id}", headers=company_headers).json()["data"]
    assert detail["samples"] == settings.FACE_MAX_SAMPLES_PER_EMPLOYEE and detail["quality_score"] == 0.9
    with SessionLocal() as db:
        assert db.scalar(select(func.count()).select_from(FaceEmbedding)) == settings.FACE_MAX_SAMPLES_PER_EMPLOYEE
        enrollment = db.get(FaceEnrollment, enrollment_id)
        # Una sola foto en el bucket (cifrada): la de mejor calidad (la primera de las buenas); nunca las 36.
        assert len(bucket.objects) == 1 and image_storage.read(FACE_ENROLLMENT_PHOTOS, enrollment) == images[10]


def test_a_36_photo_enrollment_in_person_is_approved_at_once(client, company_headers):
    created, _ = _employee(client, company_headers)
    response = enroll(client, company_headers, take(), url=f"/api/employees/{created['id']}/face/enroll")
    assert response.status_code == 201 and response.json()["data"]["face_status"] == "APPROVED"


def test_a_take_with_a_photo_from_a_file_is_rejected_and_logged(client, company_headers):
    created, headers = _employee(client, company_headers)
    rejected = enroll(client, headers, [*take(35), b"exif:juan#x"])
    assert rejected.status_code == 422 and rejected.json()["code"] == "IMAGE_NOT_FROM_CAMERA"
    assert history(client, company_headers, created["id"]) == [(False, "IMAGE_NOT_FROM_CAMERA")]


def test_spoofing_over_the_whole_take_flags_the_review_or_blocks_in_person(client, company_headers):
    created, headers = _employee(client, company_headers)
    images = [*(photo(i, kind="spoof") for i in range(20)), *(photo(i) for i in range(20, 36))]
    submitted = enroll(client, headers, images)
    assert submitted.status_code == 201
    detail = client.get(f"/api/enrollments/{submitted.json()['data']['enrollment_id']}", headers=company_headers)
    assert detail.json()["data"]["flagged_accessories"] == ["SPOOF"]
    in_person = enroll(client, company_headers, images, url=f"/api/employees/{created['id']}/face/enroll")
    assert in_person.status_code == 422 and in_person.json()["code"] == "SPOOF_DETECTED"


def test_a_photo_that_was_not_chosen_cannot_be_sent_again(client, company_headers):
    """La toma única recuerda las 36 útiles: una de las 31 no elegidas no sirve en otro registro."""
    created, headers = _employee(client, company_headers)
    first = take()
    enrollment = enroll(client, headers, first).json()["data"]["enrollment_id"]
    reason = {"reason": "La foto está borrosa"}
    assert client.post(f"/api/enrollments/{enrollment}/reject", json=reason, headers=company_headers).status_code == 200
    fresh = [photo(i, name=f"juan~0.9~w{i}") for i in range(100, 105)]
    replayed = enroll(client, headers, [*fresh, first[20]])
    assert replayed.status_code == 422 and replayed.json()["code"] == "REPLAY_DETECTED"
    assert history(client, company_headers, created["id"])[0] == (False, "REPLAY_DETECTED")


def test_the_upload_limits_of_an_enrollment(client, company_headers, monkeypatch):
    _, headers = _employee(client, company_headers)
    too_many = enroll(client, headers, take(settings.FACE_ENROLL_MAX_PHOTOS + 1))
    assert too_many.status_code == 422 and too_many.json()["code"] == "TOO_MANY_IMAGES"
    huge = client.post(ENROLL, headers={**headers, "Content-Length": str(10**9)})
    body = huge.json()
    assert huge.status_code == 413 and body["code"] == "PAYLOAD_TOO_LARGE" and body["success"] is False
    assert body["message"] == "La solicitud excede el tamaño máximo (25.25 MB)" and body["traceId"]
    monkeypatch.setattr(settings, "MAX_IMAGE_SIZE_MB", 0.0001)  # ≈ 100 bytes
    big = enroll(client, headers, [photo(0) + b" " * 200, *take(3)])
    assert big.status_code == 413 and big.json()["code"] == "PAYLOAD_TOO_LARGE"  # el mensaje de una imagen
    assert big.json()["message"] == "La imagen excede el tamaño máximo de < 0.01 MB"


# ---------------------------------------------------------------- consenso de la ráfaga


def judge(frames: list[np.ndarray], references: list[np.ndarray], fused: float = 0.6) -> tuple[bool, float | None]:
    """El veredicto del consenso con una ráfaga cuyos mejores recortes quietos son `frames`."""
    signals = face_signals.begin()
    try:
        analysis = BurstAnalysis(
            frames=36, faceless=0, jumps=0, motion=1.0, repeats=0, pulse=None, identity=tuple(frames)
        )
        signals.burst = BurstOutcome(analysis=analysis)
        return burst_rejects(references, MatchRequirement(fused)), signals.consensus
    finally:
        face_signals.finish()


def test_the_consensus_is_the_median_with_a_margin_and_needs_enough_frames():
    juan, pedro = face_vector("juan"), face_vector("pedro")
    assert judge([juan] * 3 + [pedro] * 2, [juan]) == (False, 1.0)  # dos recortes malos no mueven la mediana
    rejected, value = judge([juan] * 2 + [pedro] * 3, [juan])
    assert rejected and value is not None and value < 0.5
    near = face_vector("juan~0.55~tile")  # 0.05 bajo lo exigido: dentro del margen de 0.10
    assert judge([near] * 5, [juan]) == (False, 0.55)
    assert judge([face_vector("juan~0.45~tile")] * 5, [juan])[0] is True  # fuera del margen
    assert judge([pedro] * (settings.FACE_CONSENSUS_MIN_FRAMES - 1), [juan]) == (False, None)  # muy pocos
    signals = face_signals.begin()  # sin ráfaga: no se juzga
    assert burst_rejects([juan], MatchRequirement(0.6)) is False and signals.consensus is None
    signals.burst = BurstOutcome(malformed=True)
    assert burst_rejects([juan], MatchRequirement(0.6)) is False
    face_signals.finish()


def test_a_verification_passes_only_if_the_live_video_is_the_person(client, company_headers, caplog):
    from tests.conftest import approved_employee

    headers = approved_employee(client, company_headers)
    passed, _ = verify_with(client, headers)
    assert passed.json()["data"]["verified"] is True and metric().burst_consensus == 1.0
    with caplog.at_level(logging.INFO, logger="app.services.identity_core"):
        swapped, _ = verify_with(client, headers, burst=burst_files("pedro"))  # las frontales sí son de juan
    assert swapped.status_code == 200 and swapped.json()["data"]["verified"] is False
    assert swapped.json()["message"] == reason_message("NO_MATCH") and "Consenso de la ráfaga" in caplog.text
    row = metric()
    assert row.reason == "NO_MATCH" and row.burst_consensus is not None and row.burst_consensus < 0.5
    assert history(client, company_headers, employee_id(client, headers))[0] == (False, "NO_MATCH")
    # Sin ráfaga, o con una sin rostros medibles, la decisión de siempre (nunca relaja ni bloquea de más).
    for burst in ([], burst_files("pedro", kind="burst-faceless")):
        unmeasured, _ = verify_with(client, headers, burst=burst)
        assert unmeasured.json()["data"]["verified"] is True and metric().burst_consensus is None


def test_the_consensus_can_be_turned_off(client, company_headers, monkeypatch):
    from tests.conftest import approved_employee

    headers = approved_employee(client, company_headers)
    monkeypatch.setattr(settings, "FACE_CONSENSUS_FRAMES", 0)
    answer, _ = verify_with(client, headers, burst=burst_files("pedro"))
    assert answer.json()["data"]["verified"] is True and metric().burst_consensus is None


def checkpoint(client, headers, person: str, burst_person: str, qr: str | None = None):
    challenge = client.post("/api/face/challenge", headers=headers).json()["data"]
    files = [("images", (f"c{i}.jpg", f"face:{person}".encode(), "image/jpeg")) for i in range(3)]
    files += turn_files(challenge, person) + burst_files(burst_person)
    data = {"challenge_id": challenge["challenge_id"], **({"qr_content": qr} if qr else {})}
    return client.post("/api/checkpoint/identify/face", data=data, files=files, headers=headers)


def test_the_validator_identifies_only_if_the_live_video_agrees(client, company_headers):
    approved(client, company_headers, "juan", number="EMP-001")
    headers = validator_headers(client, company_headers, mode="FACE")
    found = checkpoint(client, headers, "juan", "juan").json()["data"]
    assert found["verified"] is True and found["name"] == "Juan Pérez"
    missed = checkpoint(client, headers, "juan", "pedro")
    assert missed.json()["data"]["verified"] is False and missed.json()["message"] == reason_message("NO_MATCH")
    assert metric().reason == "NO_MATCH" and metric().burst_consensus is not None


def test_qr_and_face_also_needs_the_live_video_of_the_qr_holder(client, company_headers):
    juan = approved(client, company_headers, "juan", number="EMP-001")
    headers = validator_headers(client, company_headers, mode="QR_AND_FACE")
    qr = qr_content(juan["id"])
    assert client.post("/api/checkpoint/qr/inspect", json={"qr_content": qr}, headers=headers).status_code == 200
    mismatch = checkpoint(client, headers, "juan", "pedro", qr=qr)
    assert mismatch.json()["data"]["verified"] is False
    assert mismatch.json()["message"] == "El rostro no corresponde al dueño del código QR"
    with SessionLocal() as db:
        latest = db.scalars(select(FaceAttemptMetric).order_by(FaceAttemptMetric.id.desc())).first()
        assert latest is not None and latest.reason == "NO_MATCH"
