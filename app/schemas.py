from typing import Literal, Optional

from pydantic import BaseModel, Field


ReceptionStatus = Literal[
    "IN_SERVICE",
    "READY_FOR_PICKUP",
    "COMPLETED",
    "CANCELLED",
]


class ReceptionCreate(BaseModel):
    device_type: str = Field(min_length=1, max_length=100)
    device_brand_model: str = Field(default="", max_length=300)
    serial_number: str = Field(default="", max_length=200)
    issue_description: str = ""
    phone_number1: str = Field(min_length=1, max_length=50)
    phone_number2: str = Field(default="", max_length=50)
    client_name: str = Field(default="", max_length=300)
    is_warranty_repair: bool = False


class ReceptionCreateResponse(BaseModel):
    id: str
    reception_number: str
    status: str
    client_id: Optional[str] = None
    device_id: Optional[str] = None


class ReceptionStatusUpdate(BaseModel):
    status: ReceptionStatus
    cancellation_reason: str = ""


class ReceptionNoteCreate(BaseModel):
    text: str = Field(min_length=1)
    visibility: Literal["STAFF", "OWNER_ONLY"] = "STAFF"
