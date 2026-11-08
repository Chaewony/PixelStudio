"""LangChain 공통 메시지·도구 호출로 MCP와 협력하는 Artist."""

from copy import deepcopy
from dataclasses import dataclass, field
import json
import logging

from jsonschema import Draft202012Validator
from langchain.messages import HumanMessage, SystemMessage, ToolMessage

from .config import Settings
from .mcp_client import AsepriteError, AsepriteToolError, payload, text_content
from .model_client import bind_artist_tools, invoke_model


class AgentError(RuntimeError):
    pass


class ToolPolicyError(ValueError):
    pass


ALLOWED_OPERATIONS = {
    "look": {"preview", "ascii"},
    "palette": {"get", "set", "preset", "ramp", "analyze", "extract"},
    "layer": {"list", "create", "rename", "set", "activate"},
}
ALLOWED_TOOLS = {"draw", "look", "palette", "layer", "sprite_info", "read_pixels", "recolor", "validate"}
RESERVED_ARGUMENTS = {"sprite", "frame", "path", "toSprite", "batch", "fromFrame", "toFrame"}
logger = logging.getLogger(__name__)


def inline_schema(value, root: dict, seen: tuple[str, ...] = ()):
    """MCP의 문서 내부 JSON 참조를 해석한 뒤 모델용 스키마를 만듭니다."""
    if isinstance(value, list):
        return [inline_schema(item, root, seen) for item in value]
    if not isinstance(value, dict):
        return value
    if "$ref" in value:
        reference = value["$ref"]
        if not reference.startswith("#/") or reference in seen:
            raise ToolPolicyError("외부 또는 순환 JSON 스키마 참조는 지원하지 않습니다.")
        target = root
        for part in reference[2:].split("/"):
            part = part.replace("~1", "/").replace("~0", "~")
            target = target[int(part)] if isinstance(target, list) else target[part]
        expanded = inline_schema(target, root, (*seen, reference))
        siblings = inline_schema({key: item for key, item in value.items() if key != "$ref"}, root, seen)
        return {**expanded, **siblings}
    expanded = {key: inline_schema(item, root, seen) for key, item in value.items() if key != "$schema"}
    # Zod가 내보내는 Draft 7 튜플을 현재 JSON Schema 표기로 변환합니다.
    if isinstance(expanded.get("items"), list):
        expanded["prefixItems"] = expanded.pop("items")
        expanded["items"] = expanded.pop("additionalItems", {})
    return expanded


def output_blocks(result) -> list[dict]:
    # 이미지의 파일 경로가 아니라 실제 이미지 바이트를 API에 전달합니다.
    summary = {"data": payload(result), "text": text_content(result)[:12000]}
    blocks = [{"type": "text", "text": json.dumps(summary, ensure_ascii=False)}]
    for part in result.content:
        if part.type == "image":
            if part.mime_type not in {"image/png", "image/jpeg", "image/webp"}:
                raise AgentError("지원하지 않는 MCP 이미지 형식입니다.")
            blocks.append({
                "type": "image", "mime_type": part.mime_type, "base64": part.data,
            })
    return blocks


