"""Asistente de reportes de cada empresa: preguntas en español → consultas de SU empresa → Excel.

Sin inteligencia externa (ningún dato sale del servidor): un intérprete propio del español sobre un
catálogo de datos (`datasets`), que aprende de cada empresa (`learning`) y crece solo cuando se
agregan datos nuevos al catálogo.

- `language`: normalizar texto y reconocer palabras aunque tengan errores de dedo.
- `periods`: «ayer», «el mes pasado», «del 1 al 15 de marzo»... en la hora del negocio.
- `datasets`: qué datos hay, sus columnas, cómo los nombra la gente y qué valores tienen.
- `interpreter`: la pregunta (y la conversación) → un plan de reporte validado.
- `narrator`: la respuesta en español, con hallazgos calculados de los datos.
- `excel`: el archivo de Excel (por partes, sin cargar todo en memoria).
- `service`: lo que usan las rutas (preguntar, vista previa, exportar, guardar, aprender).
"""
