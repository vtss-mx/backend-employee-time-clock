"""Genera app/facial_recognition/data/clip_prompts.json (centroides de texto CLIP).

Solo se ejecuta cuando se modifican los prompts; en tiempo de ejecución la API usa
únicamente el codificador de imagen. Requiere (no incluido en requirements.txt):

    pip install onnxruntime tokenizers numpy
    curl -LO https://huggingface.co/Xenova/clip-vit-base-patch32/resolve/main/onnx/text_model.onnx
    curl -LO https://huggingface.co/Xenova/clip-vit-base-patch32/resolve/main/tokenizer.json
    python scripts/build_clip_prompts.py text_model.onnx tokenizer.json

Para cada accesorio se promedian los embeddings de varios prompts positivos y
negativos (prompt ensembling). La probabilidad se calcula como
sigmoid(100 * (cos(img, pos) - cos(img, neg))).

Evaluación (CelebA, 3,070 imágenes, solo para calibrar umbrales; no se entrena con ella):
  lentes:   AUC 0.986, recall 94.6 % con FPR 1 % (umbral 0.61), recall 95.5 % con FPR 2 % (0.53)
  gorra:    AUC 0.983, recall 89.3 % con FPR 1 % (umbral 0.69), recall 93.6 % con FPR 2 % (0.59)
  (varios "falsos positivos" revisados manualmente eran gorros mal etiquetados en CelebA)
  cubrebocas (500 imágenes de DamarJati/Face-Mask-Detection): AUC 0.992,
            recall 80 % con umbral 0.30; FPR 0.5 % en CelebA (0 % en personas con lentes de sol)
"""

import json
import sys
from pathlib import Path

import numpy as np
import onnxruntime as ort
from tokenizers import Tokenizer

PROMPTS = {
    "glasses": {
        "positive": [
            "a photo of a person wearing eyeglasses",
            "a photo of a person wearing glasses",
            "a photo of a person wearing sunglasses",
            "a close-up portrait of someone wearing spectacles",
        ],
        "negative": [
            "a photo of a person with bare eyes and no glasses",
            "a close-up portrait of a person's face",
            "a photo of a person's face showing their eyes clearly",
        ],
    },
    "headwear": {
        "positive": [
            "a photo of a person wearing a cap",
            "a photo of a person wearing a hat",
            "a photo of a person wearing a baseball cap",
            "a photo of a person wearing a beanie",
            "a photo of a person wearing a sun visor",
            "a photo of a person wearing a helmet",
        ],
        "negative": [
            "a photo of a person with nothing on their head",
            "a close-up portrait of a person's face",
            "a photo of a person showing their hair and forehead",
            "a photo of a person with a bare head",
        ],
    },
    "mask": {
        "positive": [
            "a photo of a person wearing a surgical face mask covering the mouth and nose",
            "a photo of a person wearing a medical mask",
            "a photo of a person wearing a cloth face mask over the mouth",
        ],
        # Los negativos incluyen los distractores (lentes de sol, pañoletas, sombreros)
        # que CLIP tiende a asociar con la palabra "mask".
        "negative": [
            "a close-up portrait of a person's face",
            "a photo of a person's face with mouth and nose visible",
            "a photo of a person wearing sunglasses",
            "a photo of a person wearing a headscarf",
            "a photo of a person smiling",
            "a photo of a person wearing a hat",
        ],
    },
}


def main(text_model: str, tokenizer_path: str) -> None:
    tokenizer = Tokenizer.from_file(tokenizer_path)
    session = ort.InferenceSession(text_model)

    def centroid(prompts: list[str]) -> list[float]:
        ids = np.zeros((len(prompts), 77), dtype=np.int64)
        for i, prompt in enumerate(prompts):
            encoded = tokenizer.encode(prompt).ids[:77]
            ids[i, : len(encoded)] = encoded
        emb = session.run(["text_embeds"], {"input_ids": ids})[0]
        emb = emb / np.linalg.norm(emb, axis=1, keepdims=True)
        mean = emb.mean(axis=0)
        return (mean / np.linalg.norm(mean)).round(6).tolist()

    output = {
        "model": "Xenova/clip-vit-base-patch32",
        "logit_scale": 100.0,
        "attributes": {
            name: {"prompts": p, "positive": centroid(p["positive"]), "negative": centroid(p["negative"])}
            for name, p in PROMPTS.items()
        },
    }
    target = Path(__file__).resolve().parents[1] / "app" / "facial_recognition" / "data" / "clip_prompts.json"
    target.write_text(json.dumps(output))
    print(f"OK -> {target}")


if __name__ == "__main__":
    main(*sys.argv[1:3])
