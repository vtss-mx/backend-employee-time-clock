"""Catálogo del asistente: qué datos hay, sus columnas, cómo los nombra la gente y sus valores.

Es lo que hace crecer al asistente: cada módulo nuevo con datos de la empresa agrega aquí su
`Dataset` (y su consulta en `report_repository.SOURCES`) y el asistente ya sabe responder sobre él,
exportarlo y agruparlo; ninguna otra pieza cambia. `tests/test_reports_catalog.py` falla si una
tabla con datos de una empresa no está en un reporte ni excluida con su motivo.

Nunca hay aquí datos biométricos, contraseñas, tokens ni llaves: solo lo que la empresa ya ve en
sus pantallas.
"""

from dataclasses import dataclass, replace
from typing import Literal

Kind = Literal["text", "number", "date", "datetime", "bool", "category"]


@dataclass(frozen=True)
class Value:
    """Un valor por el que se puede filtrar y cómo lo dice la gente («rechazados», «sin rostro»)."""

    value: str | bool
    label: str
    words: tuple[str, ...]


@dataclass(frozen=True)
class Column:
    code: str
    label: str
    kind: Kind
    #: Cómo se nombra la columna en una pregunta («correo», «email»).
    words: tuple[str, ...] = ()
    #: Valores con sus sinónimos (categorías y sí/no).
    values: tuple[Value, ...] = ()
    #: Catálogo de la BD cuyos nombres se muestran y reconocen (p. ej. motivos de rechazo).
    catalog: str | None = None
    #: Se reconoce por nombre propio en la pregunta: department, employee o validator.
    entity: str | None = None
    #: Se puede agrupar «por» ella.
    group: bool = False
    #: Se muestra si no se piden columnas.
    default: bool = True
    #: Se puede sumar o promediar.
    metric: bool = False


@dataclass(frozen=True)
class Dataset:
    code: str
    name: str
    #: (singular, plural) para redactar: «3 empleados», «1 identificación».
    noun: tuple[str, str]
    description: str
    #: Cómo se refiere la gente a estos datos.
    words: tuple[str, ...]
    columns: tuple[Column, ...]
    #: Columna que filtran los periodos («ayer», «en marzo»); None = no tiene fecha.
    time: str | None
    sort: str
    descending: bool
    examples: tuple[str, ...]
    #: Tablas de la BD de las que salen (esquema.tabla).
    sources: tuple[str, ...]

    def column(self, code: str) -> Column | None:
        return next((c for c in self.columns if c.code == code), None)

    @property
    def default_columns(self) -> list[str]:
        return [c.code for c in self.columns if c.default]


def _bool(yes_label: str, yes: tuple[str, ...], no_label: str, no: tuple[str, ...]) -> tuple[Value, ...]:
    return (Value(True, yes_label, yes), Value(False, no_label, no))


# ---------- Columnas compartidas ----------

EMPLOYEE = Column(
    "employee",
    "Empleado",
    "text",
    ("empleado", "persona", "trabajador", "colaborador", "quien"),
    entity="employee",
    group=True,
)
EMPLOYEE_NUMBER = Column(
    "employee_number", "Número de empleado", "text", ("numero de empleado", "clave"), default=False
)
DEPARTMENT = Column(
    "department", "Departamento", "text", ("departamento", "area", "depto", "seccion"), entity="department", group=True
)
FACE_STATUS = Column(
    "face_status",
    "Registro facial",
    "category",
    ("registro facial", "estado del rostro", "estado facial"),
    catalog="face_statuses",
    group=True,
    values=(
        Value(
            "NOT_ENROLLED",
            "Sin registrar",
            ("sin registro facial", "sin rostro", "sin registrar", "no registrado", "sin registro"),
        ),
        Value(
            "PENDING_REVIEW", "En validación", ("en validacion", "por validar", "pendiente de validar", "en revision")
        ),
        Value("APPROVED", "Validado", ("validado", "aprobado", "con rostro", "con registro facial", "rostro validado")),
        Value("REJECTED", "Rechazado", ("rechazado", "registro rechazado")),
    ),
)

