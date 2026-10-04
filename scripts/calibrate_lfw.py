"""Mide los modelos faciales con LFW (6 000 pares estándar, 10 particiones) y ajusta su calibración.

Para qué sirve: `app/facial_recognition/calibration.py` convierte la similitud coseno en una
confianza con una regresión logística ajustada sobre LFW. Este script reproduce esa medición con el
MISMO código de producción (decodificación, detector YuNet, alineación y `FusionFaceEngine`) para:

- obtener, por cada par, la similitud de SFace, la de FaceNet y la fusionada
  (w·SFace + (1-w)·FaceNet, la que usa la app);
- ajustar (a, b) por modelo: confianza = 1 / (1 + e^-(a + b·similitud)), clases balanceadas;
- dar la base para decidir umbrales (p. ej. un piso por modelo, para que una imagen diseñada para
  engañar a un solo modelo no baste).

No forma parte de la app ni descarga datos: LFW (lfw.tgz, imágenes originales sin alinear, y
pairs.txt) se descarga aparte, p. ej. de las copias de scikit-learn en figshare
(https://ndownloader.figshare.com/files/5976018 y .../5976006) o de http://vis-www.cs.umass.edu/lfw/.
El conjunto de datos no se guarda en el repositorio.

Uso, en un contenedor desechable con la imagen de producción (trae los modelos en /app/models):

    docker run --rm --cpus 2 --entrypoint python -e PYTHONPATH=/app \\
        -v "$LFW_DIR:/work" -v "$PWD/scripts:/scripts:ro" time-clock-backend \\
        /scripts/calibrate_lfw.py --lfw /work/lfw --pairs /work/pairs.txt --out /work/lfw_scores.json

Qué rostro se mide (`--face`): la app rechaza una captura con dos personas (MULTIPLE_FACES), pero
~15 % de las imágenes de LFW trae más de un rostro válido y la puntuación de YuNet se satura (~0.93):
el «más confiable» a veces es alguien del fondo y contamina los pares genuinos (en la medición de
2026-10 explicó 158 de los 189 genuinos rechazados al 80 %). Por eso, por omisión se toma el rostro
central (`central`: LFW centra a la persona etiquetada); `confident` toma el de mayor puntuación.
En ambos casos el rostro debe alcanzar FACE_DETECTION_MIN_SCORE, como en producción; un par con una
imagen sin rostro se omite y se cuenta. El JSON de salida trae los valores de cada par, los pares
omitidos y el resumen (distribuciones y calibración por modelo).
"""

import argparse
import json
import os
import sys
import time
from collections.abc import Iterable, Sequence
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import numpy as np

if TYPE_CHECKING:  # importar la app en tiempo de ejecución exige configurarla antes (`_configure`)
    from app.facial_recognition.engine import DetectedFace

#: Percentiles que se reportan de cada distribución.
PERCENTILES = (0.1, 1.0, 5.0, 50.0)
MODELS = ("sface", "facenet", "fusion")


@dataclass(frozen=True)
class Pair:
    fold: int
    same: bool
    first: str
    second: str


def image_name(person: str, number: str) -> str:
    return f"{person}/{person}_{int(number):04d}.jpg"


def read_pairs(path: Path) -> list[Pair]:
    """pairs.txt: cabecera «particiones pares_por_clase»; en cada partición, primero los pares de la
    misma persona (nombre, n1, n2) y después los de personas distintas (nombre1, n1, nombre2, n2)."""
    lines = path.read_text().splitlines()
    folds, per_class = (int(v) for v in lines[0].split())
    rows = [line.split("\t") for line in lines[1:] if line.strip()]
    if len(rows) != folds * per_class * 2:
        raise ValueError(f"pairs.txt: se esperaban {folds * per_class * 2} pares y hay {len(rows)}")
    pairs = []
    for index, row in enumerate(rows):
        fold = index // (per_class * 2)
        if len(row) == 3:
            pairs.append(Pair(fold, True, image_name(row[0], row[1]), image_name(row[0], row[2])))
        else:
            pairs.append(Pair(fold, False, image_name(row[0], row[1]), image_name(row[2], row[3])))
    return pairs


# ------------------------------------------------------------------ extracción (procesos hijos)

_state: dict = {}


