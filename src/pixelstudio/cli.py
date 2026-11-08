"""프로젝트 단위 실행 명령."""

import argparse
import asyncio
import sys
import logging

from .config import ConfigurationError, Settings
from .model_client import APIRequestError, check_api
from .artifacts import ArtifactError
from .mcp_client import AsepriteError
from .agent import AgentError


async def run(args) -> None:
    settings = Settings.load()
    if args.command == "check-api":
        print(await check_api(settings))
    elif args.command == "check-mcp":
        from .mcp_client import get_status
        print(await get_status(settings))
    elif args.command == "test-mcp":
        from .mcp_smoke import test_drawing
        async with asyncio.timeout(settings.job_timeout):
            artifacts = await test_drawing(settings)
        print(f"테스트 완료: {artifacts.directory}")
    elif args.command == "draw":
        from .artist import create_sprite
        job_id, png, _preview, _source = await create_sprite(args.prompt, settings)
        print(f"제작 완료: {job_id}\n{png.parent}")


def main() -> None:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("httpx2").setLevel(logging.WARNING)
    parser = argparse.ArgumentParser(prog="pixelstudio")
    parser.add_argument("command", choices=["check-api", "check-mcp", "test-mcp", "draw", "bot"])
    parser.add_argument("--prompt", help="draw 명령에서 그릴 대상을 설명합니다.")
    args = parser.parse_args()
    if args.command == "draw" and not args.prompt:
        parser.error("draw에는 --prompt가 필요합니다.")
    try:
        if args.command == "bot":
            from .bot import run_bot
            run_bot()
            return
        asyncio.run(run(args))
    except (ConfigurationError, APIRequestError, AsepriteError, ArtifactError, AgentError) as exc:
        parser.exit(1, f"오류: {exc}\n")
    except TimeoutError:
        parser.exit(1, "오류: 작업 시간이 초과되었습니다. Aseprite에 남은 문서를 확인해주세요.\n")
