"""Textos de los catálogos con el estilo «simple pero profesional»

Decisión del dueño del producto: todo mensaje se lee de un vistazo (`backend-employee-time-clock/AGENTS.md` §11.5 y
`webapp-employee-time-clock/AGENTS.md` §7.6). Los textos de los catálogos que la app muestra (motivos de una
verificación, errores del rostro, frases e instrucciones de la cámara, descripciones) se acortan y pierden el relleno
(«No fue posible», «No pudimos», «nuevamente», «inténtalo de nuevo», "We couldn't", "Please try again"), en los dos
idiomas: el español en la columna de cada catálogo y el inglés en `catalog.translations` (§11.3). No cambian códigos,
`{marcadores}`, el largo de las columnas ni el sentido de ninguna regla.

Cada texto lleva aquí su versión anterior y la nueva (no se leen del seed: una migración posterior que vuelva a
cambiar el mismo texto no altera lo que esta aplica), así que `downgrade` deja exactamente lo que había.

Revision ID: 0069
Revises: 0068
Create Date: 2026-10-05 23:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0069"
down_revision: str | None = "0068"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CATALOG = "catalog"
LOCALE = "en-US"

#: (catálogo, código, columna) → ((español anterior, español nuevo), (inglés anterior, inglés nuevo)). Un idioma que no
#: cambia lleva el mismo texto en los dos lados (no se toca su fila).
TEXTS: dict[tuple[str, str, str], tuple[tuple[str, str], tuple[str, str]]] = {
    ("antispoof_levels", "MAXIMUM", "description"): (
        ("Basta con que una sola captura parezca una foto o una pantalla para rechazar. Exige buena luz y una cámara de calidad.", "Rechaza si una sola captura parece una foto o una pantalla. Exige buena luz y una cámara de calidad."),
        ("A single capture that looks like a photo or a screen is enough to reject. Requires good lighting and a quality camera.", "Rejects if a single capture looks like a photo or a screen. Requires good lighting and a quality camera."),
    ),
    ("antispoof_levels", "STANDARD", "description"): (
        ("Rechaza cuando la mayoría de las capturas parecen una foto, una pantalla o un video. Recomendado para la mayoría de las cámaras.", "Rechaza cuando la mayoría de las capturas parecen una foto, una pantalla o un video. Recomendado para casi todas las cámaras."),
        ("Rejects when most of the captures look like a photo, a screen, or a video. Recommended for most cameras.", "Rejects when most of the captures look like a photo, a screen, or a video. Recommended for most cameras."),
    ),
    ("assignment_states", "ENDED", "description"): (
        ("Ya no rige: lo registrado con él lo conserva.", "Ya no rige; lo registrado con él no cambia."),
        ("No longer in effect: what was recorded with it keeps it.", "No longer in effect; what was recorded under it doesn't change."),
    ),
    ("attendance_review_statuses", "REJECTED", "description"): (
        ("La empresa rechazó el registro: no lo hizo la persona o no fue en el lugar debido.", "La empresa rechazó el registro: no lo hizo la persona o no fue en el lugar debido."),
        ("The company rejected the record: the person didn't make it or it wasn't at the right place.", "The company rejected the record: it wasn't made by the employee or at the right place."),
    ),
    ("board_states", "MISSED_CHECKOUT", "description"): (
        ("Venció el límite para checar su salida sin hacerlo.", "No checó su salida antes del límite."),
        ("The deadline to check out passed without checking out.", "Didn't check out before the deadline."),
    ),
    ("day_off_types", "OTHER", "description"): (
        ("Otro día libre que autoriza la empresa (la registra la empresa).", "Otro día libre que autoriza y registra la empresa."),
        ("Another day off authorized by the company (recorded by the company).", "Another day off authorized and recorded by the company."),
    ),
    ("day_off_types", "PERMISSION", "description"): (
        ("Permiso para faltar con autorización de la empresa (lo puede pedir desde Mi asistencia).", "Ausencia autorizada por la empresa; el empleado puede pedirla desde Mi asistencia."),
        ("Leave from work authorized by the company (they can request it from My attendance).", "Time off authorized by the company; the employee can request it from My attendance."),
    ),
    ("day_off_types", "VACATION", "description"): (
        ("Días de vacaciones del empleado (los puede pedir desde Mi asistencia).", "El empleado también puede pedirlas desde Mi asistencia."),
        ("The employee's vacation days (they can request them from My attendance).", "The employee can also request them from My attendance."),
    ),
    ("enrollment_rejection_reasons", "PERSON_MISMATCH", "name"): (
        ("La persona de la foto no corresponde al empleado", "La persona de la foto no es el empleado"),
        ("The person in the photo isn't the employee", "The person in the photo isn't the employee"),
    ),
    ("enrollment_rejection_reasons", "SPOOFING", "name"): (
        ("Se detecta suplantación (foto de foto o pantalla)", "Suplantación detectada (foto de una foto o de una pantalla)"),
        ("Spoofing detected (photo of a photo or a screen)", "Spoofing detected (photo of a photo or a screen)"),
    ),
    ("face_errors", "CHALLENGE_INVALID", "message"): (
        ("El reto de verificación expiró o no es válido. Inténtalo de nuevo", "El reto de verificación venció o no es válido. Intenta de nuevo."),
        ("The verification challenge expired or isn't valid. Try again", "The verification challenge expired or isn't valid. Try again."),
    ),
    ("face_errors", "CHALLENGE_TOO_FAST", "message"): (
        ("El giro se capturó demasiado rápido. Sigue la indicación en pantalla e inténtalo de nuevo.", "El giro se capturó demasiado rápido. Sigue la indicación en pantalla e intenta de nuevo."),
        ("The turn was captured too quickly. Follow the on-screen instruction and try again.", "The turn was captured too quickly. Follow the on-screen instruction and try again."),
    ),
    ("face_errors", "ENROLL_INCONSISTENT", "message"): (
        ("Las fotografías no son consistentes entre sí. Repite la captura con una sola persona, de frente y bien iluminada", "Las fotos no coinciden entre sí. Repite la captura con una sola persona, de frente y con buena luz."),
        ("The photos aren't consistent with each other. Repeat the capture with only one person, facing forward and well lit", "The photos don't match each other. Repeat the capture with only one person, facing forward, in good lighting."),
    ),
    ("face_errors", "FACE_ALREADY_REGISTERED", "message"): (
        ("Este rostro ya está registrado como otro empleado de la empresa.", "Este rostro ya está registrado como otro empleado de la empresa"),
        ("This face is already enrolled as another employee of the company.", "This face is already enrolled as another employee of the company"),
    ),
    ("face_errors", "FACE_CUT_OFF", "message"): (
        ("El rostro está incompleto. Céntralo en la cámara", "El rostro está incompleto. Céntralo en la cámara."),
        ("Your face is cut off. Center it in the camera", "Your face is cut off. Center it in the camera."),
    ),
    ("face_errors", "FACE_LOCKED", "message"): (
        ("Demasiados intentos fallidos. Por seguridad, espera {minutes} min para volver a intentarlo.", "Demasiados intentos fallidos. Por seguridad, espera {minutes} min e intenta de nuevo."),
        ("Too many failed attempts. For security, wait {minutes} min before trying again.", "Too many failed attempts. For security, wait {minutes} min and try again."),
    ),
    ("face_errors", "FACE_NOT_REGISTERED", "message"): (
        ("No tienes información facial registrada. Contacta a tu empresa.", "No tienes registro facial. Contacta a tu empresa."),
        ("You don't have any face data enrolled. Contact your company.", "You don't have a face enrollment. Contact your company."),
    ),
    ("face_errors", "FACE_PROCESSING_ERROR", "message"): (
        ("No fue posible procesar el rostro en este momento. Intenta nuevamente.", "No se pudo procesar el rostro en este momento. Intenta de nuevo."),
        ("The face couldn't be processed right now. Please try again.", "Couldn't process the face right now. Try again."),
    ),
    ("face_errors", "FACE_SERVICE_BUSY", "message"): (
        ("Hay muchas solicitudes de reconocimiento en este momento. Intenta en unos segundos.", "Hay muchas solicitudes de reconocimiento en este momento. Intenta de nuevo en unos segundos."),
        ("There are many recognition requests right now. Try again in a few seconds.", "There are many recognition requests right now. Try again in a few seconds."),
    ),
    ("face_errors", "FACE_TOO_SMALL", "message"): (
        ("El rostro está muy lejos. Acércate a la cámara", "El rostro está muy lejos. Acércate a la cámara."),
        ("Your face is too far away. Move closer to the camera", "Your face is too far away. Move closer to the camera."),
    ),
    ("face_errors", "FLASH_FLAT", "message"): (
        ("No pudimos confirmar que estás frente a la pantalla. Mira de frente a la cámara, sin filtros ni otra cámara, e inténtalo de nuevo", "No se pudo confirmar que estés frente a la pantalla. Mira de frente a la cámara, sin filtros ni otra cámara, e intenta de nuevo."),
        ("We couldn't confirm that you're in front of the screen. Look straight at the camera, without filters or another camera, and try again", "Couldn't confirm that you're in front of the screen. Look straight at the camera, without filters or another camera, and try again."),
    ),
    ("face_errors", "FLASH_MISMATCH", "message"): (
        ("No pudimos confirmar que estás frente a la pantalla. Mira de frente a la cámara, sin filtros ni otra cámara, e inténtalo de nuevo", "No se pudo confirmar que estés frente a la pantalla. Mira de frente a la cámara, sin filtros ni otra cámara, e intenta de nuevo."),
        ("We couldn't confirm that you're in front of the screen. Look straight at the camera, without filters or another camera, and try again", "Couldn't confirm that you're in front of the screen. Look straight at the camera, without filters or another camera, and try again."),
    ),
    ("face_errors", "INVALID_IMAGE", "message"): (
        ("No se pudo procesar la imagen. Toma otra foto", "No se pudo procesar la imagen. Toma otra foto."),
        ("The image couldn't be processed. Take another photo", "Couldn't process the image. Take another photo."),
    ),
    ("face_errors", "KNOWN_ATTACK", "message"): (
        ("No pudimos confirmar tu registro. Intenta de nuevo con buena luz, frente a la pantalla.", "No se pudo confirmar tu identidad. Intenta de nuevo con buena luz, frente a la pantalla."),
        ("We couldn't confirm your record. Try again in good lighting, facing the screen.", "Couldn't confirm your identity. Try again in good lighting, facing the screen."),
    ),
    ("face_errors", "LIVENESS_FAILED", "message"): (
        ("No se detectó el movimiento solicitado. Inténtalo de nuevo", "No se detectó el movimiento solicitado. Intenta de nuevo."),
        ("The requested movement wasn't detected. Try again", "The requested movement wasn't detected. Try again."),
    ),
    ("face_errors", "LIVENESS_MISMATCH", "message"): (
        ("Las capturas no corresponden a la misma persona. Inténtalo de nuevo", "Las capturas no corresponden a la misma persona. Intenta de nuevo."),
        ("The captures don't belong to the same person. Try again", "The captures don't belong to the same person. Try again."),
    ),
    ("face_errors", "LIVENESS_REQUIRED", "message"): (
        ("Se requiere completar la prueba de vida", "Completa la prueba de vida para continuar"),
        ("You need to complete the liveness check", "Complete the liveness check to continue"),
    ),
    ("face_errors", "LOW_DETECTION_SCORE", "message"): (
        ("El rostro no se distingue con claridad. Mira de frente a la cámara", "El rostro no se distingue con claridad. Mira de frente a la cámara."),
        ("The face can't be seen clearly. Look straight at the camera", "Your face isn't clearly visible. Look straight at the camera."),
    ),
    ("face_errors", "LOW_QUALITY", "message"): (
        ("La foto no tiene la calidad suficiente. Busca buena luz, limpia la cámara y mantente quieto", "La foto no tiene la calidad suficiente. Busca buena luz, limpia la cámara y mantente quieto."),
        ("The photo isn't good enough. Find good lighting, clean the camera, and hold still", "The photo isn't good enough. Find good lighting, clean the camera, and hold still."),
    ),
    ("face_errors", "MULTIPLE_FACES", "message"): (
        ("Se detectó más de una persona. Solo debe aparecer un rostro", "Se detectó más de una persona. Solo debe aparecer un rostro."),
        ("More than one person was detected. Only one face should appear", "More than one person was detected. Only one face should appear."),
    ),
    ("face_errors", "REPLAY_DETECTED", "message"): (
        ("Esta captura ya se había usado antes. Toma una captura nueva frente a la cámara.", "Esta captura ya se usó. Toma una nueva frente a la cámara."),
        ("This capture was already used. Take a new capture in front of the camera.", "This capture was already used. Take a new one in front of the camera."),
    ),
    ("face_errors", "REPLAY_PERCEPTUAL", "message"): (
        ("Esta captura ya se había usado antes. Toma una captura nueva frente a la cámara.", "Esta captura ya se usó. Toma una nueva frente a la cámara."),
        ("This capture was already used. Take a new capture in front of the camera.", "This capture was already used. Take a new one in front of the camera."),
    ),
    ("face_errors", "RISK_DENIED", "message"): (
        ("No pudimos confirmar tu registro. Intenta de nuevo con buena luz, frente a la pantalla.", "No se pudo confirmar tu identidad. Intenta de nuevo con buena luz, frente a la pantalla."),
        ("We couldn't confirm your record. Try again in good lighting, facing the screen.", "Couldn't confirm your identity. Try again in good lighting, facing the screen."),
    ),
    ("face_errors", "SPOOF_DETECTED", "message"): (
        ("No pudimos confirmar que eres una persona frente a la cámara. Evita reflejos y pantallas, y usa tu rostro real (no una foto o video)", "No se pudo confirmar que seas una persona frente a la cámara. Evita reflejos y pantallas, y usa tu rostro real (no una foto o video)."),
        ("We couldn't confirm that you're a person in front of the camera. Avoid reflections and screens, and use your real face (not a photo or video)", "Couldn't confirm that you're a person in front of the camera. Avoid reflections and screens, and use your real face (not a photo or video)."),
    ),
    ("face_errors", "STATIC_CAPTURE", "message"): (
        ("Las capturas son idénticas, como si fueran una foto fija. Colócate frente a la cámara e inténtalo de nuevo.", "Las capturas son idénticas, como una foto fija. Colócate frente a la cámara e intenta de nuevo."),
        ("The captures are identical, as if they were a still photo. Stand in front of the camera and try again.", "The captures are identical, like a still photo. Stand in front of the camera and try again."),
    ),
    ("face_errors", "STEP_UP_REQUIRED", "message"): (
        ("Por seguridad, completa un paso más: sigue las indicaciones en pantalla.", "Por seguridad, completa un paso más: sigue las indicaciones en pantalla"),
        ("For security, complete one more step: follow the on-screen instructions.", "For security, complete one more step: follow the on-screen instructions"),
    ),
    ("face_errors", "TOO_BLURRY", "message"): (
        ("La imagen está borrosa. Mantente quieto", "La imagen está borrosa. Mantente quieto."),
        ("The image is blurry. Hold still", "The image is blurry. Hold still."),
    ),
    ("face_errors", "TOO_BRIGHT", "message"): (
        ("La imagen está sobreexpuesta. Evita luz directa", "La imagen está sobreexpuesta. Evita la luz directa."),
        ("The image is overexposed. Avoid direct light", "The image is overexposed. Avoid direct light."),
    ),
    ("face_errors", "TOO_DARK", "message"): (
        ("La imagen está muy oscura. Busca mejor iluminación", "La imagen está muy oscura. Busca mejor iluminación."),
        ("The image is too dark. Find better lighting", "The image is too dark. Find better lighting."),
    ),
    ("face_statuses", "APPROVED", "description"): (
        ("Identidad validada. Puede identificarse con su rostro o su código QR.", "Puede identificarse con su rostro o su código QR."),
        ("Identity validated. They can identify with their face or their QR code.", "They can identify themselves with their face or QR code."),
    ),
    ("face_statuses", "NOT_ENROLLED", "description"): (
        ("El empleado aún no registra su rostro. Se le pedirá en su próximo inicio de sesión.", "El empleado aún no registra su rostro. Se le pedirá en su próximo inicio de sesión."),
        ("The employee hasn't enrolled their face yet. They'll be asked to the next time they sign in.", "The employee hasn't enrolled their face yet. They'll be asked to enroll at their next sign-in."),
    ),
    ("face_statuses", "REJECTED", "description"): (
        ("El registro fue rechazado; el empleado deberá registrarse de nuevo.", "El empleado deberá registrar su rostro de nuevo."),
        ("The enrollment was rejected; the employee must enroll again.", "The employee must enroll their face again."),
    ),
    ("flash_modes", "ENFORCE", "description"): (
        ("El rostro debe reflejar los colores que pinta la pantalla: un video inyectado o generado no los ve. Con luz del sol directa puede pedir repetir. Actívalo después de calibrar.", "El rostro debe reflejar los colores de la pantalla: un video inyectado o generado no los refleja. Con luz del sol directa puede pedir repetir. Actívalo después de calibrar."),
        ("The face must reflect the colors the screen paints: an injected or generated video doesn't see them. In direct sunlight it may ask to try again. Turn it on after calibrating.", "The face must reflect the screen's colors: an injected or generated video doesn't. In direct sunlight it may ask to try again. Turn it on after calibrating."),
    ),
    ("fraud_case_statuses", "OPEN", "description"): (
        ("Nadie lo ha revisado todavía.", "Sin revisar todavía."),
        ("No one has reviewed it yet.", "Not reviewed yet."),
    ),
    ("fraud_kinds", "BUDDY_PUNCHING", "description"): (
        ("Una persona registra por otra: cuenta compartida, QR reenviado o títere remoto.", "Una persona checa por otra: cuenta compartida, QR reenviado o títere remoto."),
        ("One person checks in for another: a shared account, a forwarded QR, or a remote puppet.", "One person checks in for another: a shared account, a forwarded QR, or a remote puppet."),
    ),
    ("fraud_kinds", "INTERNAL", "description"): (
        ("Abuso desde la empresa o un validador: registros aprobados con el rostro de otra persona o dispositivos no autorizados.", "Abuso desde la empresa o un validador: registros faciales aprobados con el rostro de otra persona o dispositivos no autorizados."),
        ("Abuse from the company or a validator: enrollments approved with another person's face or unauthorized devices.", "Abuse from the company or a validator: enrollments approved with another person's face or unauthorized devices."),
    ),
    ("fraud_kinds", "LOCATION", "description"): (
        ("Ubicación simulada o poco creíble para registrar desde otro lugar.", "Ubicación simulada o poco creíble para checar desde otro lugar."),
        ("A simulated or implausible location to check in from somewhere else.", "A simulated or implausible location to check in from somewhere else."),
    ),
    ("menu_modules", "ATTENDANCE", "description"): (
        ("El día a día: tablero, turnos, calendario de días libres y sitios donde se checa.", "Tablero, turnos, calendario de días libres y sitios."),
        ("Day to day: board, shifts, days-off calendar, and the sites where people check in.", "Board, shifts, days-off calendar, and sites."),
    ),
    ("menu_modules", "DATA", "description"): (
        ("Conexión de la información de la empresa con sus otros sistemas (nómina, ERP).", "Conexión con otros sistemas de la empresa (nómina, ERP)."),
        ("Connecting the company's information with its other systems (payroll, ERP).", "Connections to the company's other systems (payroll, ERP)."),
    ),
    ("policy_change_statuses", "PENDING", "description"): (
        ("Relaja la seguridad: espera la aprobación de otro ADMIN.", "Relaja la seguridad: espera la aprobación de otro ADMIN."),
        ("It relaxes security: it's waiting for another ADMIN's approval.", "It lowers security: it's waiting for another ADMIN's approval."),
    ),
    ("price_periods", "MONTH", "description"): (
        ("El precio es de un mes: cada día cuesta la parte que le toca de su mes.", "El precio es de un mes; cada día cuesta su parte proporcional."),
        ("The price is for one month: each day costs its share of its month.", "The price is for one month; each day costs its prorated share."),
    ),
    ("price_periods", "YEAR", "description"): (
        ("El precio es de un año: cada día cuesta la parte que le toca de su año.", "El precio es de un año; cada día cuesta su parte proporcional."),
        ("The price is for one year: each day costs its share of its year.", "The price is for one year; each day costs its prorated share."),
    ),
    ("reverification_reasons", "FREQUENT_FAILURES", "name"): (
        ("Fallas frecuentes al identificarse", "Fallas frecuentes al identificarse"),
        ("Frequent failures when identifying", "Frequent identification failures"),
    ),
    ("review_reasons", "NETWORK", "description"): (
        ("La conexión venía de un centro de datos, de otro país o cambió de forma repentina.", "La conexión venía de un centro de datos o de otro país, o cambió de repente."),
        ("The connection came from a data center or another country, or it changed suddenly.", "The connection came from a data center or another country, or it changed suddenly."),
    ),
    ("risk_actions", "REVIEW", "description"): (
        ("Se registra, pero queda pendiente de que la empresa lo confirme o lo rechace; se abre un caso.", "Se registra, pero la empresa debe confirmarlo o rechazarlo; se abre un caso."),
        ("It's recorded, but the company still has to confirm or reject it; a case is opened.", "It's recorded, but the company must confirm or reject it; a case is opened."),
    ),
    ("risk_signals", "BURST_MISSING", "description"): (
        ("La captura no trajo la ráfaga de recortes que la app siempre envía, o llegó dañada. Suele indicar un programa que llama a la API.", "Falta la ráfaga de recortes que la aplicación siempre envía, o llegó dañada. Suele indicar un programa que llama a la API."),
        ("The capture didn't include the burst of crops the app always sends, or it arrived damaged. It usually points to a program calling the API.", "The burst of crops the app always sends is missing or damaged. It usually points to a program calling the API."),
    ),
    ("risk_signals", "COMPANY_UNDER_ATTACK", "description"): (
        ("La empresa acumula intentos sospechosos recientes (refuerzo automático).", "La empresa acumula intentos sospechosos recientes (refuerzo automático)."),
        ("The company is piling up recent suspicious attempts (automatic reinforcement).", "The company has accumulated recent suspicious attempts (automatic reinforcement)."),
    ),
    ("risk_signals", "DEVICE_KEY_MISSING", "description"): (
        ("El intento no trajo la firma de la llave del dispositivo o no se pudo verificar: la aplicación siempre la manda; un programa o una ventana privada, no.", "Falta la firma de la llave del dispositivo o no se pudo verificar: la aplicación siempre la envía; un programa o una ventana privada, no."),
        ("The attempt didn't include the device key signature or it couldn't be verified: the app always sends it; a program or a private window doesn't.", "The device key signature is missing or couldn't be verified: the app always sends it; a program or a private window doesn't."),
    ),
    ("risk_signals", "FLASH_PACE_MISMATCH", "description"): (
        ("Las capturas del destello no coinciden con las que se enviaron en vivo, o su comprobante no es válido. Solo una app alterada lo produce.", "Las capturas del destello no coinciden con las que se enviaron en vivo, o su comprobante no es válido. Solo una aplicación alterada lo produce."),
        ("The flash captures don't match the ones sent live, or their receipt isn't valid. Only a tampered app produces this.", "The flash captures don't match the ones sent live, or their receipt isn't valid. Only a tampered app produces this."),
    ),
    ("risk_signals", "FLASH_UNPACED", "description"): (
        ("Los colores del destello no se dictaron en vivo, así que pudieron prepararse con calma. También pasa en redes que bloquean el canal en vivo.", "Los colores del destello no se dictaron en vivo, así que pudieron prepararse de antemano. También pasa en redes que bloquean el canal en vivo."),
        ("The flash colors weren't paced live, so they could be prepared in advance. It also happens on networks that block the live channel.", "The flash colors weren't paced live, so they could be prepared in advance. It also happens on networks that block the live channel."),
    ),
    ("risk_signals", "IDENTITY_MISMATCH", "description"): (
        ("El rostro se parece a otro empleado de la empresa tanto o más que al que se verificó (1:N en cada verificación): registro con el rostro de otra persona o suplantación.", "El rostro se parece al menos tanto a otro empleado de la empresa como al verificado (1:N en cada verificación): registro con el rostro de otra persona o suplantación."),
        ("The face looks like another employee of the company as much as or more than the one being verified (1:N on every verification): an enrollment with someone else's face or impersonation.", "The face looks at least as much like another employee at the company as like the verified one (1:N on every verification): an enrollment with someone else's face, or impersonation."),
    ),
    ("risk_signals", "NETWORK_COUNTRY_MISMATCH", "description"): (
        ("La IP es de un país distinto al del sitio donde se checó: ubicación simulada o VPN.", "La IP es de un país distinto al del sitio donde se checó: ubicación simulada o VPN."),
        ("The IP is from a country other than the site where the check-in happened: simulated location or VPN.", "The IP is from a different country than the check-in site: simulated location or VPN."),
    ),
    ("risk_signals", "NETWORK_JUMP", "description"): (
        ("La IP cambió de país o de red respecto del registro anterior en menos tiempo del creíble.", "La IP cambió de país o de red desde el registro anterior, más rápido de lo creíble."),
        ("The IP changed country or network since the previous record in less time than is plausible.", "The IP changed country or network since the previous record, faster than is plausible."),
    ),
    ("risk_signals", "TELEMETRY_MISSING", "description"): (
        ("La captura no trajo la telemetría que la aplicación siempre manda (o llegó mal formada): típico de un programa que llama a la API.", "Falta la telemetría que la aplicación siempre envía, o llegó mal formada: típico de un programa que llama a la API."),
        ("The capture didn't include the telemetry the app always sends (or it was malformed): typical of a program calling the API.", "The telemetry the app always sends is missing or malformed: typical of a program calling the API."),
    ),
    ("risk_signals", "TRACK_INCONSISTENT", "description"): (
        ("La cámara informó una resolución o cuadros por segundo fuera de lo que dice poder, o no tiene identificador.", "La cámara informó una resolución o cuadros por segundo fuera de lo que dice admitir, o no tiene identificador."),
        ("The camera reported a resolution or frame rate beyond what it says it supports, or it has no identifier.", "The camera reported a resolution or frame rate beyond what it says it supports, or it has no identifier."),
    ),
    ("risk_signals", "VIRTUAL_CAMERA_PRESENT", "description"): (
        ("El dispositivo tiene una cámara virtual (OBS, ManyCam...) aunque la captura no la haya usado.", "El dispositivo tiene una cámara virtual (OBS, ManyCam…) aunque la captura no la haya usado."),
        ("The device has a virtual camera (OBS, ManyCam...) even if the capture didn't use it.", "The device has a virtual camera (OBS, ManyCam…) even if the capture didn't use it."),
    ),
    ("risk_tiers", "LOW", "description"): (
        ("Sin señales que pesen: se permite.", "Sin señales que pesen: se permite."),
        ("No signals that carry weight: it's allowed.", "No significant signals: it's allowed."),
    ),
    ("screens", "ADMIN_COMPANIES", "description"): (
        ("Alta, consulta, edición, activación y eliminación de empresas y sus administradores.", "Alta y administración de empresas y sus administradores."),
        ("Add, view, edit, activate, and delete companies and their administrators.", "Add and manage companies and their administrators."),
    ),
    ("screens", "ADMIN_ERRORS", "description"): (
        ("Errores registrados en el backend (cualquiera, hasta el más pequeño) y su seguimiento.", "Fallas del servidor y de la aplicación web, y su seguimiento."),
        ("Errors recorded in the backend (any of them, down to the smallest) and their follow-up.", "Server and web app failures and their follow-up."),
    ),
    ("screens", "ADMIN_FACE_SECURITY", "description"): (
        ("Lo que la plataforma ajustó sola para endurecer la prueba de vida, las empresas bajo ataque y lo medido del destello.", "Ajustes automáticos de la prueba de vida, empresas bajo ataque y mediciones del destello."),
        ("What the platform adjusted on its own to harden the liveness check, the companies under attack, and the flash measurements.", "Automatic liveness check adjustments, companies under attack, and flash measurements."),
    ),
    ("screens", "ADMIN_FRAUD_CASES", "description"): (
        ("Intentos sospechosos y de riesgo alto: revisar la evidencia y confirmar o descartar el fraude.", "Revisión de intentos sospechosos o de riesgo alto con su evidencia."),
        ("Suspicious and high-risk attempts: review the evidence and confirm or dismiss the fraud.", "Review of suspicious or high-risk attempts with their evidence."),
    ),
    ("screens", "ADMIN_PERFORMANCE", "description"): (
        ("Tiempos de respuesta de las APIs y de las funciones clave, la base de datos, las pantallas de la aplicación web y las alertas de peticiones lentas.", "Tiempos de respuesta de las APIs, funciones clave, base de datos y pantallas; alertas de peticiones lentas."),
        ("Response times of the APIs and key functions, the database, the web app's screens, and slow request alerts.", "Response times for APIs, key functions, the database, and screens; slow request alerts."),
    ),
    ("screens", "ADMIN_USAGE", "description"): (
        ("Peticiones, datos enviados y recibidos, tiempo de proceso, errores y almacenamiento de cada empresa y de cada usuario.", "Peticiones, datos, tiempo de proceso, errores y almacenamiento por empresa y por usuario."),
        ("Requests, data sent and received, processing time, errors, and storage for each company and each user.", "Requests, data, processing time, errors, and storage per company and per user."),
    ),
    ("screens", "COMPANY_API", "description"): (
        ("Llaves de la API para conectar los sistemas de la empresa (nómina, ERP) con su información.", "Llaves de la API para conectar otros sistemas (nómina, ERP)."),
        ("API keys to connect the company's systems (payroll, ERP) with its information.", "API keys to connect other systems (payroll, ERP)."),
    ),
    ("screens", "COMPANY_CALENDAR", "description"): (
        ("Días festivos, vacaciones, permisos e incapacidades: qué días no se trabaja (y quién sí).", "Días festivos, vacaciones, permisos e incapacidades."),
        ("Holidays, vacations, leaves, and sick leaves: which days aren't worked (and who works anyway).", "Holidays, vacations, leaves, and sick leaves."),
    ),
    ("screens", "COMPANY_SHIFTS", "description"): (
        ("Turnos de trabajo, su asignación a uno o varios empleados y las solicitudes de cambio de turno.", "Turnos, su asignación a los empleados y las solicitudes de cambio."),
        ("Work shifts, their assignment to one or more employees, and shift change requests.", "Shifts, their assignment to employees, and change requests."),
    ),
    ("screens", "EMPLOYEE_ATTENDANCE", "description"): (
        ("Entrada, descansos y salida de su turno (con su rostro y su ubicación) y sus vacaciones o permisos.", "Entrada, descansos y salida con rostro y ubicación; vacaciones y permisos."),
        ("Check-in, breaks, and check-out for their shift (with their face and location) and their vacations or leaves.", "Check-in, breaks, and check-out with face and location; vacation and leave."),
    ),
    ("session_revocation_reasons", "ACCOUNT_DEACTIVATED", "message"): (
        ("Tu sesión ya no es válida. Inicia sesión nuevamente.", "Tu sesión ya no es válida. Inicia sesión de nuevo."),
        ("Your session is no longer valid. Please sign in again.", "Your session is no longer valid. Sign in again."),
    ),
    ("session_revocation_reasons", "COMPANY_DEACTIVATED", "message"): (
        ("Tu sesión ya no es válida. Inicia sesión nuevamente.", "Tu sesión ya no es válida. Inicia sesión de nuevo."),
        ("Your session is no longer valid. Please sign in again.", "Your session is no longer valid. Sign in again."),
    ),
    ("session_revocation_reasons", "COMPANY_SUSPENDED", "message"): (
        ("Tu empresa está suspendida en la plataforma. Contacta al administrador de la plataforma para reactivarla.", "Tu empresa está suspendida. Contacta al administrador de la plataforma para reactivarla."),
        ("Your company is suspended on the platform. Contact the platform administrator to reactivate it.", "Your company is suspended. Contact the platform administrator to reactivate it."),
    ),
    ("session_revocation_reasons", "DEVICE_REVOKED", "message"): (
        ("La empresa retiró la autorización de este dispositivo. Pide que lo autoricen de nuevo para operar.", "La empresa retiró la autorización de este dispositivo. Pide que lo autoricen de nuevo."),
        ("Your company withdrew its approval of this device. Ask them to approve it again to operate.", "Your company withdrew its approval of this device. Ask them to approve it again."),
    ),
    ("session_revocation_reasons", "EMPLOYEE_REMOVED", "message"): (
        ("Tu sesión ya no es válida. Inicia sesión nuevamente.", "Tu sesión ya no es válida. Inicia sesión de nuevo."),
        ("Your session is no longer valid. Please sign in again.", "Your session is no longer valid. Sign in again."),
    ),
    ("session_revocation_reasons", "LOGOUT_ALL", "message"): (
        ("Tu sesión ya no es válida. Inicia sesión nuevamente.", "Tu sesión ya no es válida. Inicia sesión de nuevo."),
        ("Your session is no longer valid. Please sign in again.", "Your session is no longer valid. Sign in again."),
    ),
    ("session_revocation_reasons", "LOGOUT", "message"): (
        ("Tu sesión ya no es válida. Inicia sesión nuevamente.", "Tu sesión ya no es válida. Inicia sesión de nuevo."),
        ("Your session is no longer valid. Please sign in again.", "Your session is no longer valid. Sign in again."),
    ),
    ("session_revocation_reasons", "PASSWORD_CHANGED", "message"): (
        ("Tu sesión ya no es válida. Inicia sesión nuevamente.", "Tu sesión ya no es válida. Inicia sesión de nuevo."),
        ("Your session is no longer valid. Please sign in again.", "Your session is no longer valid. Sign in again."),
    ),
    ("session_revocation_reasons", "PASSWORD_RESET", "message"): (
        ("Tu sesión ya no es válida. Inicia sesión nuevamente.", "Tu sesión ya no es válida. Inicia sesión de nuevo."),
        ("Your session is no longer valid. Please sign in again.", "Your session is no longer valid. Sign in again."),
    ),
    ("session_revocation_reasons", "REFRESH_REUSE_DETECTED", "message"): (
        ("Tu sesión ya no es válida. Inicia sesión nuevamente.", "Tu sesión ya no es válida. Inicia sesión de nuevo."),
        ("Your session is no longer valid. Please sign in again.", "Your session is no longer valid. Sign in again."),
    ),
    ("session_revocation_reasons", "REVOKED_BY_USER", "message"): (
        ("Tu sesión ya no es válida. Inicia sesión nuevamente.", "Tu sesión ya no es válida. Inicia sesión de nuevo."),
        ("Your session is no longer valid. Please sign in again.", "Your session is no longer valid. Sign in again."),
    ),
    ("session_revocation_reasons", "SIGNED_IN_ELSEWHERE", "message"): (
        ("Se inició sesión con tu cuenta en otro dispositivo. Por seguridad, solo puedes tener una sesión activa a la vez.", "Se inició sesión con tu cuenta en otro dispositivo. Solo puedes tener una sesión activa a la vez."),
        ("Someone signed in with your account on another device. For security, you can only have one active session at a time.", "Someone signed in with your account on another device. You can only have one active session at a time."),
    ),
    ("shift_request_statuses", "CANCELLED", "description"): (
        ("Se retiró y ya no aplica (el empleado antes de que se decidiera, o la empresa).", "La retiró el empleado (antes de la decisión) o la empresa."),
        ("It was withdrawn and no longer applies (by the employee before a decision, or by the company).", "Withdrawn by the employee (before a decision) or by the company."),
    ),
    ("verification_reasons", "ALREADY_USED", "message"): (
        ("Este código QR ya se usó y no vuelve a servir. Pide al empleado que muestre el código nuevo de su teléfono.", "Este código QR ya se usó. Pide al empleado que muestre el código nuevo de su teléfono."),
        ("This QR code was already used and won't work again. Ask the employee to show the new code on their phone.", "This QR code was already used. Ask the employee to show the new code on their phone."),
    ),
    ("verification_reasons", "AMBIGUOUS_MATCH", "message"): (
        ("No fue posible distinguir a la persona con suficiente certeza. Intenta de nuevo con buena luz", "No se pudo distinguir a la persona con suficiente certeza. Intenta de nuevo con buena luz."),
        ("We couldn't tell who the person is with enough certainty. Try again in good lighting", "Couldn't tell who the person is with enough certainty. Try again in good lighting."),
    ),
    ("verification_reasons", "CHALLENGE_TOO_FAST", "message"): (
        ("El giro se capturó demasiado rápido. Sigue la indicación en pantalla e inténtalo de nuevo.", "El giro se capturó demasiado rápido. Sigue la indicación en pantalla e intenta de nuevo."),
        ("The turn was captured too quickly. Follow the on-screen instruction and try again.", "The turn was captured too quickly. Follow the on-screen instruction and try again."),
    ),
    ("verification_reasons", "FLASH_FLAT", "message"): (
        ("No pudimos confirmar que estás frente a la pantalla. Inténtalo de nuevo mirando de frente a la cámara", "No se pudo confirmar que estés frente a la pantalla. Mira de frente a la cámara e intenta de nuevo."),
        ("We couldn't confirm that you're in front of the screen. Try again looking straight at the camera", "Couldn't confirm that you're in front of the screen. Look straight at the camera and try again."),
    ),
    ("verification_reasons", "FLASH_INCONCLUSIVE", "description"): (
        ("Con tanta luz ambiente que los colores de la pantalla no se notaron en el rostro.", "Demasiada luz ambiente: los colores de la pantalla no se notaron en el rostro."),
        ("So much ambient light that the screen colors didn't show on the face.", "Too much ambient light: the screen colors didn't show on the face."),
    ),
    ("verification_reasons", "FLASH_INCONCLUSIVE", "message"): (
        ("No pudimos ver el reflejo de la pantalla en tu rostro. Evita la luz directa del sol, sube el brillo de la pantalla e inténtalo de nuevo", "No se detectó el reflejo de la pantalla en tu rostro. Evita la luz directa del sol, sube el brillo de la pantalla e intenta de nuevo."),
        ("We couldn't see the screen's reflection on your face. Avoid direct sunlight, turn up the screen brightness, and try again", "Couldn't detect the screen's reflection on your face. Avoid direct sunlight, turn up the screen brightness, and try again."),
    ),
    ("verification_reasons", "FLASH_MISMATCH", "message"): (
        ("No pudimos confirmar que estás frente a la pantalla. Inténtalo de nuevo mirando de frente a la cámara", "No se pudo confirmar que estés frente a la pantalla. Mira de frente a la cámara e intenta de nuevo."),
        ("We couldn't confirm that you're in front of the screen. Try again looking straight at the camera", "Couldn't confirm that you're in front of the screen. Look straight at the camera and try again."),
    ),
    ("verification_reasons", "INCONSISTENT_MATCH", "message"): (
        ("Las capturas no coinciden con una sola persona. Intenta de nuevo", "Las capturas no coinciden con una sola persona. Intenta de nuevo."),
        ("The captures don't match a single person. Try again", "The captures don't match a single person. Try again."),
    ),
    ("verification_reasons", "KNOWN_ATTACK", "message"): (
        ("No pudimos confirmar tu registro. Intenta de nuevo con buena luz, frente a la pantalla.", "No se pudo confirmar tu identidad. Intenta de nuevo con buena luz, frente a la pantalla."),
        ("We couldn't confirm your record. Try again in good lighting, facing the screen.", "Couldn't confirm your identity. Try again in good lighting, facing the screen."),
    ),
    ("verification_reasons", "LIVENESS_FAILED", "message"): (
        ("No fue posible verificar tu identidad: no se detectó el movimiento solicitado", "No se detectó el movimiento solicitado. Intenta de nuevo."),
        ("We couldn't verify your identity: the requested movement wasn't detected", "The requested movement wasn't detected. Try again."),
    ),
    ("verification_reasons", "LIVENESS_MISMATCH", "message"): (
        ("No fue posible verificar tu identidad: las capturas no corresponden a la misma persona", "Las capturas no corresponden a la misma persona. Intenta de nuevo."),
        ("We couldn't verify your identity: the captures don't belong to the same person", "The captures don't belong to the same person. Try again."),
    ),
    ("verification_reasons", "REPLAY_DETECTED", "message"): (
        ("Esta captura ya se había usado antes. Toma una captura nueva frente a la cámara.", "Esta captura ya se usó. Toma una nueva frente a la cámara."),
        ("This capture was already used. Take a new capture in front of the camera.", "This capture was already used. Take a new one in front of the camera."),
    ),
    ("verification_reasons", "REPLAY_PERCEPTUAL", "message"): (
        ("Esta captura ya se había usado antes. Toma una captura nueva frente a la cámara.", "Esta captura ya se usó. Toma una nueva frente a la cámara."),
        ("This capture was already used. Take a new capture in front of the camera.", "This capture was already used. Take a new one in front of the camera."),
    ),
    ("verification_reasons", "RISK_DENIED", "message"): (
        ("No pudimos confirmar tu registro. Intenta de nuevo con buena luz, frente a la pantalla.", "No se pudo confirmar tu identidad. Intenta de nuevo con buena luz, frente a la pantalla."),
        ("We couldn't confirm your record. Try again in good lighting, facing the screen.", "Couldn't confirm your identity. Try again in good lighting, facing the screen."),
    ),
    ("verification_reasons", "SPOOF_DETECTED", "message"): (
        ("No se pudo confirmar que seas una persona frente a la cámara.", "No se pudo confirmar que seas una persona frente a la cámara. Intenta de nuevo."),
        ("We couldn't confirm that you're a person in front of the camera.", "Couldn't confirm that you're a person in front of the camera. Try again."),
    ),
    ("verification_reasons", "STATIC_CAPTURE", "message"): (
        ("Las capturas son idénticas, como si fueran una foto fija. Colócate frente a la cámara e inténtalo de nuevo.", "Las capturas son idénticas, como una foto fija. Colócate frente a la cámara e intenta de nuevo."),
        ("The captures are identical, as if they were a still photo. Stand in front of the camera and try again.", "The captures are identical, like a still photo. Stand in front of the camera and try again."),
    ),
    ("verification_reasons", "STEP_UP_REQUIRED", "message"): (
        ("Por seguridad, completa un paso más de la prueba de vida.", "Por seguridad, completa un paso más de la prueba de vida"),
        ("For security, complete one more step of the liveness check.", "For security, complete one more step of the liveness check"),
    ),
    ("work_session_statuses", "MISSED_CHECKOUT", "description"): (
        ("Venció el límite para checar la salida sin hacerlo.", "No checó su salida antes del límite."),
        ("The deadline to check out passed without checking out.", "Didn't check out before the deadline."),
    ),
}


def _apply(new: bool) -> None:
    """Cada texto en español (columna del catálogo) y en inglés (`catalog.translations`); una sentencia por texto."""
    bind = op.get_bind()
    pick = 1 if new else 0
    for (catalog, code, column), (spanish, english) in TEXTS.items():
        if spanish[0] != spanish[1]:
            bind.execute(
                sa.text(f"UPDATE {CATALOG}.{catalog} SET {column} = :text WHERE code = :code"),
                {"text": spanish[pick], "code": code},
            )
        if english[0] != english[1]:
            bind.execute(
                sa.text(
                    f"UPDATE {CATALOG}.translations SET text = :text "
                    "WHERE catalog = :catalog AND code = :code AND locale = :locale AND field = :field"
                ),
                {"text": english[pick], "catalog": catalog, "code": code, "locale": LOCALE, "field": column},
            )


def upgrade() -> None:
    _apply(new=True)


def downgrade() -> None:
    _apply(new=False)
