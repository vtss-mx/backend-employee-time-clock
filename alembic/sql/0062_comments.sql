-- Comentarios de la migración 0062 (antifraude de identidad, fases 0 y 1): las tablas nuevas. Los esquemas no
-- cambian de descripción.

COMMENT ON TABLE biometrics.capture_traces IS 'Huella perceptual de cada captura frontal (pHash del rostro y del cuadro y el embedding CIFRADO, nunca la imagen) para detectar el reenvío de una captura modificada; se conserva FACE_REPLAY_RETENTION_DAYS.';

COMMENT ON TABLE ops.policy_changes IS 'Auditoría de cada cambio de la política de verificación (quién, cuándo, antes → después); lo que relaja la seguridad espera la aprobación de otro ADMIN.';

COMMENT ON TABLE ops.risk_assessments IS 'Decisión del motor de riesgo de cada intento facial: puntaje, nivel, acción y sus motivos (particionada por mes en created_at).';

COMMENT ON TABLE ops.fraud_cases IS 'Casos de fraude (intentos sospechosos o de riesgo alto de un mismo sujeto) que revisa el ADMIN.';

COMMENT ON TABLE ops.fraud_case_attempts IS 'Intentos de un caso de fraude con la copia de sus motivos, números y huellas pHash (sobreviven a la retención de la bitácora).';

COMMENT ON TABLE ops.fraud_case_events IS 'Historial de solo inserción de cada caso de fraude (apertura, estados, notas, evidencia consultada).';

COMMENT ON TABLE ops.fraud_evidence IS 'Referencia de los fotogramas de evidencia de un caso, cifrados en el bucket (nunca la imagen); se borran a los FRAUD_EVIDENCE_RETENTION_DAYS días.';

COMMENT ON TABLE ops.attack_signatures IS 'Lista de bloqueo: huellas pHash de ataques confirmados, de una empresa o de toda la plataforma (sin datos personales).';

COMMENT ON TABLE ops.risk_signal_stats IS 'Línea base de cada señal del motor de riesgo por empresa: fraudes confirmados y falsos positivos.';
