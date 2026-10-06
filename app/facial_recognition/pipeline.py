"""Pipeline de análisis facial.

analyze_frontal (registro y verificación):
    imagen → detección → una sola persona → tamaño/encuadre → pose frontal
    → iluminación/nitidez → accesorios (lentes, gorra, cubrebocas) → embedding

analyze_step (prueba de vida):
    imagen → detección → una sola persona → el movimiento del reto (girar, mirar arriba o abajo,
    acercarse) medido contra las frontales → embedding

analyze_flash (reto fotométrico):
    imagen → detección → una sola persona → cromaticidad del rostro y del fondo (photometry)

analyze_burst (ráfaga corta de recortes del rostro, antifraude 2a):
    hoja JPEG → recortes → rostro y puntos de cada uno → continuidad, micromovimiento, bucle, pulso (burst)
    → embeddings de los mejores recortes quietos (consenso de identidad)

accessories_of (registro con muchas fotos: CLIP solo en las referencias elegidas):
    imagen → detección → una sola persona → accesorios
"""

import hashlib
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum

import cv2
import numpy as np

from app.core.observability import observed
from app.facial_recognition.accessories import AccessoryScores, ClipAccessoryDetector
from app.facial_recognition.antispoof import MiniFasAntiSpoof
from app.facial_recognition.burst import (
    HOLD,
    MOVE,
    BurstLayout,
    Pulse,
    decode_sheet,
    gray,
    landmark_jumps,
    micro_motion,
    moire,
    noise_ratio,
    pulse,
    repeated_frames,
    skin_rgb,
    tiles_of,
)
from app.facial_recognition.engine import DetectedFace, FaceEngine, FaceLandmarks
from app.facial_recognition.errors import FaceValidationError
from app.facial_recognition.image_utils import decode_image
from app.facial_recognition.occlusion import lower_face_skin_ratio
from app.facial_recognition.phash import phash
from app.facial_recognition.photometry import FlashSample, flash_sample
from app.facial_recognition.pose import HeadPose, LivenessAction, StepTarget, estimate_pose, step_measure


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
    """Qué exige la empresa en cada captura (lo configura el ADMIN de la plataforma en su política)."""

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
    #: Calidad mínima de una captura frontal (0 = sin mínimo; ISO/IEC 29794-5: una imagen pobre
    #: compara mal y facilita los engaños).
    min_quality: float = 0.0

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
    # Ruido mínimo del fondo (σ, niveles 0-255) para medir el cociente de ruido rostro/fondo (antifraude 2a).
    noise_min_background: float = 0.3


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
    #: Paso del reto: cuánto se movió en el sentido pedido (pose.step_measure).
    step_value: float | None = None
    #: Huellas perceptuales (pHash de 64 bits) del rostro alineado y del cuadro completo: reconocen una captura
    #: reenviada aunque la hayan vuelto a comprimir o le hayan cambiado el brillo (anti-reenvío perceptual).
    face_phash: int | None = None
    frame_phash: int | None = None
    #: Los 5 puntos del rostro (ojos, nariz, comisuras; 5 x 2, px): el paralaje de los giros (antifraude 2a).
    landmarks: np.ndarray | None = None
    #: Señales físicas de la frontal a resolución completa (antifraude 2a): patrón de pantalla (dB) y ruido del
    #: rostro entre el del fondo (None si no se pudo medir).
    moire: float | None = None
    noise_ratio: float | None = None


@dataclass(frozen=True)
class BurstRules:
    """Cómo se mide la ráfaga (los da quien la pide, desde la configuración)."""

    #: Dos recortes no contiguos a menos de estos niveles de gris son el mismo fotograma (bucle).
    identical: float
    #: Salto máximo de los puntos entre recortes seguidos (distancias entre ojos).
    max_jump: float
    #: Tramo quieto mínimo (s) para medir el pulso, frecuencia de la serie y banda cardiaca (Hz).
    pulse_seconds: float
    pulse_rate: float
    pulse_low_hz: float
    pulse_high_hz: float
    #: Recortes quietos que se representan para el consenso de identidad (0 = ninguno: consenso apagado).
    identity_frames: int = 0


