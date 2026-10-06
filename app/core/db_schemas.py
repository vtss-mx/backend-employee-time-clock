"""Esquemas de PostgreSQL: la base de datos se organiza por dominio.

En SQLite (pruebas) los esquemas no existen: el motor los traduce a "sin esquema".
"""

AUTH = "auth"  # usuarios, sesiones, cuentas recordadas y límites de peticiones
TENANCY = "tenancy"  # empresas y su política de verificación
WORKFORCE = "workforce"  # empleados, su credencial QR y los validadores de identidad
BIOMETRICS = "biometrics"  # registros faciales, embeddings cifrados y retos de prueba de vida
ATTENDANCE = "attendance"  # bitácora de verificaciones (checadas)
CATALOG = "catalog"  # catálogos: roles, estados, motivos, métodos, accesorios, países...
OPS = "ops"  # operación de la plataforma: errores del sistema, consumo y almacenamiento de cada empresa
BILLING = "billing"  # cobranza: plan de cada empresa, plantilla diaria, cargos, pagos y su aplicación

ALL_SCHEMAS = (AUTH, TENANCY, WORKFORCE, BIOMETRICS, ATTENDANCE, CATALOG, OPS, BILLING)