DATASETS: tuple[Dataset, ...] = (
    Dataset(
        code="employees",
        name="Empleados",
        noun=("empleado", "empleados"),
        description="Plantilla de la empresa: datos, departamento, estado y registro facial.",
        words=("empleado", "trabajador", "colaborador", "personal", "plantilla", "gente", "persona", "staff", "nomina"),
        columns=(
            replace(EMPLOYEE_NUMBER, default=True),
            Column("full_name", "Nombre", "text", ("nombre",)),
            DEPARTMENT,
            Column("email", "Correo", "text", ("correo", "email", "mail")),
            Column("phone", "Teléfono", "text", ("telefono", "celular", "movil"), default=False),
            Column("rfc", "RFC", "text", ("rfc",), default=False),
            Column("curp", "CURP", "text", ("curp",), default=False),
            Column("nss", "NSS", "text", ("nss", "seguro social", "imss"), default=False),
            Column("birth_date", "Fecha de nacimiento", "date", ("nacimiento", "cumpleanos", "edad"), default=False),
            Column(
                "active",
                "Activo",
                "bool",
                ("estatus", "activo"),
                group=True,
                values=_bool(
                    "Activo",
                    ("activo", "activa", "vigente"),
                    "Inactivo",
                    ("inactivo", "inactiva", "desactivado", "dado de baja", "de baja", "baja"),
                ),
            ),
            FACE_STATUS,
            Column(
                "headwear_exempt",
                "Exento de gorra",
                "bool",
                ("exento", "excepcion", "gorra"),
                default=False,
                values=_bool("Exento", ("exento", "con excepcion"), "Sin excepción", ("sin excepcion",)),
            ),
            Column("created_at", "Alta", "datetime", ("alta", "ingreso", "registrado", "contratado", "nuevo")),
        ),
        time="created_at",
        sort="full_name",
        descending=False,
        examples=(
            "¿Cuántos empleados activos hay por departamento?",
            "Empleados sin registro facial",
            "Empleados dados de alta este mes con su correo y teléfono",
        ),
        sources=("workforce.employees", "auth.users"),
    ),
    Dataset(
        code="departments",
        name="Departamentos",
        noun=("departamento", "departamentos"),
        description="Departamentos con su número de empleados y sus responsables.",
        words=("departamento", "area", "depto", "seccion", "responsable", "jefe", "encargado"),
        columns=(
            Column("name", "Departamento", "text", ("nombre",)),
            Column("description", "Descripción", "text", ("descripcion",), default=False),
            Column("employees", "Empleados", "number", ("empleado", "plantilla", "personal"), metric=True),
            Column("managers", "Responsables", "text", ("responsable", "jefe", "encargado", "gerente")),
            Column("created_at", "Creado", "datetime", ("creado", "alta")),
        ),
        time="created_at",
        sort="name",
        descending=False,
        examples=("Departamentos con sus responsables", "¿Qué departamento tiene más empleados?"),
        sources=("workforce.departments", "workforce.department_managers"),
    ),
    Dataset(
        code="attendance",
        name="Identificaciones",
        noun=("identificación", "identificaciones"),
        description="Cada vez que un empleado se identificó (con rostro o QR), exitosa o no: la asistencia.",
        words=(
            "identificacion",
            "asistencia",
            "checada",
            "checado",
            "checar",
            "chequeo",
            "verificacion",
            "acceso",
            "marcaje",
            "fichaje",
            "bitacora",
            "intento",
            "registro de entrada",
            "registro de asistencia",
            "entrada",
            "llegada",
            "identificaron",
            "identifico",
            "se identificaron",
            "checaron",
            "asistieron",
            "vinieron",
        ),
        columns=(
            Column("occurred_at", "Fecha y hora", "datetime", ("fecha", "hora", "cuando")),
            Column("day", "Día", "date", ("dia", "diario", "fecha"), group=True, default=False),
            Column("week", "Semana", "date", ("semana", "semanal"), group=True, default=False),
            Column("month", "Mes", "text", ("mes", "mensual"), group=True, default=False),
            Column("hour", "Hora del día", "number", ("hora",), group=True, default=False),
            EMPLOYEE,
            EMPLOYEE_NUMBER,
            DEPARTMENT,
            Column(
                "method",
                "Método",
                "category",
                ("metodo", "forma", "tipo", "medio"),
                catalog="verification_methods",
                group=True,
                values=(
                    Value("QR_FACE", "QR + rostro", ("qr y rostro", "qr mas rostro", "qr con rostro")),
                    Value("FACE", "Rostro", ("rostro", "cara", "facial", "reconocimiento facial", "biometrico")),
                    Value("QR", "QR", ("qr", "codigo qr", "codigo")),
                ),
            ),
            Column(
                "success",
                "Resultado",
                "bool",
                ("resultado",),
                group=True,
                values=_bool(
                    "Exitosa",
                    ("exitosa", "exitoso", "correcta", "aceptada", "valida", "reconocido", "bien"),
                    "Fallida",
                    (
                        "fallida",
                        "fallido",
                        "fallo",
                        "fallaron",
                        "rechazada",
                        "denegada",
                        "error",
                        "no reconocido",
                        "intento fallido",
                    ),
                ),
            ),
            Column(
                "reason",
                "Motivo",
                "category",
                ("motivo", "razon", "causa", "por que"),
                catalog="verification_reasons",
                group=True,
                values=(
                    Value("SPOOF_DETECTED", "Posible foto o pantalla", ("suplantacion", "foto", "pantalla")),
                    Value("LIVENESS_FAILED", "Prueba de vida no superada", ("prueba de vida",)),
                    Value("EXPIRED", "QR vencido", ("vencido",)),
                    Value("ALREADY_USED", "QR ya usado", ("ya usado",)),
                ),
            ),
            Column(
                "validator",
                "Validador",
                "text",
                ("validador", "tableta", "dispositivo", "punto de control", "caseta", "checador", "lector"),
                entity="validator",
                group=True,
            ),
            Column(
                "score",
                "Confianza",
                "number",
                ("confianza", "similitud", "parecido", "certeza"),
                default=False,
                metric=True,
            ),
        ),
        time="occurred_at",
        sort="occurred_at",
        descending=True,
        examples=(
            "¿Cuántas identificaciones hubo hoy?",
            "Identificaciones fallidas de la semana pasada por motivo",
            "¿Quién se identificó más este mes?",
            "Identificaciones por día de los últimos 30 días",
        ),
        sources=("attendance.verification_logs",),
    ),
    Dataset(
        code="workdays",
        name="Jornadas (entrada y salida)",
        noun=("jornada", "jornadas"),
        description="Por empleado y día: primera identificación (entrada), última (salida) y horas entre ambas.",
        words=(
            "jornada",
            "entrada y salida",
            "horas trabajadas",
            "horas",
            "horario",
            "hora de entrada",
            "hora de salida",
            "retardo",
            "tarde",
            "llegaron tarde",
            "llego tarde",
            "puntualidad",
            "asistencia diaria",
            "salida",
            "tiempo trabajado",
        ),
        columns=(
            Column("day", "Día", "date", ("dia", "fecha", "diario"), group=True),
            EMPLOYEE,
            EMPLOYEE_NUMBER,
            DEPARTMENT,
            Column("entry", "Entrada", "datetime", ("entrada", "llegada", "llegaron", "llego")),
            Column("exit", "Salida", "datetime", ("salida", "salieron", "salio")),
            Column("hours", "Horas", "number", ("horas", "tiempo", "trabajadas"), metric=True),
            Column("checks", "Identificaciones", "number", ("identificaciones", "checadas"), metric=True),
            Column(
                "entry_hour", "Hora de entrada (decimal)", "number", ("hora de entrada",), default=False, metric=True
            ),
            Column("exit_hour", "Hora de salida (decimal)", "number", ("hora de salida",), default=False, metric=True),
        ),
        time="day",
        sort="day",
        descending=True,
        examples=(
            "Horas trabajadas por empleado esta semana",
            "¿Quién llegó después de las 9 ayer?",
            "Promedio de horas por departamento el mes pasado",
        ),
        sources=("attendance.verification_logs",),
    ),
    Dataset(
        code="enrollments",
        name="Registros faciales",
        noun=("registro facial", "registros faciales"),
        description="Solicitudes de registro facial: cuándo se enviaron, quién las revisó y con qué resultado.",
        words=(
            "registro facial",
            "solicitud",
            "enrolamiento",
            "validacion de rostro",
            "revision",
            "alta de rostro",
            "registros faciales",
        ),
        columns=(
            Column("submitted_at", "Enviado", "datetime", ("enviado", "fecha", "cuando")),
            EMPLOYEE,
            EMPLOYEE_NUMBER,
            DEPARTMENT,
            Column(
                "status",
                "Estado",
                "category",
                ("estado", "estatus", "resultado"),
                catalog="enrollment_statuses",
                group=True,
                values=(
                    Value("PENDING", "Pendiente", ("pendiente", "por revisar", "sin revisar")),
                    Value("APPROVED", "Aceptado", ("aceptado", "aprobado", "validado")),
                    Value("REJECTED", "Rechazado", ("rechazado",)),
                ),
            ),
            Column("reviewed_at", "Revisado", "datetime", ("revisado",)),
            Column("reviewer", "Revisó", "text", ("reviso", "revisor"), default=False),
            Column("rejection_reason", "Motivo de rechazo", "text", ("motivo", "razon")),
            Column(
                "in_person",
                "En persona",
                "bool",
                ("en persona", "presencial"),
                default=False,
                values=_bool("En persona", ("en persona", "presencial"), "Por el empleado", ("por el empleado",)),
            ),
            Column("quality", "Calidad", "number", ("calidad",), default=False, metric=True),
        ),
        time="submitted_at",
        sort="submitted_at",
        descending=True,
        examples=("Registros faciales pendientes", "Registros rechazados este mes con su motivo"),
        sources=("biometrics.face_enrollments",),
    ),
    Dataset(
        code="validators",
        name="Validadores",
        noun=("validador", "validadores"),
        description="Puntos de control (tabletas o teléfonos) con su modo, ubicación y uso.",
        words=("validador", "punto de control", "caseta", "checador", "lector", "tableta"),
        columns=(
            Column("name", "Validador", "text", ("nombre",), entity="validator"),
            Column("mode", "Modo", "category", ("modo",), catalog="validator_modes", group=True),
            Column("city", "Ciudad", "text", ("ciudad", "municipio"), group=True),
            Column("state", "Estado", "text", ("estado",), default=False, group=True),
            Column(
                "location_required",
                "Ubicación obligatoria",
                "bool",
                ("ubicacion", "geocerca"),
                values=_bool(
                    "Con ubicación",
                    ("con ubicacion", "ubicacion obligatoria", "geocerca"),
                    "Sin ubicación",
                    ("sin ubicacion",),
                ),
            ),
            Column(
                "identifications", "Identificaciones", "number", ("identificaciones", "uso", "checadas"), metric=True
            ),
            Column("last_identification", "Última identificación", "datetime", ("ultima identificacion", "ultimo uso")),
            Column("devices", "Dispositivos autorizados", "number", ("dispositivos",), metric=True),
            Column("created_at", "Alta", "datetime", ("alta", "creado"), default=False),
        ),
        time="created_at",
        sort="name",
        descending=False,
        examples=("¿Qué validador tiene más identificaciones?", "Validadores con ubicación obligatoria"),
        sources=("workforce.validators",),
    ),
    Dataset(
        code="devices",
        name="Dispositivos de validadores",
        noun=("dispositivo", "dispositivos"),
        description="Teléfonos o tabletas de cada validador y su autorización.",
        words=("dispositivo", "equipo", "telefono", "tableta", "celular", "aparato"),
        columns=(
            Column("validator", "Validador", "text", ("validador",), entity="validator", group=True),
            Column("name", "Dispositivo", "text", ("nombre", "modelo")),
            Column(
                "status",
                "Estado",
                "category",
                ("estado", "estatus"),
                catalog="device_statuses",
                group=True,
                values=(
                    Value("PENDING", "Por autorizar", ("por autorizar", "pendiente", "sin autorizar")),
                    Value("APPROVED", "Autorizado", ("autorizado", "aprobado")),
                    Value("REJECTED", "Rechazado", ("rechazado",)),
                    Value("REVOKED", "Revocado", ("revocado",)),
                ),
            ),
            Column("created_at", "Registrado", "datetime", ("registrado", "alta")),
            Column("last_seen_at", "Último uso", "datetime", ("ultimo uso", "visto")),
        ),
        time="created_at",
        sort="created_at",
        descending=True,
        examples=("Dispositivos por autorizar", "Dispositivos por validador"),
        sources=("workforce.validator_devices",),
    ),
    Dataset(
        code="api_keys",
        name="Llaves de la API",
        noun=("llave", "llaves"),
        description="Llaves de integración (nómina, ERP): permisos, vigencia y último uso.",
        words=("llave", "api", "integracion", "clave de api", "token de api"),
        columns=(
            Column("name", "Llave", "text", ("nombre",)),
            Column("prefix", "Prefijo", "text", ("prefijo",), default=False),
            Column(
                "status",
                "Estado",
                "category",
                ("estado", "estatus"),
                catalog="api_key_statuses",
                group=True,
                values=(
                    Value("ACTIVE", "Activa", ("activa", "vigente")),
                    Value("EXPIRED", "Vencida", ("vencida", "expirada")),
                    Value("REVOKED", "Revocada", ("revocada",)),
                ),
            ),
            Column("scopes", "Permisos", "text", ("permiso", "alcance")),
            Column("created_at", "Creada", "datetime", ("creada", "alta")),
            Column("expires_at", "Vence", "datetime", ("vence", "vencimiento")),
            Column("last_used_at", "Último uso", "datetime", ("ultimo uso", "usada")),
        ),
        time="created_at",
        sort="created_at",
        descending=True,
        examples=("Llaves de la API activas y su último uso",),
        sources=("tenancy.company_api_keys", "tenancy.company_api_key_scopes"),
    ),
    Dataset(
        code="face_learning",
        name="Evolución del reconocimiento facial",
        noun=("empleado", "empleados"),
        description="Por empleado: muestras del registro, muestras aprendidas del uso y cuántas veces ayudaron.",
        words=("aprendizaje", "aprendido", "evolucion", "muestras", "reconocimiento", "galeria"),
        columns=(
            EMPLOYEE,
            EMPLOYEE_NUMBER,
            DEPARTMENT,
            Column("anchors", "Muestras del registro", "number", ("registro", "ancla"), metric=True),
            Column("learned", "Muestras aprendidas", "number", ("aprendida", "aprendido"), metric=True),
            Column("matches", "Veces que ayudaron", "number", ("ayudaron", "coincidencias", "usos"), metric=True),
            Column("last_learned_at", "Último aprendizaje", "datetime", ("ultimo aprendizaje",)),
        ),
        time=None,
        sort="learned",
        descending=True,
        examples=("¿Qué empleados tienen más muestras aprendidas?",),
        sources=("biometrics.face_embeddings",),
    ),
)

BY_CODE = {dataset.code: dataset for dataset in DATASETS}

#: Tablas con datos de una empresa que el asistente NO reporta, y por qué (la prueba del catálogo
#: exige que toda tabla con `company_id` esté en un reporte o aquí).
EXCLUDED_TABLES = {
    "tenancy.verification_policy": "Configuración (no son datos de operación); se ve en Configuración.",
    "auth.auth_sessions": "Seguridad: sesiones y sus tokens.",
    "biometrics.capture_fingerprints": "Seguridad: huellas anti-reenvío de capturas.",
    "ops.error_occurrences": "Operación de la plataforma (solo el ADMIN).",
    "reporting.assistant_queries": "La memoria del propio asistente.",
    "reporting.learned_phrases": "La memoria del propio asistente.",
    "reporting.saved_reports": "Los reportes guardados (se usan desde Reportes).",
}
