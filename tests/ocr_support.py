"""Motor de OCR simulado para las pruebas (el real tiene las suyas: `tests/test_ocr.py` y `tests/test_ocr_real.py`).

Ningún caso de la suite ejecuta Tesseract: `use_backend(FakeOcr())` (autouse en `conftest`) lo reemplaza, como el bucket
con `FakeStorage` y la voz con `FakeSpeech`. `read_text` devuelve el texto que la prueba configuró (p. ej. la MRZ de un
pasaporte o la CURP de una INE), sin mirar los bytes de la imagen (que ya vienen limpios de `document_files`). `down`
hace que falle como si Tesseract no estuviera (el servicio degrada a sin texto: mejor esfuerzo, nunca bloquea).
"""

from app.ocr import OcrText, OcrUnavailable


class FakeOcr:
    def __init__(self) -> None:
        #: Lo que el motor «lee» de la imagen (lo configura la prueba antes de subir el documento).
        self.text = ""
        self.confidence = 0.9
        self.down = False
        #: Cuántas veces se llamó y con qué idiomas (para verificar que el servicio NO llame al OCR con un PDF).
        self.calls = 0
        self.languages: list[str] = []

    def read_text(self, image: bytes, languages: str) -> OcrText:
        self.calls += 1
        self.languages.append(languages)
        if self.down:
            raise OcrUnavailable("OCR falso no disponible")
        return OcrText(text=self.text, confidence=self.confidence)
