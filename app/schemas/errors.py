from pydantic import BaseModel, Field


class ErrorBody(BaseModel):
    code: str = Field(examples=["not_found"], description="Stable machine-readable code")
    message: str = Field(examples=["Document not found"], description="Safe, human-readable message")
    request_id: str = Field(description="Also returned in the X-Request-ID header; quote it when reporting a problem")


class ErrorResponse(BaseModel):
    error: ErrorBody
