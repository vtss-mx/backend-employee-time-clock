-- Comentarios de la migración 0073 (decisión del dueño del 2026-10-06: 36 fotos y consenso de identidad de la ráfaga).
-- Solo números: la ráfaga se analiza en memoria y se descarta (nunca va a la BD).

COMMENT ON COLUMN ops.face_attempt_metrics.burst_consensus IS 'Consenso de identidad de la ráfaga: mediana del parecido de sus mejores recortes quietos con las muestras de la persona (solo endurece: bajo lo exigido menos FACE_CONSENSUS_MARGIN, el intento es NO_MATCH). Nulo si no se pudo medir.';
