-- Comentarios de la migración 0061 (foto de perfil): la tabla nueva y el esquema auth, que ahora también guarda la
-- referencia de las fotos de perfil (la imagen vive cifrada en el bucket, nunca en la base).

COMMENT ON SCHEMA auth IS 'Identidad: cuentas, la referencia de sus fotos de perfil, sesiones, cuentas recordadas y límites de peticiones (de la plataforma, sin seguridad por fila).';

COMMENT ON TABLE auth.user_avatars IS 'Foto de perfil de una persona: la referencia (objeto, tipo, tamaño, SHA-256) de cada tamaño cifrado en el bucket, nunca la imagen. Una fila por tamaño (512 y 96 px); la versión vigente se repite en auth.users.avatar_version.';