def _configure() -> None:
    """La configuración de la app exige base de datos, firma de tokens y llave de cifrado al importarse.
    Este script no se conecta, no firma ni cifra nada: bastan valores desechables (como en las pruebas).
    Los ajustes faciales (FACE_*, MIN/MAX_IMAGE_DIMENSION) se respetan si vienen en el entorno."""
    import secrets

    from cryptography.fernet import Fernet

    os.environ.update(
        {
            "DATABASE_URL": "sqlite://",
            "JWT_ALGORITHM": "HS256",
            "JWT_SECRET_KEY": secrets.token_hex(32),
            "DATA_ENCRYPTION_KEY": Fernet.generate_key().decode(),
        }
    )


def _init_worker(min_score: float, face: str) -> None:
    """Un motor por proceso (las redes DNN de OpenCV no son thread-safe), con 1 hilo como en producción."""
    _configure()
    import cv2

    from app.core.config import settings
    from app.facial_recognition.opencv_engine import FusionFaceEngine

    cv2.setNumThreads(1)
    weight = settings.FACE_FUSION_SFACE_WEIGHT
    _state["engine"] = FusionFaceEngine(settings.FACE_MODELS_DIR, auto_download=False, sface_weight=weight)
    _state["min_score"] = min_score
    _state["central"] = face == "central"
    _state["dimensions"] = (settings.MIN_IMAGE_DIMENSION, settings.MAX_IMAGE_DIMENSION)


def _represent(path: str) -> tuple[str, int, list[float] | None, list[float] | None]:
    """Embeddings de SFace y FaceNet (norma 1) del rostro elegido, igual que `analyze_frontal`, y cuántos
    rostros alcanzaron la puntuación mínima (0 = sin rostro)."""
    from app.facial_recognition.image_utils import decode_image

    engine = _state["engine"]
    min_dimension, max_dimension = _state["dimensions"]
    image = decode_image(Path(path).read_bytes(), min_dimension=min_dimension, max_dimension=max_dimension)
    faces = engine.detect(image, min_score=_state["min_score"])
    if not faces:
        return path, 0, None, None
    face = _central(faces, image.shape[1], image.shape[0]) if _state["central"] else faces[0]
    fused = engine.represent(image, face, engine.align(image, face))
    # La fusión es [√w·SFace, √(1-w)·FaceNet]: se separa para medir cada modelo por su cuenta.
    sface, facenet = _unit(fused[:128]), _unit(fused[128:])
    return path, len(faces), sface.tolist(), facenet.tolist()


def _central(faces: Sequence[DetectedFace], width: int, height: int) -> DetectedFace:
    """El rostro cuyo centro está más cerca del centro de la imagen."""
    return min(faces, key=lambda f: (f.x + f.width / 2 - width / 2) ** 2 + (f.y + f.height / 2 - height / 2) ** 2)


def _unit(vector: np.ndarray) -> np.ndarray:
    return vector / float(np.linalg.norm(vector))


def extract(root: Path, names: Sequence[str], workers: int, min_score: float, face: str) -> dict[str, dict]:
    """Embeddings por imagen (en paralelo, un proceso por núcleo asignado)."""
    paths = [str(root / name) for name in names]
    found: dict[str, dict] = {}
    started = time.monotonic()
    with ProcessPoolExecutor(workers, initializer=_init_worker, initargs=(min_score, face)) as pool:
        for index, (path, count, sface, facenet) in enumerate(pool.map(_represent, paths, chunksize=16), 1):
            name = str(Path(path).relative_to(root))
            found[name] = {"faces": count, "sface": sface, "facenet": facenet}
            if index % 500 == 0:
                print(f"  {index}/{len(paths)} imágenes ({time.monotonic() - started:.0f} s)", file=sys.stderr)
    return found


# ------------------------------------------------------------------ análisis


def logistic_fit(similarity: np.ndarray, same: np.ndarray, iterations: int = 100) -> tuple[float, float]:
    """(a, b) de confianza = 1 / (1 + e^-(a + b·s)) por máxima verosimilitud (Newton), sin
    regularización y con clases balanceadas (cada clase pesa lo mismo aunque se omitan pares)."""
    weights = np.where(same, len(same) / (2 * same.sum()), len(same) / (2 * (~same).sum()))
    design = np.column_stack([np.ones_like(similarity), similarity])
    params = np.zeros(2)
    for _ in range(iterations):
        probability = 1.0 / (1.0 + np.exp(-(design @ params)))
        gradient = design.T @ (weights * (same - probability))
        hessian = (design * (weights * probability * (1 - probability))[:, None]).T @ design
        step = np.linalg.solve(hessian, gradient)
        params += step
        if float(np.abs(step).max()) < 1e-10:
            break
    return round(float(params[0]), 4), round(float(params[1]), 4)


