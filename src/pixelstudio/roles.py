"""Planner와 Reviewer의 구조화된 응답을 검증합니다. 편집 도구는 제공하지 않습니다."""

import base64
import json
from pathlib import Path

from langchain.messages import HumanMessage, SystemMessage
from pydantic import ValidationError

from .agent import AgentError
from .model_client import invoke_model
from .schemas import Plan, Review, criteria_for, validate_plan, validate_review


def role_prompt(role: str) -> str:
    return (Path(__file__).with_name("prompts") / f"{role}.md").read_text(encoding="utf-8")


async def structured_request(model, settings, schema, name: str, messages, validate):
    definition = {"name": name, "description": "검증할 구조화 결과를 제출합니다.",
                  "parameters": schema.model_json_schema()}
    options = {"strict": False, "parallel_tool_calls": False} if settings.provider == "openai" else {}
    bound = model.bind_tools([definition], tool_choice=name, **options)
    # 형식 오류에만 한 번 교정 기회를 줍니다. 인증·잔액·네트워크 오류는 재시도하지 않습니다.
    for attempt in range(2):
        response = await invoke_model(bound, messages)
        try:
            if response.invalid_tool_calls or len(response.tool_calls) != 1:
                raise ValueError("지정된 결과 도구를 정확히 한 번 호출해야 합니다.")
            call = response.tool_calls[0]
            if call["name"] != name:
                raise ValueError("지정된 결과 도구만 사용할 수 있습니다.")
            result = schema.model_validate(call["args"])
            validate(result)
            return result
        except (ValueError, ValidationError) as exc:
            if attempt:
                raise AgentError(f"{name} 결과 형식 검증에 실패했습니다.") from None
            message = "필드 형식과 필수 조건을 확인해주세요."
            if not isinstance(exc, ValidationError):
                message = str(exc)
            messages = [*messages, HumanMessage(f"결과가 검증에 실패했습니다. {message} 올바른 결과를 제출하세요.")]


async def make_plan(model, settings, brief: str) -> dict:
    result = await structured_request(model, settings, Plan, "submit_plan", [
        SystemMessage(role_prompt("planner")),
        HumanMessage(json.dumps({"사용자_요청": brief}, ensure_ascii=False)),
    ], lambda plan: validate_plan(plan, brief))
    return result.model_dump(mode="json")


async def review_image(model, settings, brief: str, plan: dict, png: Path, preview: Path) -> dict:
    criteria = criteria_for(plan, brief)
    blocks = [{"type": "text", "text": json.dumps({
        "사용자_요청": brief, "검토_기준": criteria,
        "이미지_순서": "첫 번째는 원본 PNG, 두 번째는 최근접 보간 확대본입니다. 같은 그림입니다.",
    }, ensure_ascii=False)}]
    for path in (png, preview):
        blocks.append({"type": "image", "mime_type": "image/png",
                       "base64": base64.b64encode(path.read_bytes()).decode("ascii")})
    result = await structured_request(model, settings, Review, "submit_review", [
        SystemMessage(role_prompt("reviewer")), HumanMessage(content_blocks=blocks),
    ], lambda review: validate_review(review, criteria))
    return result.model_dump(mode="json")
