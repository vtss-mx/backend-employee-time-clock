"""Reproduce la conversión a ONNX de los modelos incluidos en app/facial_recognition/data/models.

No forma parte de la app (requiere PyTorch). Ejecútalo en un contenedor desechable:

    git clone --depth 1 https://github.com/minivision-ai/Silent-Face-Anti-Spoofing.git /work/sfas
    pip install torch==2.5.1 torchvision==0.20.1 --index-url https://download.pytorch.org/whl/cpu
    pip install onnx onnxruntime==1.20.1 "numpy<2" tqdm requests && pip install --no-deps facenet-pytorch==2.6.0
    python scripts/export_models.py      # escribe /work/onnx/*.onnx

Después se cuantiza FaceNet a int8 (misma precisión en LFW, 4x más pequeño, 2.5x más rápido):

    from onnxruntime.quantization import quantize_dynamic, QuantType
    quantize_dynamic("facenet512_vggface2.onnx", "facenet512_vggface2_int8.onnx", weight_type=QuantType.QUInt8)

Licencias: facenet-pytorch (MIT), Silent-Face-Anti-Spoofing (Apache 2.0). Copias en data/models/.
Si cambias un modelo, actualiza su SHA-256 en app/facial_recognition/model_store.py.
"""

import hashlib
import os
import sys
from pathlib import Path

import torch

WORK = Path(os.environ.get("EXPORT_WORKDIR", "/work"))
OUT = WORK / "onnx"
ANTISPOOF = [("2.7_80x80_MiniFASNetV2.pth", "MiniFASNetV2"), ("4_0_0_80x80_MiniFASNetV1SE.pth", "MiniFASNetV1SE")]


def export(model: torch.nn.Module, shape: tuple[int, ...], path: Path, output: str) -> None:
    torch.onnx.export(
        model.eval(),
        torch.randn(*shape),
        str(path),
        input_names=["input"],
        output_names=[output],
        dynamic_axes={"input": {0: "n"}, output: {0: "n"}},
        opset_version=17,
        dynamo=False,
    )


def export_facenet() -> None:
    from facenet_pytorch import InceptionResnetV1  # FaceNet-512, VGGFace2 (MIT)

    export(InceptionResnetV1(pretrained="vggface2"), (1, 3, 160, 160), OUT / "facenet512_vggface2.onnx", "embedding")


def export_antispoof() -> None:
    sys.path.insert(0, str(WORK / "sfas"))
    from src.model_lib import MiniFASNet  # Silent-Face-Anti-Spoofing (Apache 2.0)
    from src.utility import get_kernel, parse_model_name

    for weights, cls_name in ANTISPOOF:
        h, w, _, _ = parse_model_name(weights)
        model = getattr(MiniFASNet, cls_name)(conv6_kernel=get_kernel(h, w))
        state = torch.load(WORK / "sfas/resources/anti_spoof_models" / weights, map_location="cpu")
        state = {k.removeprefix("module."): v for k, v in state.items()}
        # Los pesos V1SE usan nombres antiguos del bloque SE (se_fc1 → se_module.fc1).
        state = {k.replace(".se_fc", ".se_module.fc").replace(".se_bn", ".se_module.bn"): v for k, v in state.items()}
        model.load_state_dict(state)  # estricto: todos los pesos deben cargarse
        export(model, (1, 3, h, w), OUT / weights.replace(".pth", ".onnx"), "logits")


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    export_facenet()
    export_antispoof()
    for path in sorted(OUT.iterdir()):
        print(path.name, path.stat().st_size, hashlib.sha256(path.read_bytes()).hexdigest())


if __name__ == "__main__":
    main()
