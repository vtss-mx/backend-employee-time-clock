"""Motor basado en OpenCV: YuNet (detección) + SFace (embeddings).

- YuNet: detector CNN ligero que devuelve bbox, 5 landmarks y puntuación.
- SFace: red de reconocimiento entrenada con pérdida tipo ArcFace; produce un
  embedding de 128 dimensiones. La similitud coseno entre embeddings del mismo
  individuo suele ser > 0.36 (umbral recomendado por OpenCV: 0.363).

Ambos modelos se ejecutan con el backend DNN de OpenCV (CPU), sin dependencias
nativas adicionales (no requiere dlib, TensorFlow ni GPU).
"""

import threading
from pathlib import Path

import cv2
import numpy as np

from app.facial_recognition.engine import DetectedFace, FaceEngine, FaceLandmarks
from app.facial_recognition.model_store import DETECTOR_MODEL, FACENET_MODEL, RECOGNIZER_MODEL, ensure_models


class OpenCVFaceEngine(FaceEngine):
    model_name = "opencv-sface-2021dec"
    embedding_dim = 128

    def __init__(self, models_dir: str | Path, auto_download: bool = True) -> None:
        paths = ensure_models(models_dir, download=auto_download, models=(DETECTOR_MODEL, RECOGNIZER_MODEL))
        # Umbral bajo en el detector: el filtrado fino se hace en el pipeline para
        # poder detectar también rostros secundarios (validación "una sola persona").
        self._detector = cv2.FaceDetectorYN.create(str(paths[DETECTOR_MODEL.filename]), "", (320, 320), 0.5, 0.3, 50)
        self._recognizer = cv2.FaceRecognizerSF.create(str(paths[RECOGNIZER_MODEL.filename]), "")
        # Las redes DNN de OpenCV no son thread-safe. Cada worker del pool tiene su propia
        # instancia, así que este lock nunca se disputa entre solicitudes (no hay cuello de botella).
        self._lock = threading.Lock()

    def detect(self, image_bgr: np.ndarray, min_score: float) -> list[DetectedFace]:
        height, width = image_bgr.shape[:2]
        with self._lock:
            self._detector.setInputSize((width, height))
            _, faces = self._detector.detect(image_bgr)
        if faces is None:
            return []
        rows: list[np.ndarray] = list(np.asarray(faces, dtype=np.float32))
        detected = [
            DetectedFace(
                x=int(row[0]),
                y=int(row[1]),
                width=int(row[2]),
                height=int(row[3]),
                score=float(row[14]),
                # YuNet: ojo derecho, ojo izquierdo, punta de nariz, comisuras de la boca.
                landmarks=FaceLandmarks(
                    eye_a=(float(row[4]), float(row[5])),
                    eye_b=(float(row[6]), float(row[7])),
                    nose=(float(row[8]), float(row[9])),
                    mouth_a=(float(row[10]), float(row[11])),
                    mouth_b=(float(row[12]), float(row[13])),
                ),
                raw=row.copy(),
            )
            for row in rows
            if float(row[14]) >= min_score
        ]
        return sorted(detected, key=lambda f: f.score, reverse=True)

    def align(self, image_bgr: np.ndarray, face: DetectedFace) -> np.ndarray:
        with self._lock:
            return self._recognizer.alignCrop(image_bgr, face.raw)

    def embed(self, aligned_face_bgr: np.ndarray) -> np.ndarray:
        with self._lock:
            feature = self._recognizer.feature(aligned_face_bgr)
        vector = np.asarray(feature, dtype=np.float32).reshape(-1)
        norm = float(np.linalg.norm(vector))
        if norm == 0.0:
            raise ValueError("Embedding inválido (norma cero)")
        return vector / norm


class FusionFaceEngine(OpenCVFaceEngine):
    """SFace (128-d) + FaceNet-512 VGGFace2 (512-d) fusionados a nivel de puntuación.

    Dos redes entrenadas con datos y arquitecturas distintas cometen errores distintos.
    El embedding final es la concatenación [√w·SFace, √(1-w)·FaceNet] (norma 1), de modo que
    la similitud coseno resultante es exactamente w·cos_SFace + (1-w)·cos_FaceNet: el matcher,
    el almacenamiento cifrado y los umbrales funcionan sin cambios.

    LFW, 6000 pares, mismo umbral 0.40 (FAR 0.03 %):  SFace 97.57 % → fusión 99.13 % aceptados.
    """

    def __init__(self, models_dir: str | Path, auto_download: bool = True, sface_weight: float = 0.5) -> None:
        import onnxruntime as ort

        super().__init__(models_dir, auto_download)
        path = ensure_models(models_dir, download=auto_download, models=(FACENET_MODEL,))[FACENET_MODEL.filename]
        options = ort.SessionOptions()
        options.intra_op_num_threads = 1  # paralelismo vía workers (1 por núcleo)
        options.inter_op_num_threads = 1
        self._facenet = ort.InferenceSession(str(path), options, providers=["CPUExecutionProvider"])
        self._w_sface = float(np.sqrt(sface_weight))
        self._w_facenet = float(np.sqrt(1.0 - sface_weight))
        self.model_name = f"fusion-sface128+facenet512-w{sface_weight:.2f}"
        self.embedding_dim = 128 + 512

    @staticmethod
    def _facenet_input(image_bgr: np.ndarray, face: DetectedFace, margin: float = 0.2) -> np.ndarray:
        """Recorte cuadrado con margen (como MTCNN en el entrenamiento), 160x160, normalizado."""
        h, w = image_bgr.shape[:2]
        side = max(face.width, face.height) * (1 + margin)
        cx, cy = face.x + face.width / 2, face.y + face.height / 2
        left, top = int(max(0, cx - side / 2)), int(max(0, cy - side / 2))
        right, bottom = int(min(w, cx + side / 2)), int(min(h, cy + side / 2))
        crop = cv2.resize(image_bgr[top:bottom, left:right], (160, 160))[:, :, ::-1].astype(np.float32)
        return ((crop - 127.5) / 128.0).transpose(2, 0, 1)[None]

    def represent(self, image_bgr: np.ndarray, face: DetectedFace, aligned_face_bgr: np.ndarray) -> np.ndarray:
        sface = self.embed(aligned_face_bgr)
        facenet = self._facenet.run(None, {"input": self._facenet_input(image_bgr, face)})[0][0].astype(np.float32)
        norm = float(np.linalg.norm(facenet))
        if norm == 0.0:
            raise ValueError("Embedding FaceNet inválido (norma cero)")
        return np.concatenate([self._w_sface * sface, self._w_facenet * (facenet / norm)]).astype(np.float32)
