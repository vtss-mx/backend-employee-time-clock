"""Esquemas de PostgreSQL: la base de datos se organiza por dominio.

En SQLite (pruebas) los esquemas no existen: el motor los traduce a "sin esquema".
"""

AUTH = "auth"  # usuarios, sesiones, cuentas recordadas y límites de peticiones
TENANCY = "tenancy"  # empresas y su política de verificación
WORKFORCE = "workforce"  # empleados y su credencial QR
BIOMETRICS = "biometrics"  # registros faciales, embeddings cifrados y retos de prueba de vida
ATTENDANCE = "attendance"  # bitácora de verificaciones (checadas)

ALL_SCHEMAS = (AUTH, TENANCY, WORKFORCE, BIOMETRICS, ATTENDANCE)
