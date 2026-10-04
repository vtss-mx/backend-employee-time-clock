"""Pipeline de análisis facial.

analyze_frontal (registro y verificación):
    imagen → detección → una sola persona → tamaño/encuadre → pose frontal
    → iluminación/nitidez → accesorios (lentes, gorra, cubrebocas) → embedding

analyze_turn (prueba de vida):
    imagen → detección → una sola persona → giro en la dirección del reto → embedding
"""

import hashlib
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum

import cv2
import numpy as np

from app.facial_recognition.accessories import AccessoryScores, ClipAccessoryDetector
from app.facial_recognition.antispoof import MiniFasAntiSpoof
from app.facial_recognition.engine import DetectedFace, FaceEngine
from app.facial_recognition.errors import FaceValidationError
from app.facial_recognition.image_utils import decode_image
from app.facial_recognition.occlusion import lower_face_skin_ratio
from app.facial_recognition.pose import HeadPose, TurnDirection, estimate_pose


class Accessory(StrEnum):
    GLASSES = "GLASSES"
    HEADWEAR = "HEADWEAR"
    MASK = "MASK"


logger = logging.getLogger(__name__)


def accessories_error(found: Sequence[Accessory]) -> FaceValidationError:
    """Accesorios detectados (solo códigos): la API arma el mensaje con el catálogo de accesorios."""
    return FaceValidationError("ACCESSORIES_DETECTED", {"accessories": [a.value for a in found]})


def accessory_consensus(per_frame: Sequence[Sequence[Accessory]]) -> tuple[Accessory, ...]:
    """Accesorios presentes en la MAYORÍA estricta de las capturas.

    Una persona no se pone y quita lentes o cubrebocas entre capturas tomadas con
    milisegundos de diferencia: si solo una de varias lo marca, es ruido del detector.
    Con una sola captura, esa captura decide.
    """
    total = len(per_frame)
    return tuple(a for a in Accessory if sum(a in found for found in per_frame) * 2 > total)


@dataclass(frozen=True)
class FacePolicy:
    """Qué exige la empresa en cada captura (configurable por COMPANY desde el frontend)."""

    block_glasses: bool = True
    block_headwear: bool = True
    block_mask: bool = True
    anti_spoofing: bool = True
    #: Rechazar imágenes con metadatos de cámara o de edición (no son capturas en vivo).
    reject_foreign_images: bool = True
    #: Anti-spoofing: probabilidad de rostro real por debajo de la cual una captura es sospechosa, y
    #: si basta una sola captura sospechosa (si no, decide la mayoría).
    spoof_threshold: float = 0.05
    spoof_any_frame: bool = False

    def blocks(self, accessory: Accessory) -> bool:
        return {
            Accessory.GLASSES: self.block_glasses,
            Accessory.HEADWEAR: self.block_headwear,
            Accessory.MASK: self.block_mask,
        }[accessory]

    @property
    def any_accessory(self) -> bool:
        return self.block_glasses or self.block_headwear or self.block_mask


DEFAULT_POLICY = FacePolicy()


@dataclass(frozen=True)
class QualityThresholds:
    min_detection_score: float
    secondary_detection_score: float
    min_face_size_px: int
    min_sharpness: float
    min_brightness: float
    max_brightness: float
    min_image_dimension: int
    max_image_dimension: int
    max_yaw_ratio: float = 0.15
    max_roll_degrees: float = 15.0
    min_pitch_ratio: float = 0.20
    max_pitch_ratio: float = 0.85
    glasses_threshold: float = 0.55
    headwear_threshold: float = 0.60
    mask_threshold: float = 0.30
    # Cubrebocas solo si además la nariz/mejillas NO muestran piel (verificación física).
    mask_max_skin_ratio: float = 0.55
    # Si la piel no se puede medir (frente cubierta), CLIP debe estar casi seguro.
    mask_strict_threshold: float = 0.90
    liveness_min_yaw_ratio: float = 0.18


@dataclass(frozen=True)
class FaceAnalysis:
    embedding: np.ndarray
    detection_score: float
    quality_score: float
    sharpness: float
    brightness: float
    face_box: tuple[int, int, int, int]
    pose: HeadPose | None = None
    accessories: AccessoryScores | None = None
    #: Fracción de piel visible en nariz/mejillas (None si no se pudo medir).
    lower_face_skin: float | None = None
    #: Accesorios detectados en ESTA captura (la decisión final se toma por consenso).
    accessories_found: tuple[Accessory, ...] = ()
    #: Probabilidad de rostro real (anti-spoofing). None si está desactivado.
    real_probability: float | None = None
    #: Huella de la captura (SHA-256 de sus píxeles): detecta capturas repetidas o reenviadas.
    capture_digest: str | None = None
    #: Rostro alineado en miniatura (32x32, gris): detecta fotogramas idénticos de una foto fija.
    face_thumb: np.ndarray | None = None
    #: Ancho y alto de la imagen recibida (todas las capturas de una toma salen de la misma cámara).
    image_size: tuple[int, int] | None = None


