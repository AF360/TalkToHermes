from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


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


class TurnResponse(StrictModel):
    turn_id: str
    conversation_id: str
    client_turn_id: str
    state: str
    created_at: str
    updated_at: str
    response_text: str | None = None
    error_code: str | None = None
    degraded_local_audio: bool = False


class ApprovalRequest(StrictModel):
    decision: Literal["once", "deny"]


class StatusResponse(StrictModel):
    status: str
    instance_id: str


class ErrorDetail(StrictModel):
    code: str
    message: str
    retryable: bool = False


class ErrorResponse(StrictModel):
    error: ErrorDetail
