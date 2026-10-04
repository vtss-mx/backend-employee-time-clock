"""Doble modelo: con la fusión (SFace + FaceNet), cada modelo debe reconocer a la persona por su
cuenta, no solo su promedio. Una imagen fabricada para engañar a un solo modelo no basta.

Se usan vectores con la forma real de la fusión: [√0.5·SFace (128), √0.5·FaceNet (512)]."""

from dataclasses import replace
from datetime import UTC, datetime

import numpy as np
import pytest

from app.core.config import settings
from app.facial_recognition.calibration import model_floor_confidence, similarity_for_confidence
from app.facial_recognition.matcher import FUSION_DIMENSION, MatchRequirement, acceptance, normalize_rows
from app.services.face_gallery import Gallery, identify
from app.services.face_service import Reference
from app.services.identity_core import match_one, required_match
from app.services.policy_service import PolicySnapshot
from tests.conftest import _analysis

RNG = np.random.default_rng(7)


def _unit(size: int) -> np.ndarray:
    vector = RNG.standard_normal(size)
    return vector / np.linalg.norm(vector)


def _like(base: np.ndarray, similarity: float) -> np.ndarray:
    """Otro vector con similitud coseno EXACTA a `base`."""
    other = RNG.standard_normal(base.size)
    other -= base * float(other @ base)
    other /= np.linalg.norm(other)
    return similarity * base + np.sqrt(1 - similarity**2) * other


def _fused(sface: np.ndarray, facenet: np.ndarray) -> np.ndarray:
    return (np.concatenate([sface, facenet]) * np.sqrt(0.5)).astype(np.float32)


SFACE, FACENET = _unit(128), _unit(512)
PERSON = _fused(SFACE, FACENET)


def _probe(cos_sface: float, cos_facenet: float) -> np.ndarray:
    """Captura cuya fusión con PERSON vale 0.5·cos_sface + 0.5·cos_facenet."""
    return _fused(_like(SFACE, cos_sface), _like(FACENET, cos_facenet))


def test_each_model_must_reach_its_own_floor():
    policy = PolicySnapshot()  # 99.999 %: fusión ≥ 0.653; SFace ≥ 0.460 y FaceNet ≥ 0.579 por separado
    requirement = required_match(policy)
    assert requirement.floors == pytest.approx((0.460, 0.579), abs=0.002)
    # Fusión holgada (0.675) con SFace engañado a la baja: antes pasaba, ahora no.
    fooled = _probe(0.40, 0.95)
    fused, accepted = acceptance([fooled], [PERSON], requirement)
    assert float(fused[0, 0]) == pytest.approx(0.675, abs=1e-4) and fused[0, 0] >= requirement.fused
    assert not accepted[0, 0]
    # Lo mismo con FaceNet: la fusión no basta si un modelo no la reconoce.
    assert not acceptance([_probe(0.95, 0.45)], [PERSON], requirement)[1][0, 0]
    # La persona real: ambos modelos la reconocen.
    assert acceptance([_probe(0.75, 0.85)], [PERSON], requirement)[1][0, 0]


def test_one_to_one_requires_every_capture_with_both_models():
    reference = Reference(1, PERSON, False, datetime.now(UTC), None)
    genuine = [replace(_analysis("x"), embedding=_probe(0.75, 0.85)) for _ in range(2)]
    assert match_one(genuine, [reference], PolicySnapshot()).matched
    one_fooled = [genuine[0], replace(_analysis("y"), embedding=_probe(0.40, 0.97))]
    assert not match_one(one_fooled, [reference], PolicySnapshot()).matched


def test_identification_rejects_a_single_fooled_model():
    impostor = _fused(_unit(128), _unit(512))
    gallery = Gallery((1, 2, "t"), np.array([10, 20]), np.array([1, 2]), normalize_rows(np.stack([PERSON, impostor])))
    requirement = required_match(PolicySnapshot())
    assert identify(gallery, [_probe(0.75, 0.85)], required=requirement, margin=0.05).employee_id == 1
    fooled = identify(gallery, [_probe(0.40, 0.97)], required=requirement, margin=0.05)
    assert (fooled.employee_id, fooled.reason) == (None, "NO_MATCH")


def test_floors_follow_the_company_level_and_only_apply_to_the_fusion(monkeypatch):
    assert model_floor_confidence(0.8) == 0.40 and model_floor_confidence(0.999) == pytest.approx(0.40)
    assert model_floor_confidence(0.9999) == pytest.approx(0.90)
    assert model_floor_confidence(0.99999) == pytest.approx(0.99)
    lax = required_match(PolicySnapshot(min_confidence=0.9))
    assert lax.floors == (similarity_for_confidence(0.40, "sface"), similarity_for_confidence(0.40, "facenet"))
    # Vectores que no son de la fusión (otro modelo): solo cuenta la similitud.
    small = [_unit(128).astype(np.float32)]
    assert acceptance(small, small, MatchRequirement(0.5, (0.99, 0.99)))[1][0, 0]
    assert FUSION_DIMENSION == 640
    monkeypatch.setattr(settings, "FACE_RECOGNITION_MODEL", "sface")
    assert required_match(PolicySnapshot()).floors is None
