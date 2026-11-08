"""프로젝트 API 인증과 로컬 MCP만 사용하는 픽셀아트 제작 경로."""

import asyncio
from pathlib import Path

from .agent import AgentError, ArtistTools, run_artist
from .artifacts import Artifacts, create_document, export_document
from .config import Settings
from .mcp_client import AsepriteMCP
from .model_client import model_session


async def create_sprite(brief: str, settings: Settings | None = None):
    settings = settings or Settings.load()
    # 인증 누락 시 Aseprite에 빈 문서를 만들기 전에 실패합니다.
    settings.require_api()
    if not brief.strip() or len(brief) > 500:
        raise ValueError("그림 설명은 1~500자로 입력해주세요.")
    artifacts = Artifacts.create(settings.root)
    record = {"job_id": artifacts.job_id, "agent": "artist", "model": settings.model,
              "provider": settings.provider,
              "brief": brief, "status": "running"}
    artifacts.report(record)
    tools = None
    try:
        async with asyncio.timeout(settings.job_timeout):
            async with model_session(settings) as model, AsepriteMCP(settings) as mcp:
                sprite = await create_document(mcp)
                record["sprite"] = sprite
                tools = ArtistTools(mcp, sprite, settings.max_tool_calls)
                if "aseprite_draw" not in tools.schemas:
                    raise AgentError("MCP 서버에 그리기 도구가 없습니다.")
                instructions = (Path(__file__).with_name("prompts") / "artist.md").read_text(encoding="utf-8")
                instructions += "\n\n제작 리소스:\n" + await mcp.craft_notes()
                result = await run_artist(model, settings, tools, instructions, brief)
                # 파일 저장은 모델의 추가 호출이나 경로 선택에 맡기지 않습니다.
                await export_document(mcp, sprite, artifacts)
                record.update({"status": "completed", "summary": result.text,
                               "response_ids": result.response_ids,
                               "input_tokens": result.input_tokens, "output_tokens": result.output_tokens})
    except BaseException as exc:
        record["status"] = "cancelled" if isinstance(exc, asyncio.CancelledError) else "failed"
        record["error_type"] = type(exc).__name__
        raise
    finally:
        if tools:
            record["tool_calls"] = tools.call_count
            record["tools"] = tools.trace
        artifacts.report(record)
    return artifacts.job_id, artifacts.png, artifacts.preview, artifacts.source
