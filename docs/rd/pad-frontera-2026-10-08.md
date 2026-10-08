# PAD de frontera: más de 1000 rasgos de presentación, 7 familias que miden y no niegan (2026-10-08)

Decisión del dueño del producto (2026-10-07): «al menos 1000 señales de riesgo, I+D de frontera, integra lo que haga
falta». Este documento explica cómo se cumple SIN rechazar a personas reales.

## 1. El enfoque: medir de verdad, no fabricar reglas que nieguen

Inventar 1000 reglas que nieguen produciría miles de rechazos falsos: cada regla nueva, mal calibrada, deja fuera a
personas reales. En su lugar, la plataforma MIDE más de 1000 rasgos REALES y enumerables de cada captura frontal
(detección de ataques de presentación, *Presentation Attack Detection*, PAD) y los agrupa en 7 FAMILIAS. Cada familia
alimenta UNA señal del motor de riesgo.

**Invariante único y duro: lo nuevo nunca niega.** Cada señal PAD:

- nace en «Solo medir» (`OBSERVE`) en el catálogo (`catalog.risk_signals`, migración 0090);
- está en `risk_rules.CALIBRATING_SIGNALS` → `ASK_ONLY_SIGNALS`: aunque una empresa la ponga «Obligatoria» con muchos
  puntos, como mucho deja el registro «en revisión» (`risk_rules.ask_only`), nunca lo niega;
- no es una regla dura (`hard = false`): nunca niega sola por nivel crítico.

Así, hasta que el dueño calibre una familia con datos reales (§4), ninguna puede ser la única razón de un rechazo. La
prueba `test_capture_protocol.py` lo verifica: con las 7 familias disparadas y en modo «Obligatoria» con 100 puntos, la
decisión es a lo más `REVIEW`, jamás `DENY`.

## 2. La rejilla enumerable: 7 × 17 × 3 × 4 = 1428 rasgos

`app/facial_recognition/pad.py` construye el registro estable `PAD_FEATURES` como el producto determinista
`familia.región.escala.canal.estadístico`. Cada nombre es único (p. ej. `texture.r2c1.s2.Y.lbp`).

- **7 familias**, cada una una pista física real de un ataque de presentación (foto, pantalla o máscara):

  | Familia | Pista | Estadístico por región (siempre en [0, 1]) |
  |---|---|---|
  | `texture` | microtextura (una impresión o pantalla la aplana) | fracción de patrones LBP uniformes (8 vecinos, ≤ 2 transiciones) |
  | `frequency` | retícula de una pantalla fotografiada (moiré) | energía del espectro en la banda alta (radio ≥ 0.25) entre la total |
  | `color` | momento de color del canal | media del canal ÷ 255 |
  | `noise` | ruido del sensor (un rostro pegado/generado trae menos) | energía del residuo de alta frecuencia ÷ (residuo + señal) |
  | `specular` | reflejos de una pantalla o del papel | fracción de píxeles casi saturados (≥ 240) |
  | `sharpness` | nitidez fina (una reproducción la pierde) | energía del laplaciano ÷ (laplaciano + varianza) |
  | `chroma` | croma (color fuera de lo esperado en piel real) | distancia del canal a la luma ÷ 255 (en la luma, saturación media ÷ 255) |

- **17 regiones**: la rejilla de 4×4 del rostro (`r0c0`..`r3c3`) más el rostro entero (`whole`). Un ataque local
  (una máscara parcial, un reflejo en una esquina) se ve en su celda aunque el promedio del rostro lo diluya.
- **3 escalas** (`s0`=64 px, `s1`=48 px, `s2`=32 px): el rostro reducido a tres tamaños. Lo fino (microtextura, ruido)
  vive en `s0`; lo grueso (color, croma) sobrevive en `s2`.
- **4 canales**: `R`, `G`, `B` y la luma `Y`. El moiré y el ruido difieren por canal (el sensor muestrea el color en
  mosaico de Bayer); el croma se define por canal como la distancia a la luma.

Total: 7 × 17 × 3 × 4 = **1428 rasgos**, todos con nombre único. Cada rasgo está ACOTADO a [0, 1] por construcción
(fracciones y cocientes), así la familia es la media de sus rasgos, también en [0, 1], sin constantes mágicas que
calibrar. `families(vector)` hace esa agregación: 1428 rasgos → 7 números.

## 3. De los 1428 rasgos a 7 señales

1. El pipeline mide la frontal UNA vez (`FacePipeline.analyze_frontal`); de los MISMOS arreglos ya decodificados
   (nunca se decodifica dos veces) saca el recorte del rostro y llama `pad.extract([recorte])` → `pad.families(...)`:
   los 7 números de esa frontal (`FaceAnalysis.pad`). Los 1428 rasgos crudos se descartan ahí mismo.
