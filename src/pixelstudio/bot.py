"""기존 Discord 명령을 유지하면서 API 기반 Artist를 실행합니다."""

import asyncio
import logging

import discord
from discord import app_commands

from .artist import create_sprite
from .config import Settings
from .mcp_client import get_status


logger = logging.getLogger(__name__)


def can_draw(settings: Settings, guild_id: int | None, channel_id: int | None,
             user_id: int) -> bool:
    if guild_id != int(settings.guild_id):
        return False
    if settings.allowed_channel_id:
        return channel_id == int(settings.allowed_channel_id)
    return user_id == int(settings.owner_id)


def create_bot(settings: Settings) -> discord.Client:
    settings.require_discord()
    guild = discord.Object(id=int(settings.guild_id))

    class PixelStudioClient(discord.Client):
        def __init__(self):
            super().__init__(intents=discord.Intents.default(),
                             allowed_mentions=discord.AllowedMentions.none())
            self.tree = app_commands.CommandTree(self)
            self.job_lock = asyncio.Lock()

        async def setup_hook(self):
            commands = await self.tree.sync(guild=guild)
            logger.info("서버 %s에 명령 %s개 등록 완료", guild.id, len(commands))

        async def on_ready(self):
            logger.info("로그인 완료: %s · /ping /aseprite /draw", self.user)

    client = PixelStudioClient()

    @client.tree.command(name="ping", description="PixelStudio 연결을 확인합니다.", guild=guild)
    async def ping(interaction: discord.Interaction):
        await interaction.response.send_message("Pong! 🟢 PixelStudio 연결 성공")

    @client.tree.command(name="aseprite", description="Aseprite MCP 연결 상태를 직접 확인합니다.", guild=guild)
    async def aseprite_status(interaction: discord.Interaction):
        if interaction.user.id != int(settings.owner_id):
            await interaction.response.send_message("이 명령은 봇 소유자만 사용할 수 있습니다.", ephemeral=True)
            return
        if client.job_lock.locked():
            await interaction.response.send_message("다른 제작 작업을 처리 중입니다.", ephemeral=True)
            return
        async with client.job_lock:
            await interaction.response.defer(thinking=True, ephemeral=True)
            try:
                message = await get_status(settings)
            except Exception:
                logger.exception("Aseprite 연결 확인 실패")
                message = "연결 확인에 실패했습니다. PC 터미널과 Aseprite 실행 상태를 확인해주세요."
            await interaction.edit_original_response(content=message[:1900])

    @client.tree.command(name="draw", description="32×32 픽셀아트를 만들고 PNG를 받습니다.", guild=guild)
    @app_commands.describe(prompt="그릴 대상과 색상, 표정을 설명해주세요.")
    async def draw(interaction: discord.Interaction, prompt: app_commands.Range[str, 1, 500]):
        if not can_draw(settings, interaction.guild_id, interaction.channel_id, interaction.user.id):
            message = ("이 명령은 지정된 제작 채널에서만 사용할 수 있습니다."
                       if settings.allowed_channel_id else "이 명령은 봇 소유자만 사용할 수 있습니다.")
            await interaction.response.send_message(message, ephemeral=True)
            return
        if client.job_lock.locked():
            await interaction.response.send_message("다른 제작 작업을 처리 중입니다.", ephemeral=True)
            return
        async with client.job_lock:
            await interaction.response.defer(thinking=True)
            try:
                job_id, png, preview, source = await create_sprite(prompt, settings)
            except TimeoutError:
                await interaction.edit_original_response(content=(
                    "제작 시간이 초과되었습니다. 재요청 전에 Aseprite에 남은 작업을 확인해주세요."
                ))
                return
            except Exception:
                logger.exception("픽셀아트 제작 실패")
                await interaction.edit_original_response(content=(
                    "제작 또는 결과 확인에 실패했습니다. PC 터미널과 outputs의 report.json을 확인해주세요."
                ))
                return

            files = []
            try:
                paths = [preview, png, source]
                if any(path.stat().st_size > interaction.filesize_limit for path in paths):
                    raise ValueError("Discord 첨부 파일 한도를 초과했습니다.")
                files = [discord.File(path, filename=path.name) for path in paths]
                display_prompt = discord.utils.escape_mentions(discord.utils.escape_markdown(prompt))
                await interaction.edit_original_response(
                    content=(f"작업 완료 · `{job_id}`\n"
                             f"**요청 내용**\n{display_prompt}\n\n"
                             "32×32 원본 PNG · 8배 미리보기 · Aseprite 원본"),
                    attachments=files,
                )
            except (discord.HTTPException, OSError, ValueError):
                logger.exception("결과 전송 실패: %s · 저장 위치: %s", job_id, png.parent)
                try:
                    await interaction.edit_original_response(content=(
                        f"제작은 완료됐지만 파일 전송에 실패했습니다. 작업 ID: `{job_id}`. "
                        "PC의 outputs 폴더에 결과가 보관되어 있습니다."
                    ))
                except discord.HTTPException:
                    logger.exception("전송 실패 안내도 전송하지 못했습니다: %s", job_id)
            finally:
                for file in files:
                    file.close()

    return client


def run_bot(settings: Settings | None = None) -> None:
    settings = settings or Settings.load()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    create_bot(settings).run(settings.bot_token, log_handler=None)
