"""로컬 Aseprite MCP에 stdio로 직접 연결합니다. Codex는 실행하지 않습니다."""

import asyncio
import json
import os
import shutil

from mcp import Client, StdioServerParameters

from .config import Settings


class AsepriteError(RuntimeError):
    pass


class AsepriteToolError(AsepriteError):
    """서버가 명시적으로 반환한 도구 오류. 전송 오류와 구분합니다."""


def text_content(result) -> str:
    return "\n".join(
        block.text for block in result.content if block.type == "text"
    )


def payload(result) -> dict:
    if isinstance(result.structured_content, dict):
        return result.structured_content
    for block in result.content:
        if block.type == "text":
            try:
                value = json.loads(block.text)
            except (ValueError, TypeError):
                continue
            if isinstance(value, dict):
                return value
    return {}


def server_environment() -> dict[str, str]:
    # 그림 도구 프로세스에는 API 키나 Discord 인증 정보를 전달하지 않습니다.
    private = {
        "OPENAI_API_KEY", "OPENAI_ADMIN_KEY", "DISCORD_BOT_TOKEN",
        "CODEX_API_KEY", "CODEX_ACCESS_TOKEN", "AZURE_OPENAI_API_KEY",
        "ANTHROPIC_API_KEY", "GOOGLE_API_KEY", "GEMINI_API_KEY", "LANGSMITH_API_KEY",
    }
    env = {key: value for key, value in os.environ.items() if key.upper() not in private}
    env["ASEPRITE_AI_HEADLESS"] = "0"
    env["ASEPRITE_AI_ALLOW_LUA"] = "0"
    return env


class AsepriteMCP:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.client: Client | None = None
        self.tools = {}

    async def __aenter__(self):
        command = shutil.which("npx.cmd" if os.name == "nt" else "npx")
        if not command:
            raise AsepriteError("npx를 찾을 수 없습니다. Node.js 설치와 PATH를 확인해주세요.")
        params = StdioServerParameters(
            command=command,
            args=["-y", self.settings.mcp_package, "serve"],
            env=server_environment(),
            cwd=self.settings.root,
        )
        self.client = Client(params, read_timeout_seconds=self.settings.mcp_timeout)
        await self.client.__aenter__()
        try:
            cursor = None
            while True:
                listing = await self.client.list_tools(cursor=cursor)
                for tool in listing.tools:
                    if tool.name in self.tools:
                        raise AsepriteError(f"중복 MCP 도구 이름: {tool.name}")
                    self.tools[tool.name] = tool
                cursor = listing.next_cursor
                if not cursor:
                    break
        except BaseException:
            await self.client.__aexit__(None, None, None)
            raise
        return self

    async def __aexit__(self, exc_type, exc_value, traceback):
        if self.client is not None:
            # 호출자의 오류를 MCP 내부 TaskGroup의 ExceptionGroup으로 바꾸지 않습니다.
            await self.client.__aexit__(None, None, None)

    async def call(self, name: str, arguments: dict | None = None):
        if self.client is None or name not in self.tools:
            raise AsepriteError(f"사용할 수 없는 MCP 도구입니다: {name}")
        result = await self.client.call_tool(
            name, arguments or {}, read_timeout_seconds=self.settings.mcp_timeout
        )
        if result.is_error:
            raise AsepriteToolError(f"{name} 실패: {text_content(result)[:1200]}")
        return result

    async def preflight(self, *, require_ready: bool = False) -> dict:
        state = payload(await self.call("preflight"))
        if require_ready and (not state.get("ready") or state.get("mode") != "live"):
            raise AsepriteError(
                "Aseprite live 연결이 준비되지 않았습니다. Aseprite와 확장 권한을 확인해주세요."
            )
        return state

    async def wait_until_ready(self, timeout: float = 10) -> dict:
        """확장의 시작·재연결만 기다립니다. 문서 편집은 재시도하지 않습니다."""
        state = {}
        try:
            async with asyncio.timeout(timeout):
                while True:
                    state = await self.preflight()
                    if state.get("mode") != "live":
                        raise AsepriteError("Aseprite가 live 모드가 아닙니다. 자동으로 모드를 변경하지 않습니다.")
                    if state.get("ready"):
                        return state
                    await asyncio.sleep(0.5)
        except TimeoutError:
            if not state.get("bridgeConnected"):
                message = "Aseprite MCP 브리지에 연결되지 않았습니다. PC 터미널의 MCP 실행 오류를 확인해주세요."
            elif not state.get("pluginConnected"):
                message = ("Aseprite 확장이 브리지에 연결되지 않았습니다. 열린 작업을 저장한 뒤 "
                           "Aseprite를 완전히 종료하고 다시 실행해주세요. 확장의 연결 권한 창이 나오면 허용해주세요.")
            else:
                message = "Aseprite 확장 응답 준비 시간이 초과됐습니다. Aseprite의 대화상자와 실행 상태를 확인해주세요."
            raise AsepriteError(message) from None

    async def craft_notes(self) -> str:
        notes = []
        for uri in ("skill://draw", "skill://palette", "rules://index"):
            try:
                result = await self.client.read_resource(uri)
                for part in result.contents:
                    text = getattr(part, "text", None)
                    if text:
                        notes.append(f"[{uri}]\n{text[:6000]}")
            except Exception:
                # 리소스가 없는 서버도 핵심 도구만으로 사용할 수 있습니다.
                continue
        return "\n\n".join(notes)


async def get_status(settings: Settings) -> str:
    async with asyncio.timeout(settings.mcp_timeout + 30):
        async with AsepriteMCP(settings) as mcp:
            state = await mcp.preflight()
    sprite = state.get("activeSprite")
    return "\n".join([
        f"연결 상태: {'정상' if state.get('ready') else '연결 준비 안 됨'}",
        f"모드: {state.get('mode', '알 수 없음')}",
        f"Aseprite 버전: {state.get('asepriteVersion') or '알 수 없음'}",
        f"확장 버전: {state.get('extensionVersion') or '알 수 없음'}",
        f"현재 열린 문서: {sprite.get('name', '이름 없음') if sprite else '없음'}",
        f"안내: {state.get('directive', '')}",
    ])
