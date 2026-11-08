"""역할별 모델 사용량, 작업 전체 호출 한도와 진행 기록을 관리합니다."""

from dataclasses import dataclass, field
from datetime import datetime, timezone
import logging

from .config import Settings


logger = logging.getLogger(__name__)


class ExecutionLimitError(RuntimeError):
    pass


@dataclass
class JobLedger:
    settings: Settings
    model_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    response_ids: list[str] = field(default_factory=list)
    roles: dict = field(default_factory=dict)
    events: list[dict] = field(default_factory=list)

    def event(self, stage: str, event: str, **details):
        self.events.append({"time": datetime.now(timezone.utc).isoformat(),
                            "stage": stage, "event": event, **details})

    def begin_request(self, role: str):
        if self.model_calls >= self.settings.max_model_calls:
            raise ExecutionLimitError("작업 전체의 모델 호출 한도에 도달했습니다.")
        self.model_calls += 1
        usage = self.roles.setdefault(role, {"model_calls": 0, "input_tokens": 0, "output_tokens": 0})
        usage["model_calls"] += 1
        self.event(role, "model_requested", call=self.model_calls)
        logger.info("%s 모델 요청 %s/%s", role, self.model_calls, self.settings.max_model_calls)

    def record_response(self, role: str, response):
        usage = response.usage_metadata or {}
        for field_name in ("input_tokens", "output_tokens"):
            count = usage.get(field_name, 0)
            setattr(self, field_name, getattr(self, field_name) + count)
            self.roles[role][field_name] += count
        if response.id:
            self.response_ids.append(response.id)
        self.event(role, "model_responded", response_id=response.id)

    def report(self):
        return {"model_calls": self.model_calls, "input_tokens": self.input_tokens,
                "output_tokens": self.output_tokens, "response_ids": self.response_ids,
                "role_usage": self.roles, "events": self.events}


class MeteredModel:
    """bind_tools 뒤에도 동일한 작업 예산을 공유하는 모델 어댑터."""

    def __init__(self, model, ledger: JobLedger, role: str):
        self.model, self.ledger, self.role = model, ledger, role

    def bind_tools(self, definitions, **kwargs):
        return MeteredModel(self.model.bind_tools(definitions, **kwargs), self.ledger, self.role)

    async def ainvoke(self, messages):
        self.ledger.begin_request(self.role)
        try:
            response = await self.model.ainvoke(messages)
        except BaseException as exc:
            self.ledger.event(self.role, "model_failed", error_type=type(exc).__name__)
            raise
        self.ledger.record_response(self.role, response)
        return response
