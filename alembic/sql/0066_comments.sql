-- Comentarios de la migración 0066 (antifraude de identidad, fase 2a: protocolo de captura de frontera). Solo números:
-- la hoja de la ráfaga se analiza en memoria y se descarta (nunca va a la BD).

COMMENT ON COLUMN tenancy.verification_policy.flash_paced IS 'Destello dictado por el servidor: los colores se revelan uno por uno por el canal en vivo con tiempo por color (sin canal, el destello de siempre y la señal FLASH_UNPACED).';

COMMENT ON COLUMN tenancy.verification_policy.capture_burst IS 'Ráfaga corta de recortes del rostro con las capturas (continuidad, micromovimiento y pulso; decisión D11).';

COMMENT ON COLUMN biometrics.face_challenges.flash_paced IS 'El reto no entregó sus colores: se revelan uno por uno por el canal en vivo y la respuesta trae el comprobante firmado.';

COMMENT ON COLUMN ops.face_attempt_metrics.burst_frames IS 'Recortes de la ráfaga analizados (la hoja nunca se guarda).';

COMMENT ON COLUMN ops.face_attempt_metrics.burst_motion IS 'Micromovimiento de la ráfaga: diferencia media entre recortes seguidos del tramo quieto (niveles de gris; 0 = congelada).';

COMMENT ON COLUMN ops.face_attempt_metrics.pulse_snr IS 'Pulso por video (rPPG, POS): SNR en dB del pico cardiaco. Solo se mide; nunca decide.';

COMMENT ON COLUMN ops.face_attempt_metrics.moire IS 'Patrón de pantalla (moiré) de las frontales: el pico más prominente del espectro del rostro, en dB (el mayor de las frontales).';

COMMENT ON COLUMN ops.face_attempt_metrics.noise_ratio IS 'Ruido del sensor del rostro entre el del fondo de la misma captura (el menor de las frontales; un rostro pegado o recapturado es más liso).';

COMMENT ON COLUMN ops.face_attempt_metrics.parallax IS 'Paralaje de los giros y cabeceos: residuo de la nariz tras la afín de ojos y comisuras, en distancias entre ojos (el menor; una foto plana da ≈ 0).';

COMMENT ON COLUMN ops.face_attempt_metrics.flash_pace_ms IS 'Destello dictado por el servidor: la respuesta más lenta de un color (ms desde que se reveló); nulo si no fue dictado.';
