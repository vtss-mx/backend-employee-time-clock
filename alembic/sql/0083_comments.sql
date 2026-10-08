-- Comentarios de la migración 0083 (los tres pasos independientes del registro facial). Ninguna imagen vive en la
-- base: el borrador guarda la REFERENCIA de la foto inicial, cifrada en el bucket, y su plantilla facial cifrada (un
-- vector de números, regla 13 de la raíz).

COMMENT ON TABLE biometrics.face_enrollment_drafts IS 'Borrador del registro facial del propio empleado (paso 1 de 3, decisión del dueño 2026-10-07): la foto inicial aceptada, cifrada en el bucket (solo su referencia), y su plantilla facial cifrada con que el paso 2 comprueba que las capturas son de la misma persona. Uno por empleado; vence a las FACE_ENROLLMENT_DRAFT_HOURS; al aceptar las capturas su foto pasa a ser la foto de referencia del registro y la fila sale.';

COMMENT ON COLUMN biometrics.face_enrollment_drafts.template_encrypted IS 'Plantilla facial de la foto inicial cifrada con DATA_ENCRYPTION_KEY (vector float32 del modelo model_name, dimension valores): nunca una imagen.';

COMMENT ON COLUMN biometrics.face_enrollment_drafts.photo_object IS 'Objeto CIFRADO en el bucket (<prefijo>/companies/<empresa>/employees/<empleado>/face-enrollments/drafts/<id>.<ext>.enc); solo el backend lo lee. Al aceptar las capturas lo referencia el registro (face_enrollments.photo_object, sin volver a subirlo); pasa a la cola de borrado al reemplazar el borrador, al vencer o al eliminar a la persona.';

COMMENT ON COLUMN biometrics.face_enrollment_drafts.expires_at IS 'Hasta cuándo sirve la foto inicial para el paso 2 (checked_at + FACE_ENROLLMENT_DRAFT_HOURS); vencida, el paso 2 responde 409 ENROLLMENT_PHOTO_REQUIRED y la depuración la borra con su objeto.';