def distribution(values: np.ndarray) -> dict[str, float]:
    stats = {"mean": values.mean(), "std": values.std(ddof=1)}
    stats |= {f"p{p:g}": np.percentile(values, p) for p in PERCENTILES}
    stats |= {"min": values.min(), "max": values.max()}
    return {"n": len(values)} | {k: round(float(v), 6) for k, v in stats.items()}


def summarize(rows: Sequence[dict]) -> dict:
    same = np.array([row["same"] for row in rows])
    summary: dict = {}
    for model in MODELS:
        values = np.array([row[model] for row in rows])
        a, b = logistic_fit(values, same)
        summary[model] = {
            "genuine": distribution(values[same]),
            "impostor": distribution(values[~same]),
            "calibration": {"a": a, "b": b},
        }
    return summary


def pair_rows(pairs: Iterable[Pair], found: dict[str, dict], weight: float) -> tuple[list[dict], list[dict]]:
    rows, skipped = [], []
    for pair in pairs:
        first, second = found[pair.first], found[pair.second]
        if not first["faces"] or not second["faces"]:
            skipped.append({"fold": pair.fold, "same": pair.same, "first": pair.first, "second": pair.second})
            continue
        sface = float(np.dot(first["sface"], second["sface"]))
        facenet = float(np.dot(first["facenet"], second["facenet"]))
        rows.append(
            {
                "fold": pair.fold,
                "same": pair.same,
                "first": pair.first,
                "second": pair.second,
                "sface": round(sface, 6),
                "facenet": round(facenet, 6),
                "fusion": round(weight * sface + (1 - weight) * facenet, 6),
            }
        )
    return rows, skipped


def print_summary(summary: dict, skipped: int, total: int) -> None:
    print(f"Pares evaluados: {total - skipped} de {total} (omitidos por no hallar rostro: {skipped})")
    for model, data in summary.items():
        calibration = data["calibration"]
        print(f"\n{model}: a={calibration['a']}, b={calibration['b']}")
        for kind in ("genuine", "impostor"):
            stats = data[kind]
            cells = " ".join(f"{k}={v:.4f}" for k, v in stats.items() if k != "n")
            print(f"  {kind:<8} n={stats['n']} {cells}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--lfw", type=Path, required=True, help="carpeta de lfw.tgz extraído (persona/imagen.jpg)")
    parser.add_argument("--pairs", type=Path, required=True, help="pairs.txt (6 000 pares, 10 particiones)")
    parser.add_argument("--out", type=Path, required=True, help="JSON de salida")
    parser.add_argument("--workers", type=int, help="procesos (omisión: 1 por núcleo asignado al contenedor)")
    parser.add_argument("--min-score", type=float, help="puntuación mínima del rostro (omisión: la de producción)")
    parser.add_argument("--face", choices=("central", "confident"), default="central", help="rostro que se mide")
    args = parser.parse_args()

    _configure()
    from app.core.config import settings
    from app.core.system import available_cpus

    min_score = args.min_score if args.min_score is not None else settings.FACE_DETECTION_MIN_SCORE
    weight = settings.FACE_FUSION_SFACE_WEIGHT
    workers = args.workers or available_cpus()
    pairs = read_pairs(args.pairs)
    names = sorted({name for pair in pairs for name in (pair.first, pair.second)})
    print(f"{len(pairs)} pares, {len(names)} imágenes distintas, {workers} procesos", file=sys.stderr)

    found = extract(args.lfw, names, workers, min_score, args.face)
    rows, skipped = pair_rows(pairs, found, weight)
    summary = summarize(rows)
    no_face = sorted(name for name, data in found.items() if not data["faces"])
    report = {
        "meta": {
            "sface_weight": weight,
            "min_detection_score": min_score,
            "face_selection": args.face,
            "images": len(names),
            "images_with_several_faces": sum(data["faces"] > 1 for data in found.values()),
            "images_without_face": no_face,
            "pairs_total": len(pairs),
            "pairs_skipped": len(skipped),
        },
        "summary": summary,
        "skipped": skipped,
        "pairs": rows,
    }
    args.out.write_text(json.dumps(report, ensure_ascii=False, indent=1))
    print_summary(summary, len(skipped), len(pairs))
    print(f"\nJSON: {args.out}")


if __name__ == "__main__":
    main()