class ArtistTools:
    """모델의 편집 대상과 도구 권한을 코드에서 고정합니다."""

    def __init__(self, mcp, sprite: str, max_calls: int):
        self.mcp = mcp
        self.sprite = sprite
        self.max_calls = max_calls
        self.call_count = 0
        self.needs_inspection = False
        self.trace = []
        self.schemas = {}
        self.tool_names = {}
        self.definitions = []
        for name, tool in mcp.tools.items():
            if name not in ALLOWED_TOOLS:
                continue
            schema = inline_schema(deepcopy(tool.input_schema), tool.input_schema)
            properties = schema.setdefault("properties", {})
            for key in RESERVED_ARGUMENTS:
                properties.pop(key, None)
            schema["required"] = [key for key in schema.get("required", []) if key in properties]
            schema["additionalProperties"] = False
            if name in ALLOWED_OPERATIONS and "op" in properties:
                operations = sorted(ALLOWED_OPERATIONS[name].intersection(properties["op"].get("enum", [])))
                properties["op"]["enum"] = operations
                schema["required"] = sorted(set(schema["required"]) | {"op"})
            if name == "look":
                for key in ("reference", "framesBefore", "framesAfter", "ghostOpacity"):
                    properties.pop(key, None)
                if "scale" in properties:
                    properties["scale"]["maximum"] = 16
            if name == "validate":
                # 첫 MVP는 문서 자체 검사만 제공하고 별도 장면 관계 명세는 받지 않습니다.
                properties.pop("expect", None)
            alias = f"aseprite_{name}"
            Draft202012Validator.check_schema(schema)
            self.schemas[alias] = schema
            self.tool_names[alias] = name
            self.definitions.append({
                "name": alias,
                "description": (tool.description or name)[-2200:],
                "parameters": schema,
            })

    def validate(self, name: str, arguments: str | dict) -> tuple[str, dict]:
        if name not in self.schemas:
            raise ToolPolicyError("허용하지 않은 도구입니다.")
        try:
            values = deepcopy(arguments) if isinstance(arguments, dict) else json.loads(arguments)
        except ValueError:
            raise ToolPolicyError("도구 인자가 올바른 JSON이 아닙니다.") from None
        if not isinstance(values, dict):
            raise ToolPolicyError("도구 인자는 JSON 객체여야 합니다.")
        errors = list(Draft202012Validator(self.schemas[name]).iter_errors(values))
        if errors:
            error = errors[0]
            location = ".".join(str(part) for part in error.path) or "인자"
            raise ToolPolicyError(f"{location}: {error.message[:400]}")
        if self.tool_names[name] == "draw":
            for operation in values.get("ops", []):
                if operation.get("kind") == "grid":
                    lengths = sorted({len(row) for row in operation.get("rows", [])})
                    if len(lengths) != 1:
                        raise ToolPolicyError(f"grid의 모든 rows 길이를 같게 맞춰주세요. 현재 길이: {lengths}")
                    x, y = operation.get("x", 0), operation.get("y", 0)
                    if x < 0 or y < 0 or x + lengths[0] > 32 or y + len(operation["rows"]) > 32:
                        raise ToolPolicyError("grid가 32×32 캔버스를 벗어납니다. 위치와 행·열 수를 줄여주세요.")
        return self.tool_names[name], values

    async def _call(self, name: str, values: dict):
        if self.call_count >= self.max_calls:
            raise AgentError("작업의 도구 호출 한도에 도달했습니다.")
        self.call_count += 1
        # 모델이 문서 ID나 프레임을 생략해도 활성 문서에 의존하지 않습니다.
        arguments = {**values, "sprite": self.sprite}
        if "frame" in self.mcp.tools[name].input_schema.get("properties", {}):
            arguments["frame"] = 1
        if name == "look" and arguments.get("op", "preview") == "preview":
            arguments.setdefault("scale", 8)
        logger.info("Artist 도구 %s/%s: %s", self.call_count, self.max_calls, name)
        try:
            result = await self.mcp.call(name, arguments)
        except AsepriteToolError as exc:
            self.trace.append({"tool": name, "sprite": self.sprite, "error": str(exc), "arguments": values})
            if name in {"draw", "recolor", "palette", "layer"}:
                self.needs_inspection = True
            raise
        self.trace.append({"tool": name, "sprite": self.sprite, "data": payload(result)})
        return result

    async def execute(self, alias: str, arguments: str | dict) -> list[dict]:
        name, values = self.validate(alias, arguments)
        if self.needs_inspection and name not in {"look", "read_pixels", "sprite_info", "validate"}:
            raise ToolPolicyError("직전 편집이 실패했습니다. 먼저 look 또는 read_pixels로 문서 상태를 확인해주세요.")
        result = await self._call(name, values)
        if name in {"look", "read_pixels"}:
            self.needs_inspection = False
        blocks = output_blocks(result)
        # 픽셀 변경 직후의 실제 이미지를 같은 도구 결과에 붙여 모델에 피드백합니다.
        if name in {"draw", "recolor"}:
            preview = await self._call("look", {"op": "preview", "scale": 8})
            blocks.extend(output_blocks(preview))
        return blocks


@dataclass
class AgentResult:
    text: str
    response_ids: list[str] = field(default_factory=list)
    input_tokens: int = 0
    output_tokens: int = 0


async def run_artist(model, settings: Settings, tools: ArtistTools, instructions: str,
                     brief: str) -> AgentResult:
    model = bind_artist_tools(model, tools.definitions, settings.provider)
    history = [SystemMessage(instructions),
               HumanMessage(json.dumps({"그릴_대상": brief}, ensure_ascii=False))]
    result = AgentResult(text="")
    for round_index in range(settings.max_rounds):
        logger.info("Artist 모델 실행 %s/%s", round_index + 1, settings.max_rounds)
        response = await invoke_model(model, deepcopy(history))
        if response.id:
            result.response_ids.append(response.id)
        if response.usage_metadata:
            result.input_tokens += response.usage_metadata["input_tokens"]
            result.output_tokens += response.usage_metadata["output_tokens"]
        if response.invalid_tool_calls:
            raise AgentError("모델이 올바르지 않은 도구 인자를 반환했습니다. 편집을 중단합니다.")
        history.append(response)
        calls = response.tool_calls
        if not calls:
            result.text = response.text or "제작 절차를 완료했습니다."
            return result
        images = []
        for call in calls:
            if not call.get("id"):
                raise AgentError("모델의 도구 호출 ID가 없습니다.")
            try:
                blocks = await tools.execute(call["name"], call["args"])
            except ToolPolicyError as exc:
                # 유효하지 않은 요청은 실행하지 않고, 모델에 교정 기회를 줍니다.
                blocks = [{"type": "text", "text": json.dumps({"error": str(exc)}, ensure_ascii=False)}]
            except AsepriteToolError as exc:
                blocks = [{"type": "text", "text": json.dumps({
                    "error": str(exc),
                    "next": "편집 실패 시 먼저 look으로 실제 상태를 확인하고 인자를 수정하세요. 같은 편집을 무작정 반복하지 마세요.",
                }, ensure_ascii=False)}]
            except AsepriteError:
                # 변경 여부가 불명확한 편집은 자동으로 재실행하지 않습니다.
                raise AgentError("Aseprite 도구 실행에 실패했습니다. 문서 상태를 확인해주세요.") from None
            # 모든 공급자가 도구 메시지의 이미지를 받는 것은 아니므로 결과와 이미지를 나눕니다.
            history.append(ToolMessage(tool_call_id=call["id"], name=call["name"],
                                       content="\n".join(block["text"] for block in blocks if block["type"] == "text")))
            images.extend({**block, "extras": {"tool_call_id": call["id"]}}
                          for block in blocks if block["type"] == "image")
        if images:
            history.append(HumanMessage(content_blocks=[
                {"type": "text", "text": "직전 도구 실행의 실제 미리보기입니다. 이미지를 확인한 뒤 계속 제작하세요."},
                *images,
            ]))
    raise AgentError("모델 실행 횟수 한도에 도달했습니다.")
