from typing import Any

from app.core.responses import ApiResponse, ErrorItem

#: Respuesta de error documentada en Swagger (success=false, data=null, errors=[...]).
ErrorResponse = ApiResponse[Any]

__all__ = ["ApiResponse", "ErrorItem", "ErrorResponse"]
