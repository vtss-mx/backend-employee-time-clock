-- Comentarios de la migración 0087 (documentos de identidad del empleado). Ningún archivo vive en la base: solo su
-- referencia al objeto CIFRADO del bucket y los datos que el OCR extrajo (texto que la empresa confirma; regla 13).

COMMENT ON TABLE workforce.employee_documents IS 'Documentos de identidad del empleado del onboarding (comprobante de domicilio, pasaporte, INE...): la REFERENCIA de cada archivo, cifrado con DATA_ENCRYPTION_KEY en el bucket privado (nunca sus bytes), y los datos que el OCR extrajo (texto que la empresa confirma o corrige). Borrado lógico; la depuración saca su objeto del bucket.';

COMMENT ON COLUMN workforce.employee_documents.uid IS 'Clave al azar del objeto del bucket (32 caracteres hexadecimales): el archivo se sube antes de abrir la transacción de su fila.';

COMMENT ON COLUMN workforce.employee_documents.object_name IS 'Objeto CIFRADO en el bucket (<prefijo>/companies/<empresa>/employees/<empleado>/documents/<uid>.<ext>.enc); solo el backend lo lee, nunca por una URL pública ni firmada.';

COMMENT ON COLUMN workforce.employee_documents.sha256 IS 'SHA-256 del objeto cifrado: se verifica al leerlo.';

COMMENT ON COLUMN workforce.employee_documents.document_number_encrypted IS 'Número de documento CIFRADO en reposo (texto Fernet, DATA_ENCRYPTION_KEY): el identificador más fuerte; se descifra en memoria solo para la empresa y el propio empleado.';

COMMENT ON COLUMN workforce.employee_documents.uploaded_by_employee IS 'Lo subió el propio empleado (true) o la empresa en su lugar (false).';
