from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


ExposedToolName = Annotated[str, Field(pattern=r"^[A-Za-z][A-Za-z0-9]{0,63}$")]


class ConversationResponse(StrictModel):
    conversation_id: str
    created_at: str
    updated_at: str


class TextTurnRequest(StrictModel):
    client_turn_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.:-]+$")
    text: str = Field(min_length=1, max_length=100_000)
    include_text: bool = True


class TurnAcceptedResponse(StrictModel):
    turn_id: str
    state: str
    events_url: str


class ToolInvocationResponse(StrictModel):
    id: str = Field(pattern=r"^tool-[1-9][0-9]*$")
    name: ExposedToolName
    summary: str | None = Field(default=None, min_length=1, max_length=160)
    status: Literal["invoked"] = "invoked"
    started_at: str = Field(min_length=1, max_length=64)
    approval_required: bool = False
    risk: Literal["low", "medium", "high"] | None = None


class TurnResponse(StrictModel):
    turn_id: str
    conversation_id: str
    client_turn_id: str
    state: str
    created_at: str
    updated_at: str
    response_text: str | None = None
    input_text: str | None = None
    tools: list[ExposedToolName] = Field(default_factory=list)
    tool_invocations: list[ToolInvocationResponse] = Field(
        default_factory=list, max_length=256
    )
    error_code: str | None = None
    degraded_local_audio: bool = False


class ApprovalRequest(StrictModel):
    decision: Literal["once", "deny"]


class StatusResponse(StrictModel):
    status: str
    instance_id: str
    assistant_name: str = Field(min_length=1, max_length=64)


class CancelResponse(StrictModel):
    status: Literal["cancelled"]


class ErrorDetail(StrictModel):
    code: str
    message: str
    retryable: bool = False


class ErrorResponse(StrictModel):
    error: ErrorDetail
