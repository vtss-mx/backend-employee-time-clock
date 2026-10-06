-- Comentarios de la migración 0075 (documentos de la empresa). Ningún archivo vive en la base: solo su referencia al
-- objeto CIFRADO del bucket (regla 13 de la raíz).

COMMENT ON TABLE tenancy.company_documents IS 'Documentos de cada empresa para facturarle (constancia de situación fiscal, acta constitutiva, comprobante de domicilio...): la REFERENCIA de cada archivo, cifrado con DATA_ENCRYPTION_KEY en el bucket privado (nunca sus bytes). Borrado lógico; la depuración saca su objeto del bucket.';

COMMENT ON COLUMN tenancy.company_documents.uid IS 'Clave al azar del objeto del bucket (32 caracteres hexadecimales): el archivo se sube antes de abrir la transacción de su fila.';

COMMENT ON COLUMN tenancy.company_documents.object_name IS 'Objeto CIFRADO en el bucket (<prefijo>/companies/<empresa>/documents/<uid>.<ext>.enc); solo el backend lo lee, nunca por una URL pública ni firmada.';

COMMENT ON COLUMN tenancy.company_documents.sha256 IS 'SHA-256 del objeto cifrado: se verifica al leerlo.';

COMMENT ON COLUMN tenancy.company_documents.uploaded_by_platform IS 'Lo subió el ADMIN de la plataforma (true) o la empresa (false): la empresa elimina y restaura solo lo que ella subió.';