2. Por intento, `face_signals._pad_scores` toma el PEOR caso (el máximo) de cada familia entre las frontales y lo
   guarda en `ops.face_attempt_metrics.pad` (JSONB): 7 números, nunca los rasgos crudos ni imagen alguna.
3. `capture_protocol.pad_hits` dispara la señal de una familia cuando su peor puntaje pasa el umbral máximo de la
   familia (`SecurityThresholds.pad_*`). La señal entra al motor como cualquier otra, en «Solo medir».

Los umbrales se autocalibran «solo endureciendo» (`face_security.SIGNALS`, `upper=True`): parten de su tope
(`FACE_PAD_<FAMILIA>_MAX`), bajan hacia el piso común (`FACE_PAD_TIGHTEST`) según el percentil de las personas reales,
y nunca por debajo del piso. Los intentos que la revisión de un caso confirmó como fraude no cuentan (no envenenan la
calibración). El valor de una familia se lee del JSON `pad` de los intentos exitosos
(`face_security_repository.pad_metric_column`).

## 4. Plan de calibración (cómo se promueve una familia fuera de «Solo medir»)

Igual que las señales de la fase 1b y del protocolo de captura: una familia se queda en `CALIBRATING_SIGNALS` hasta
que el dueño del producto tenga datos reales que justifiquen promoverla, y SOLO entonces, con una decisión explícita:

1. **Reunir distribución real.** Con la plataforma en producción, `ops.face_attempt_metrics.pad` acumula los 7 números
   por intento. El ADMIN los observa en «Seguridad facial» (cada familia con su umbral vigente, su piso y su tope) y,
   cuando se construya, en «Deriva» (hoy las familias PAD se excluyen del monitoreo de deriva, `drift_service
   .EXCLUDED_SIGNALS`, hasta promoverlas).
2. **Separar genuino de fraude.** Solo los intentos GENUINOS (aprobados y sin fraude confirmado) forman la línea base;
   los casos confirmados dan la distribución de ataque. Se mide, por familia, el solapamiento entre ambas.
3. **Criterio de promoción.** Una familia se promueve de `OBSERVE` a sumar puntos (o a pedir «un paso más») SOLO si en
   datos reales separa fraude de genuino con una tasa de falsos positivos aceptable al umbral elegido, y el dueño lo
   aprueba. Promoverla es: sacar su código de `CALIBRATING_SIGNALS`, ajustar sus puntos en el catálogo y añadirla al
   monitoreo de deriva. Nunca se convierte en regla dura automáticamente.
4. **Reversible.** Si tras promoverla sube la tasa de falsos positivos, se regresa a `OBSERVE` (vuelve a
   `CALIBRATING_SIGNALS`) sin tocar datos.

Hasta cerrar ese ciclo, el valor de las 7 familias es puramente observacional: ayuda al ADMIN a entender los ataques,
nunca rechaza a nadie.

## 5. Costo acotado

El trabajo es fijo y pequeño, no depende del tamaño de la imagen recibida: el recorte del rostro se reduce a 64, 48 y
32 px antes de medir. Por frontal son 3 escalas × 4 canales × 17 regiones × 7 familias = 1428 reducciones sobre
arreglos de a lo más 64×64 (las celdas de la rejilla son de 8 a 16 px), más 204 FFT diminutas (la familia de
frecuencia, una por región, canal y escala). En conjunto, decenas de milisegundos por frontal. Corre en el pool de
workers faciales (`@observed("face.pad")`), fuera de toda transacción y sin ninguna consulta nueva por intento
(`tests/test_performance.py` sigue igual). Las rutas faciales ya alertan desde 2.5 s (regla 18), con holgura de sobra.

## 6. Privacidad (regla 13)

- **Ninguna imagen ni rasgo crudo se guarda.** Se calculan los 1428 rasgos en memoria, se agregan a 7 números por
  familia y los rasgos crudos se descartan. Solo los 7 números por intento llegan a `ops.face_attempt_metrics.pad`
  (números, nunca una imagen; `tests/test_image_storage.py` sigue verde: la única columna binaria es la plantilla
  facial cifrada).
- **Nada sale del servidor.** Todo el cálculo es numpy/OpenCV en nuestro servidor; ningún servicio de terceros ve las
  capturas ni los rasgos.
- **Sin cuadros medibles, «no medible».** Si el recorte es demasiado pequeño o no es una imagen de color, `extract`
  devuelve `{}` y no se guarda nada ni se dispara ninguna señal: la ausencia de medición NUNCA es sospechosa.
