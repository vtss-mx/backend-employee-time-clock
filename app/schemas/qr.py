from datetime import datetime

from pydantic import BaseModel


class EmployeeQrRead(BaseModel):
    id: int
    employee_id: int
    employee_number: str
    active: bool
    created_at: datetime
    expires_at: datetime | None
    image_base64: str  # data URL PNG listo para <img src="...">
    file_name: str
