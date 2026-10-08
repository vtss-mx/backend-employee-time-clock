# Blindaje de frontera del registro facial y endurecimiento de la política (2026-10-08)

Decisión del dueño del producto (2026-10-08): mejorar la experiencia del registro facial con los mecanismos más
avanzados y «activar todas las Políticas de verificación de identidad y endurecerlas un 100% más», manteniendo todo
fluido. Este documento explica QUÉ se hizo, POR QUÉ, y los conflictos de arquitectura que se señalaron en lugar de
«rodear» (regla de la raíz: si una petición choca con una regla, se señala antes; no se rodea).

## 1. Experiencia del registro (frontera, fluida)

1. **Avance en porcentaje**: el contador de capturas del registro pasó de «Capturas válidas: 6/32» a un **porcentaje**
   («75 %»). Es más claro para la persona y no revela el número interno de fotos. Solo cambia la presentación
   (`captureDetail` en `liveFaceView.ts`); la verificación conserva «Foto 2 de 3» (son pocas).

2. **Guía por audio (liveness activa guiada por voz)**: durante el registro la app **dicta** las indicaciones con voz
   —«colócate y quédate quieto», el movimiento que pide el reto («voltea a la derecha», «mira arriba»...), «centra tu
   rostro» y «listo»—, sincronizada con cada paso del flujo. Es *active liveness* asistida: reduce el abandono y los
   intentos fallidos sin bajar la seguridad (el reto y sus movimientos siguen decididos por el servidor, en orden al
   azar). Reglas de frontera que se respetan:
   - **Síntesis en el navegador** (`window.speechSynthesis`), nunca en el servidor: no se envía audio ni texto a ningún
     tercero (regla 13 de privacidad). Centralizada en un solo módulo (`utils/speech.ts`), detectada por capacidad
     (`typeof`) y con degradación silenciosa donde no existe (Safari sin voces, navegador integrado).
   - **Sin redibujar por cuadro**: el que habla es un efecto (`useFaceSpeech`) atado a los códigos estables del paso
     (fase, movimiento, etapa), no al texto ya resuelto, así el cambio de idioma en caliente no vuelve a hablar y el
     visor mantiene sus «estados fijos» (ningún `transition`/`animation` nuevo en `.face-scan*`).
   - **Silenciable** (botón en el encabezado del escáner) y recordado por dispositivo en IndexedDB (nunca
     `localStorage`).

3. **Voces configurables por empresa**: el ADMIN elige, por empresa, la voz de la guía (catálogo `voice_profiles`:
   femenina cálida, femenina clara, masculina serena, masculina grave y la neutra del sistema) y puede probarla antes de
   guardar. La política de la empresa guarda si se dicta (`voice_guidance_enabled`, apagada por omisión) y con cuál voz
   (`voice_profile`, `FEMALE_WARM` por omisión). Es una ayuda, no un candado: es neutral (no pasa por la regla de dos
   personas) y la empresa la lee de su política. La app mapea cada código a los parámetros de la voz (género, tono,
   velocidad) y elige la mejor voz instalada para el idioma activo.

Mecanismos antifraude de frontera que YA cubren el registro y la verificación (no se re-implementan; se documentan para
contexto): ráfaga de continuidad, moiré, ruido, paralaje de los giros, reenvío perceptual (pHash + coseno), llave del
dispositivo, telemetría del navegador, y la verificación por voz y video del registro. El destello de color se RETIRÓ
(decisión 2026-10-06) y no se reactiva (ver §3).

## 2. Endurecimiento de la política de verificación («activar todas y endurecer 100%»)

El análisis de frontera confirmó que **la configuración por omisión ya está casi al máximo**: confianza en el techo del
catálogo (0.99999), bloqueo de prenda de cabeza y cubrebocas, prueba de vida, anti-spoofing, viaje imposible, bloqueo por
intentos, motor de riesgo, verificación por voz, ráfaga y evidencia de fraude, todos ENCENDIDOS por omisión. «Endurecer
un 100% más» se aplicó donde la arquitectura lo permite SIN rechazar a personas reales: subiendo cada candado y umbral
hacia su extremo estricto en los **tres niveles predefinidos** (`policy_rules.PRESETS`), que es el mecanismo que el
ADMIN aplica por empresa para activar todo de golpe. Resumen:

