# Backend · Employee Time Clock — reglas de arquitectura

Reglas obligatorias para cualquier cambio (personas o agentes). Si una petición choca con ellas,
se señala el conflicto antes de escribir código; no se "rodean".

Stack: Python 3.12 · FastAPI · SQLAlchemy 2 · PostgreSQL (Alembic) · pytest. Idioma del código:
identificadores en inglés; docstrings, comentarios, mensajes y documentación en **español**.

## 1. El backend es la única fuente de verdad

- **Permisos, menús y reglas de negocio viven aquí**, nunca en el frontend. El frontend solo dibuja
  lo que el backend le envía.
- **Pantallas por rol**: `catalog.screens` + `catalog.role_screens` (seed en
  `alembic/seed/catalogs.json`). `/api/users/me`, el login y la renovación devuelven
  `user.screens` y `user.home` (`app/services/navigation_service.py`).
  - Permiso (BD): qué pantallas tiene cada rol.
  - Disponibilidad (código, `AVAILABILITY` en navigation_service): cuáles aplican al estado del
    usuario (p. ej. registro facial pendiente o aprobado).
- **Cada endpoint exige la pantalla que lo usa**: `dependencies=[Depends(require_screen(Screen.X, ...))]`
  (`app/dependencies.py`). Varias pantallas = cualquiera de ellas. Se suma al guardia de rol o de
  alcance (`AdminUser`, `CompanyScope`, `EmployeeUser`...), que define **de qué datos** se trata.
  Un endpoint nuevo sin `require_screen` es un error, salvo los comunes a toda sesión
  (`/users/me`, `/auth/*`, `/catalogs`, `/face/*`, `GET /settings/verification`).
- **Listas de valores en la BD** (esquema `catalog`): roles, estados, motivos, países, mensajes...
  Los `StrEnum` de `app/models/enums.py` solo nombran códigos que la lógica necesita y una prueba
  verifica que coincidan con las tablas (`tests/test_catalogs.py`).

### Agregar una pantalla
1. Registro en `screens` y su permiso en `role_screens` de `alembic/seed/catalogs.json`.
2. Migración nueva que lleve las bases existentes al mismo contenido (nunca editar una migración
   ya aplicada).
3. Valor en `Screen` (`enums.py`) y, si depende del estado del usuario, su regla en `AVAILABILITY`.
4. `require_screen` en los endpoints que la usan.
5. Frontend: su vista en `src/routes/screens.tsx` (lo valida su prueba de contrato).

## 2. Capas (de afuera hacia adentro; nunca al revés)

| Capa | Carpeta | Responsabilidad |
|---|---|---|
| Rutas | `app/routers/` | HTTP: validar entrada (schemas), permisos (dependencias), llamar a UN servicio, responder con `ok(...)`. Sin SQL ni reglas de negocio. |
| Servicios | `app/services/` | Reglas de negocio y transacciones (`commit`). Lanzan excepciones de `app/core/exceptions.py`. |
| Repositorios | `app/repositories/` | Consultas SQLAlchemy. Sin reglas de negocio ni `commit`. |
| Modelos | `app/models/` | ORM. Deben coincidir con las migraciones. |
| Esquemas | `app/schemas/` | Pydantic: contrato de la API (entrada y salida). |
| Núcleo | `app/core/` | Configuración, BD, seguridad, respuestas, errores, utilidades transversales. |

- **Multiempresa**: el alcance sale SIEMPRE del usuario autenticado (`CompanyScope`,
  `MemberCompany`) y cada repositorio/servicio se construye con él. Un id de otra empresa
  responde 404. Jamás se acepta `company_id` desde el cliente.
- **Contrato de respuesta único**: `ok(data, mensaje, code=...)` y errores con `code` estable
  (`COMPANY_HAS_EMPLOYEES`, `EMAIL_TAKEN`...). El frontend depende de esos códigos.
- **Sin duplicación**: antes de escribir una función, buscar si ya existe (servicios, repositorios,
  `app/core`, `aggregates.py`). Lógica repetida en dos sitios se extrae.

## 3. Base de datos

- Todo cambio de estructura o de datos de catálogo = **migración Alembic nueva** en
  `alembic/versions/` (numeración consecutiva, docstring en español que explique el porqué).
- Esquemas por dominio (`app/core/db_schemas.py`): auth, tenancy, workforce, biometrics,
  attendance, catalog.
- Llaves foráneas con `ondelete` explícito; índices para cada consulta frecuente.
- Borrados irreversibles solo con regla de negocio explícita (p. ej. eliminar empresa solo sin
  empleados; si no, desactivar).

## 4. Calidad (obligatoria antes de dar algo por terminado)

- `ruff check` + `ruff format` (línea 120, complejidad ≤ 12), `mypy` estricto en `app/`.
- **Pruebas** en `tests/` (SQLite con FKs activas; `TEST_DATABASE_URL` para PostgreSQL real).
  Cobertura mínima **85 %**. Todo endpoint, permiso y regla nueva lleva prueba, incluido el caso
  denegado (403/404/409).
- Calidad global de ambos proyectos (duplicación, librerías repetidas, dependencias circulares,
  cobertura): script de la raíz del repositorio (ver `../scripts/quality/README.md`).
- Sin formateadores ajenos al proyecto; se respeta el estilo existente.

## 5. Seguridad

- Contraseñas con hash; tokens ES256; sesiones validadas en cada petición.
- Secretos solo en `.env` (nunca en el código ni en el repositorio).
- Datos biométricos cifrados; el ADMIN de la plataforma no ve empleados ni rostros.

## 6. Documentación

- Docstring en cada módulo, clase y función pública explicando el **porqué**.
- Cambios de API o de arquitectura se reflejan en el `README.md` de la raíz (tabla de endpoints).