@dataclass(frozen=True)
class BurstAnalysis:
    """Lo medido en la ráfaga (solo números; la hoja ya se descartó)."""

    frames: int
    #: Recortes sin un único rostro y saltos entre recortes seguidos (continuidad).
    faceless: int
    jumps: int
    #: Micromovimiento del tramo quieto (None con menos de dos recortes) y fotogramas que repiten uno anterior.
    motion: float | None
    repeats: int
    #: Pulso por video del tramo quieto (None si fue muy corto o no hubo piel medible).
    pulse: Pulse | None
    #: Embeddings del primer recorte con rostro de cada tramo: la misma persona que las frontales (continuidad).
    anchors: tuple[np.ndarray, ...] = ()
    #: Embeddings de los mejores recortes quietos (un solo rostro, mayor confianza del detector; en orden de la toma):
    #: el consenso de identidad del video en vivo contra las muestras de la persona (`identity_core.burst_rejects`).
    identity: tuple[np.ndarray, ...] = ()


@dataclass(frozen=True)
class FlashCapture:
    """Fotograma del destello de colores: solo lo necesario para medir la respuesta y la toma."""

    sample: FlashSample
    face_box: tuple[int, int, int, int]
    image_size: tuple[int, int]
    capture_digest: str


#: Lado de la miniatura del rostro para comparar fotogramas.
THUMB_SIDE = 32


@dataclass(frozen=True)
class CaptureTraits:
    digest: str
    thumb: np.ndarray
    size: tuple[int, int]
    face_phash: int
    frame_phash: int


def points_of(landmarks: FaceLandmarks) -> np.ndarray:
    """Los 5 puntos como arreglo 5 x 2 (ojo, ojo, nariz, comisura, comisura)."""
    return np.array(
        [landmarks.eye_a, landmarks.eye_b, landmarks.nose, landmarks.mouth_a, landmarks.mouth_b], dtype=np.float64
    )


