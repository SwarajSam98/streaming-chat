from pydantic import BaseModel, Field


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=20_000)
    session_id: str | None = Field(
        default=None, min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_-]+$"
    )
    model: str | None = Field(default=None, min_length=1, max_length=100)
    temperature: float | None = Field(default=None, ge=0, le=2)
    system_prompt: str | None = Field(default=None, max_length=4_000)


class Usage(BaseModel):
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    estimated_cost_usd: float


class ChatResponse(BaseModel):
    session_id: str
    model: str
    response: str
    usage: Usage


class HistoryMessage(BaseModel):
    role: str
    content: str
    created_at: str


class SessionHistory(BaseModel):
    session_id: str
    messages: list[HistoryMessage]