#: Lado de la miniatura del rostro para comparar fotogramas.
THUMB_SIDE = 32


@dataclass(frozen=True)
class CaptureTraits:
    digest: str
    thumb: np.ndarray
    size: tuple[int, int]


def capture_traits(image: np.ndarray, aligned: np.ndarray) -> CaptureTraits:
    """Huella, miniatura y tamaño de una captura (para las comprobaciones contra engaños)."""
    gray = cv2.cvtColor(aligned, cv2.COLOR_BGR2GRAY)
    return CaptureTraits(
        digest=hashlib.sha256(image.tobytes()).hexdigest(),
        thumb=cv2.resize(gray, (THUMB_SIDE, THUMB_SIDE), interpolation=cv2.INTER_AREA).astype(np.float32),
        size=(int(image.shape[1]), int(image.shape[0])),
    )


class FacePipeline:
    def __init__(
        self,
        engine: FaceEngine,
        thresholds: QualityThresholds,
        accessory_detector: ClipAccessoryDetector | None = None,
        antispoof: MiniFasAntiSpoof | None = None,
    ) -> None:
        self.engine = engine
        self.t = thresholds
        self.accessories = accessory_detector
        self.antispoof = antispoof

    @property
    def model_name(self) -> str:
        return self.engine.model_name

    # ------------------------------------------------------------------ frontal

    def analyze_frontal(
        self, image_bytes: bytes, *, policy: FacePolicy = DEFAULT_POLICY, enforce_accessories: bool = True
    ) -> FaceAnalysis:
        """Valida una captura frontal.

        enforce_accessories=False no lanza error por accesorios: los reporta en
        `accessories_found` para decidir por consenso entre varias capturas.
        """

        image = self._decode(image_bytes, policy)
        face = self._single_face(image, min_score=self.t.min_detection_score)
        self._check_framing(image, face)

        pose = estimate_pose(face.landmarks)
        if abs(pose.yaw_ratio) > self.t.max_yaw_ratio:
            raise FaceValidationError("POSE_NOT_FRONTAL")
        if abs(pose.roll_degrees) > self.t.max_roll_degrees:
            raise FaceValidationError("POSE_TILTED")
        if not self.t.min_pitch_ratio <= pose.pitch_ratio <= self.t.max_pitch_ratio:
            raise FaceValidationError("POSE_PITCH")

        aligned = self.engine.align(image, face)
        gray = cv2.cvtColor(aligned, cv2.COLOR_BGR2GRAY)
        brightness = float(gray.mean())
        sharpness = float(cv2.Laplacian(gray, cv2.CV_64F).var())
        if brightness < self.t.min_brightness:
            raise FaceValidationError("TOO_DARK")
        if brightness > self.t.max_brightness:
            raise FaceValidationError("TOO_BRIGHT")
        if sharpness < self.t.min_sharpness:
            raise FaceValidationError("TOO_BLURRY")

        scores, skin, found = self._detect_accessories(image, face, policy=policy)
        if found and enforce_accessories:
            raise accessories_error(found)
        real = self._real_probability(image, face, policy)
        traits = capture_traits(image, aligned)

        return FaceAnalysis(
            embedding=self.engine.represent(image, face, aligned),
            detection_score=face.score,
            quality_score=self._quality_score(face.score, sharpness, brightness),
            sharpness=sharpness,
            brightness=brightness,
            face_box=(face.x, face.y, face.width, face.height),
            pose=pose,
            accessories=scores,
            lower_face_skin=skin,
            accessories_found=found,
            real_probability=real,
            capture_digest=traits.digest,
            face_thumb=traits.thumb,
            image_size=traits.size,
        )

    # ------------------------------------------------------------------ liveness

    def analyze_turn(
        self, image_bytes: bytes, direction: TurnDirection, *, policy: FacePolicy = DEFAULT_POLICY
    ) -> FaceAnalysis:
        """Frame del reto de prueba de vida: la cabeza debe estar girada hacia `direction`.

        También pasa por el anti-spoofing: un video reproducido en una pantalla puede mostrar el
        giro, pero no deja de ser una pantalla."""
        image = self._decode(image_bytes, policy)
        # Un rostro girado obtiene menor puntuación en el detector: se usa el umbral secundario.
        face = self._single_face(image, min_score=self.t.secondary_detection_score)
        self._check_framing(image, face)
        pose = estimate_pose(face.landmarks)
        if not pose.turned(direction, self.t.liveness_min_yaw_ratio):
            raise FaceValidationError(
                "LIVENESS_TURN_NOT_DETECTED", {"yaw_ratio": pose.yaw_ratio, "expected": direction.value}
            )
        aligned = self.engine.align(image, face)
        traits = capture_traits(image, aligned)
        return FaceAnalysis(
            embedding=self.engine.represent(image, face, aligned),
            detection_score=face.score,
            quality_score=0.0,
            sharpness=0.0,
            brightness=float(cv2.cvtColor(aligned, cv2.COLOR_BGR2GRAY).mean()),
            face_box=(face.x, face.y, face.width, face.height),
            pose=pose,
            real_probability=self._real_probability(image, face, policy),
            capture_digest=traits.digest,
            face_thumb=traits.thumb,
            image_size=traits.size,
        )

    # ------------------------------------------------------------------ helpers

    def _real_probability(self, image: np.ndarray, face: DetectedFace, policy: FacePolicy) -> float | None:
        return self.antispoof.real_probability(image, face) if self.antispoof and policy.anti_spoofing else None

    def _decode(self, image_bytes: bytes, policy: FacePolicy) -> np.ndarray:
        return decode_image(
            image_bytes,
            min_dimension=self.t.min_image_dimension,
            max_dimension=self.t.max_image_dimension,
            reject_foreign=policy.reject_foreign_images,
        )

    def _single_face(self, image: np.ndarray, *, min_score: float) -> DetectedFace:
        # Se usa un umbral bajo para "ver" también posibles segundas personas.
        faces = self.engine.detect(image, min_score=min(self.t.secondary_detection_score, min_score))
        if not faces:
            raise FaceValidationError("NO_FACE")
        if len(faces) > 1:
            raise FaceValidationError("MULTIPLE_FACES")
        face = faces[0]
        if face.score < min_score:
            raise FaceValidationError("LOW_DETECTION_SCORE")
        return face

    def _check_framing(self, image: np.ndarray, face: DetectedFace) -> None:
        img_h, img_w = image.shape[:2]
        if min(face.width, face.height) < self.t.min_face_size_px:
            raise FaceValidationError("FACE_TOO_SMALL")
        margin = 0.05
        if (
            face.x < -face.width * margin
            or face.y < -face.height * margin
            or face.x + face.width > img_w * (1 + margin)
            or face.y + face.height > img_h * (1 + margin)
        ):
            raise FaceValidationError("FACE_CUT_OFF")

    def _detect_accessories(
        self, image: np.ndarray, face: DetectedFace, *, policy: FacePolicy
    ) -> tuple[AccessoryScores | None, float | None, tuple[Accessory, ...]]:
        # Si la empresa permite todos los accesorios, no se ejecuta CLIP (ahorra CPU).
        if self.accessories is None or not policy.any_accessory:
            return None, None, ()
        scores = self.accessories.score(image, face)
        skin = lower_face_skin_ratio(image, face) if scores.mask >= self.t.mask_threshold else None
        detected = {
            Accessory.GLASSES: scores.glasses >= self.t.glasses_threshold,
            Accessory.HEADWEAR: scores.headwear >= self.t.headwear_threshold,
            Accessory.MASK: self._mask_confirmed(scores.mask, skin),
        }
        found = [a for a, present in detected.items() if present and policy.blocks(a)]
        if found or max(scores.glasses, scores.headwear, scores.mask) >= 0.2:
            logger.info(
                "Accesorios: lentes=%.2f gorra=%.2f cubrebocas=%.2f piel_nariz=%s → %s",
                scores.glasses,
                scores.headwear,
                scores.mask,
                "n/d" if skin is None else f"{skin:.2f}",
                [a.value for a in found] or "ninguno",
            )
        return scores, skin, tuple(found)

    def _mask_confirmed(self, clip_probability: float, skin_ratio: float | None) -> bool:
        """CLIP sospecha + la nariz y mejillas están físicamente cubiertas (sin piel visible)."""
        if clip_probability < self.t.mask_threshold:
            return False
        if skin_ratio is None:
            return clip_probability >= self.t.mask_strict_threshold
        return skin_ratio < self.t.mask_max_skin_ratio

    def _quality_score(self, detection: float, sharpness: float, brightness: float) -> float:
        sharp_norm = min(sharpness / (self.t.min_sharpness * 5 or 1), 1.0)
        bright_norm = 1.0 - min(abs(brightness - 128.0) / 128.0, 1.0)
        return round(0.5 * detection + 0.3 * sharp_norm + 0.2 * bright_norm, 4)