| Campo | Estándar (antes→ahora) | Alto (antes→ahora) | Máximo (antes→ahora) |
|---|---|---|---|
| `min_capture_quality` | 0.4 → **0.5** | 0.55 → **0.65** | 0.7 → **0.9** (tope) |
| `liveness_timeout_seconds` | 60 → **45** | 45 → **35** | 30 → **20** (mínimo) |
| `lockout_max_failures` | 5 → **4** | 4 | 3 |
| `lockout_minutes` | 15 → **30** | 30 → **120** | 60 → **1440** (tope, un día) |
| cortes de riesgo (medio/alto/crítico) | 30/60/80 → **25/55/75** | 25/50/75 → **20/45/70** | 20/40/70 → **15/35/65** |
| `anti_spoofing_level` | STANDARD | HIGH | MAXIMUM |
| `employee_device_mode` | OBSERVE | STEP_UP | APPROVAL |
| señales duras (ENFORCE) | — | REPLAY_PERCEPTUAL, **KNOWN_ATTACK** | REPLAY_PERCEPTUAL, KNOWN_ATTACK, **VALIDATOR_SIGNATURE_INVALID**, BURST_MISSING |
| presencia (firma/ubicación/sitio) | solo medir | solo medir | **obligatoria** |
| **Máximo también** | — | — | **bloqueo de lentes ON**, `verification_location`=**ENFORCE**, `qr_lifetime`=15 s, `max_location_accuracy`=10 m, `max_travel`=30 km/h |

El nivel **Máximo** queda, así, con TODOS los candados activos y en su valor más estricto: es «activar todas las
políticas y endurecerlas» en un clic del ADMIN por empresa.

## 3. Conflictos de arquitectura (señalados, NO rodeados)

1. **El destello de color está RETIRADO** (decisión del dueño 2026-10-06, migración 0080: «elimínalo por completo»).
   «Endurecer todo» NO lo reactiva: hacerlo contradice una decisión vigente y la app ya no lo pinta. Los ataques de
   presentación se cubren con la ráfaga, los movimientos, moiré/ruido/paralaje, el reenvío perceptual y la voz. Si
   algún día se reconsidera, su mecanismo sigue en el servidor y se mide siempre, solo decide con ENFORCE.

2. **«Lentes permitidos» es una decisión explícita** (2026-10-07: `block_glasses` apagado por omisión en toda empresa).
   El endurecimiento lo activa **solo dentro del nivel Máximo** (opt-in del ADMIN), sin cambiar el valor por omisión.
   Así se respeta la decisión previa y, a la vez, se ofrece el bloqueo de lentes en el nivel más estricto. Si el dueño
   quiere bloquear lentes por omisión en TODA empresa, es un cambio de una línea (el `server_default` y el
   `PolicySnapshot`) que se deja señalado, no tomado.

3. **«Lo nuevo nunca niega» (calibración primero)**: las señales de `CALIBRATING_SIGNALS` y del navegador
   (`BROWSER_SIGNALS`) pueden subir el nivel, pero la arquitectura las topa en «en revisión» (`ask_only`): una señal sin
   calibrar que negara rechazaría a personas reales de forma impredecible (su exactitud no está medida). Por eso el
   endurecimiento **NO** fuerza a ENFORCE-que-niega ninguna señal sin calibrar; solo activa las calibradas y las reglas
   duras (reenvío perceptual, ataque conocido, firma del validador). `PULSE_ABSENT` sigue siendo «solo medir» por
   diseño (422 si se intenta exigir).

4. **Los valores por omisión de una empresa nueva se mantienen usables** (no se forzó el nivel Máximo como omisión
   global). Razón: Máximo exige aprobación del dispositivo, ubicación obligatoria y calidad 0.9; como omisión global
   rechazaría a un empleado legítimo en su primer dispositivo el día uno y rompería la invariante de calibración. El
   endurecimiento máximo está a un clic (aplicar el nivel «Máximo» por empresa). **Decisión que se deja al dueño**: si
   quiere que toda empresa nueva nazca en «Máximo», se cambia la creación de la empresa para aplicar ese nivel (o los
   `server_default`/`PolicySnapshot`), asumiendo ese costo de usabilidad; se señala, no se toma.

5. **Los umbrales del motor facial no se «duplican» a ciegas**: la confianza ya está en el techo (0.99999) y los pisos
   y topes de `face_security` están calibrados contra LFW y el banco de motores reales. Duplicarlos produciría falsos
   rechazos (rechazar a la persona correcta), lo contrario a un sistema que funciona. El endurecimiento se hace con los
   candados y los niveles, no alterando la calibración medida; la autocalibración de la plataforma solo endurece dentro
   de `[piso, tope]`.

## 4. Verificación

Backend y webapp con cobertura 100 % (líneas y ramas), los siete idiomas, ortografía estricta y la matriz de
autorización/aislamiento/inyección intactas. Migración `0088` (catálogo `voice_profiles` + columnas
`voice_guidance_enabled` y `voice_profile`). Los cambios de los niveles predefinidos no tocan la base (son datos del
código) y se verifican en `tests/test_policy_governance.py`.
