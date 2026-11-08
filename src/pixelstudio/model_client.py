"""LangChain 공통 모델 연결. 공급자별 옵션은 이 모듈에서만 다룹니다."""

import asyncio
from contextlib import asynccontextmanager

from langchain.chat_models import init_chat_model
from langchain.messages import AIMessage, HumanMessage

from .config import ConfigurationError, Settings
from .harness import ExecutionLimitError


class APIRequestError(RuntimeError):
    def __init__(self, message: str, *, status_code=None, code=None):
        super().__init__(message)
        self.status_code, self.code = status_code, code


def create_model(settings: Settings):
    settings.require_api()
    options = {"api_key": settings.api_key, "max_tokens": settings.max_output_tokens,
               "max_retries": 0}
    if settings.provider == "openai":
        options.update(base_url="https://api.openai.com/v1", timeout=90,
                       use_responses_api=True, store=False,
                       include=["reasoning.encrypted_content"])
    elif settings.provider == "anthropic":
        options.update(timeout=90)
    else:
        # Google SDK의 timeout 단위 변환은 LangChain이 담당합니다.
        options.update(vertexai=False, timeout=90)
    try:
        return init_chat_model(settings.model, model_provider=settings.provider, **options)
    except ImportError:
        extra = {"anthropic": "anthropic", "google_genai": "google"}.get(settings.provider)
        command = f"uv sync --locked --extra {extra}" if extra else "uv sync --locked"
        raise ConfigurationError(f"모델 연결 패키지가 필요합니다. 실행: {command}") from None
    except Exception:
        raise ConfigurationError("모델을 초기화할 수 없습니다. 공급자·모델 설정을 확인해주세요.") from None


@asynccontextmanager
async def model_session(settings: Settings):
    model = create_model(settings)
    try:
        yield model
    finally:
        # OpenAI·Anthropic의 HTTP 풀은 LangChain이 공유하므로 작업마다 닫지 않습니다.
        # Google 모델은 각 인스턴스가 만든 SDK 연결을 공개 API로 정리합니다.
        if settings.provider == "google_genai" and model.client is not None:
            await model.client.aio.aclose()
            model.client.close()


def bind_artist_tools(model, definitions: list[dict], provider: str):
    options = {"parallel_tool_calls": False, "strict": False} if provider == "openai" else {}
    return model.bind_tools(definitions, **options)


async def invoke_model(model, messages) -> AIMessage:
    try:
        async with asyncio.timeout(90):
            response = await model.ainvoke(messages)
    except TimeoutError:
        raise APIRequestError("모델 API 응답 시간이 초과되었습니다.") from None
    except ExecutionLimitError:
        raise
    except Exception as exc:
        # 공급자 예외 원문에는 요청 내용이 들어갈 수 있으므로 그대로 노출하지 않습니다.
        status = getattr(exc, "status_code", None) or getattr(exc, "code", None)
        suffix = f" (HTTP {status})" if isinstance(status, int) else ""
        body = getattr(exc, "body", None)
        error = body.get("error", body) if isinstance(body, dict) else {}
        error = error if isinstance(error, dict) else {}
        code = error.get("code") or getattr(exc, "code", None)
        known_codes = {"credit_balance_exhausted", "insufficient_quota", "project_spend_limit_exceeded",
                       "organization_usage_limit_exceeded", "rate_limit_exceeded", "slow_down"}
        code = code if isinstance(code, str) and code in known_codes else None
        if code in known_codes - {"rate_limit_exceeded", "slow_down"}:
            message = "모델 API 잔액 또는 사용 한도가 소진됐습니다. 해당 공급자의 결제·한도를 확인해주세요."
        elif status == 429:
            message = "모델 API가 HTTP 429로 요청을 거절했습니다. 요청 속도 제한과 결제·사용 한도를 확인해주세요."
        else:
            message = f"모델 API 요청 실패{suffix}. API 키·모델 권한·한도·네트워크를 확인해주세요."
        raise APIRequestError(message, status_code=status, code=code) from None
    if not isinstance(response, AIMessage):
        raise APIRequestError("모델이 지원하지 않는 응답 형식을 반환했습니다.")
    metadata = response.response_metadata
    status = metadata.get("status", "completed")
    finish = str(metadata.get("finish_reason") or metadata.get("stop_reason") or "").lower()
    if status != "completed" or finish in {
        "length", "max_tokens", "max_output_tokens", "content_filter", "safety", "recitation",
        "malformed_function_call", "unexpected_tool_call", "refusal", "pause_turn", "blocklist",
        "prohibited_content", "spii", "image_safety", "other",
    }:
        raise APIRequestError("모델 응답이 완료되지 않았습니다. 출력 한도·모델 지원 기능을 확인해주세요.")
    if any(block.get("type") == "refusal" for block in response.content_blocks):
        raise APIRequestError("모델이 제작 요청을 거절했습니다.")
    return response


async def check_api(settings: Settings) -> str:
    async with model_session(settings) as model:
        response = await invoke_model(model, [HumanMessage(
            "연결 확인입니다. 다른 설명 없이 API 연결 성공이라고 답해주세요."
        )])
    return response.text
