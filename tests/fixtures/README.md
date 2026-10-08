# Imágenes de prueba

- `astronaut.png` (512 × 512): retrato de la astronauta Eileen Collins, de la base de imágenes de la
  NASA. **Dominio público** (obra del gobierno de EE. UU.); es la imagen de prueba `astronaut` de
  scikit-image (v0.19.3). Se usa para probar el motor facial REAL (detección, alineación,
  anti-spoofing, accesorios y embeddings) con los modelos ONNX de la imagen de Docker.
- `voice_es_mx_name.wav` y `voice_en_us_date.wav` (16 kHz, mono, PCM): voces **sintetizadas** con el sintetizador del
  sistema (macOS `say`, voces Mónica y Samantha) diciendo «Ana María Ruiz Pérez» y "May fifteenth nineteen ninety". No
  son la voz de ninguna persona real (ningún dato personal). Se usan para probar el motor de voz a texto REAL
  (`faster-whisper`, modelo `small` de la imagen de Docker, `TEST_SPEECH_MODELS_DIR`) con las reglas de comparación de la
  API en español y en inglés (`tests/test_speech_real.py`).