def capture_traits(image: np.ndarray, aligned: np.ndarray) -> CaptureTraits:
    """Huellas (exacta y perceptuales), miniatura y tamaño de una captura (para las comprobaciones contra engaños)."""
    gray = cv2.cvtColor(aligned, cv2.COLOR_BGR2GRAY)
    return CaptureTraits(
        digest=hashlib.sha256(image.tobytes()).hexdigest(),
        thumb=cv2.resize(gray, (THUMB_SIDE, THUMB_SIDE), interpolation=cv2.INTER_AREA).astype(np.float32),
        size=(int(image.shape[1]), int(image.shape[0])),
        face_phash=phash(gray),
        frame_phash=phash(cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)),
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

    @observed("face.frontal")
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

        aligned = self._align(image, face)
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
        quality = self._quality_score(face.score, sharpness, brightness)
        if quality < policy.min_quality:
            raise FaceValidationError("LOW_QUALITY", {"quality": quality, "required": policy.min_quality})
        real = self._real_probability(image, face, policy)
        traits = capture_traits(image, aligned)
        screen, noise = self._physical(image, (face.x, face.y, face.width, face.height))

        return FaceAnalysis(
            embedding=self._represent(image, face, aligned),
            detection_score=face.score,
            quality_score=quality,
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
            face_phash=traits.face_phash,
            frame_phash=traits.frame_phash,
            landmarks=points_of(face.landmarks),
            moire=screen,
            noise_ratio=noise,
        )

    # ------------------------------------------------------------------ liveness

    @observed("face.step")
    def analyze_step(
        self, image_bytes: bytes, action: LivenessAction, target: StepTarget, *, policy: FacePolicy = DEFAULT_POLICY
    ) -> FaceAnalysis:
        """Captura de un paso del reto: la persona hizo el movimiento pedido (girar, mirar arriba o abajo,
        acercarse), medido contra sus capturas frontales de la misma toma.

        También pasa por el anti-spoofing: un video reproducido en una pantalla puede mostrar el
        movimiento, pero no deja de ser una pantalla."""
        image = self._decode(image_bytes, policy)
        # Un rostro girado o inclinado obtiene menor puntuación en el detector: se usa el umbral secundario.
        face = self._single_face(image, min_score=self.t.secondary_detection_score)
        self._check_framing(image, face)
        pose = estimate_pose(face.landmarks)
        measured = step_measure(action, pose, face.width, target)
        if measured < target.required(action):
            details = {"measured": round(measured, 4), "required": target.required(action), "expected": action.value}
            raise FaceValidationError("LIVENESS_STEP_NOT_DETECTED", details)
        aligned = self._align(image, face)
        traits = capture_traits(image, aligned)
        return FaceAnalysis(
            embedding=self._represent(image, face, aligned),
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
            step_value=round(measured, 4),
            landmarks=points_of(face.landmarks),
        )

    @observed("face.flash")
    def analyze_flash(self, image_bytes: bytes, *, policy: FacePolicy = DEFAULT_POLICY) -> FlashCapture:
        """Fotograma del destello de colores: una sola persona en el encuadre y su cromaticidad (sin
        embedding ni anti-spoofing: el color cambia la imagen a propósito)."""
        image = self._decode(image_bytes, policy)
        face = self._single_face(image, min_score=self.t.secondary_detection_score)
        self._check_framing(image, face)
        box = (face.x, face.y, face.width, face.height)
        return FlashCapture(
            sample=flash_sample(image, box),
            face_box=box,
            image_size=(int(image.shape[1]), int(image.shape[0])),
            capture_digest=hashlib.sha256(image.tobytes()).hexdigest(),
        )

    @observed("face.reference_accessories")
    def accessories_of(
        self, image_bytes: bytes, *, policy: FacePolicy = DEFAULT_POLICY
    ) -> tuple[AccessoryScores | None, float | None, tuple[Accessory, ...]]:
        """Solo los accesorios (CLIP) de una captura frontal YA validada: (puntajes, piel de nariz y mejillas,
        accesorios que la empresa bloquea). El registro facial analiza sus muchas fotos sin CLIP (lo más caro) y lo
        corre únicamente en las que elige como referencias (`enrollment_selection`). Sin detector o sin ningún accesorio
        bloqueado no decodifica nada."""
        if self.accessories is None or not policy.any_accessory:
            return None, None, ()
        image = self._decode(image_bytes, policy)
        face = self._single_face(image, min_score=self.t.min_detection_score)
        return self._detect_accessories(image, face, policy=policy)

    @observed("face.burst")
    def analyze_burst(self, data: bytes, layout: BurstLayout, rules: BurstRules) -> BurstAnalysis:
        """La ráfaga corta de recortes del rostro (antifraude 2a): continuidad (un mismo rostro sin saltos),
        micromovimiento natural, fotogramas repetidos (bucle) y pulso por video. Lanza `BurstMalformed` si la hoja no es
        lo que dice su descripción (una señal para quien la pidió, nunca un error). Todo en memoria: la hoja se descarta
        al terminar.

        Con `rules.identity_frames` representa además los mejores recortes QUIETOS (un solo rostro, la mayor confianza
        del detector; a igual confianza, el más temprano) para el consenso de identidad: reutiliza los rostros ya
        localizados y un recorte que ya es ancla no se vuelve a representar (cada uno cuesta ≈ 40 ms)."""
        tiles = tiles_of(decode_sheet(data, layout), layout)
        located = {i: face for i, tile in enumerate(tiles) if (face := self._tile_face(tile)) is not None}
        grays = [gray(tile) for tile in tiles]
        points = [points_of(located[i].landmarks) if i in located else None for i in range(len(tiles))]
        segments = (layout.indices(HOLD), layout.indices(MOVE))
        firsts = [next((i for i in part if i in located), None) for part in segments]
        best = sorted((i for i in segments[0] if i in located), key=lambda i: (-located[i].score, i))
        embedded: dict[int, np.ndarray] = {}

        def embed(i: int) -> np.ndarray:
            if i not in embedded:
                embedded[i] = self._represent(tiles[i], located[i], self._align(tiles[i], located[i]))
            return embedded[i]

        return BurstAnalysis(
            frames=len(tiles),
            faceless=len(tiles) - len(located),
            jumps=sum(landmark_jumps([points[i] for i in part], rules.max_jump) for part in segments),
            motion=micro_motion([grays[i] for i in segments[0]]),
            repeats=sum(repeated_frames([grays[i] for i in part], rules.identical) for part in segments),
            pulse=_pulse(tiles, located, segments[0], layout.times, rules),
            anchors=tuple(embed(i) for i in firsts if i is not None),
            identity=tuple(embed(i) for i in sorted(best[: rules.identity_frames])),
        )

    # ------------------------------------------------------------------ helpers

    def _tile_face(self, tile: np.ndarray) -> DetectedFace | None:
        """El rostro de un recorte de la ráfaga, si hay exactamente uno."""
        faces = self.engine.detect(tile, min_score=self.t.secondary_detection_score)
        return faces[0] if len(faces) == 1 else None

    @observed("face.physical")
    def _physical(self, image: np.ndarray, box: tuple[int, int, int, int]) -> tuple[float | None, float | None]:
        """Señales físicas de una frontal a resolución completa (antifraude 2a): moiré y ruido rostro/fondo."""
        return moire(image, box), noise_ratio(image, box, min_background=self.t.noise_min_background)

    @observed("face.align")
    def _align(self, image: np.ndarray, face: DetectedFace) -> np.ndarray:
        return self.engine.align(image, face)

    @observed("face.embed")
    def _represent(self, image: np.ndarray, face: DetectedFace, aligned: np.ndarray) -> np.ndarray:
        return self.engine.represent(image, face, aligned)

    @observed("face.antispoof")
    def _real_probability(self, image: np.ndarray, face: DetectedFace, policy: FacePolicy) -> float | None:
        return self.antispoof.real_probability(image, face) if self.antispoof and policy.anti_spoofing else None

    @observed("face.decode")
    def _decode(self, image_bytes: bytes, policy: FacePolicy) -> np.ndarray:
        return decode_image(
            image_bytes,
            min_dimension=self.t.min_image_dimension,
            max_dimension=self.t.max_image_dimension,
            reject_foreign=policy.reject_foreign_images,
        )

    @observed("face.detect")
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

    @observed("face.accessories")
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


def _pulse(
    tiles: list[np.ndarray],
    located: dict[int, DetectedFace],
    hold: list[int],
    times: tuple[int, ...],
    rules: BurstRules,
) -> Pulse | None:
    """El pulso por video del tramo quieto: el color de la piel de cada recorte con rostro, si el tramo dura lo
    suficiente (con 1-2 s la banda cardiaca ni siquiera se resuelve)."""
    samples = [
        (rgb, times[i])
        for i in hold
        if i in located
        and (rgb := skin_rgb(tiles[i], (located[i].x, located[i].y, located[i].width, located[i].height))) is not None
    ]
    if not samples or (samples[-1][1] - samples[0][1]) / 1000 < rules.pulse_seconds:
        return None
    return pulse(
        [rgb for rgb, _ in samples],
        [t for _, t in samples],
        rate=rules.pulse_rate,
        low_hz=rules.pulse_low_hz,
        high_hz=rules.pulse_high_hz,
    )
