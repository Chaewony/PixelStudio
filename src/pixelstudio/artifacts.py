"""모델이 주장한 결과 대신 실제 파일을 검사합니다."""

from dataclasses import dataclass
import json
from pathlib import Path
import struct
from uuid import uuid4

from PIL import Image


class ArtifactError(RuntimeError):
    pass


class ImageContentError(ArtifactError):
    """파일은 정상이나 빈 그림·불투명 배경 등 수정 가능한 내용 오류."""


@dataclass(frozen=True)
class Artifacts:
    job_id: str
    directory: Path

    @classmethod
    def create(cls, root: Path) -> "Artifacts":
        job_id = uuid4().hex
        directory = root / "outputs" / job_id
        directory.mkdir(parents=True, exist_ok=False)
        return cls(job_id, directory)

    @property
    def png(self) -> Path:
        return self.directory / "sprite.png"

    @property
    def source(self) -> Path:
        return self.directory / "sprite.aseprite"

    @property
    def preview(self) -> Path:
        return self.directory / "preview.png"

    def report(self, data: dict) -> None:
        (self.directory / "report.json").write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def validate_and_preview(self, *, check_content: bool = True) -> None:
        for path in (self.png, self.source):
            if not path.is_file() or path.stat().st_size == 0:
                raise ArtifactError(f"결과 파일이 없습니다: {path.name}")
        with self.source.open("rb") as stream:
            header = stream.read(14)
        if len(header) != 14:
            raise ArtifactError("Aseprite 원본 파일의 헤더가 올바르지 않습니다.")
        size, magic, frames, width, height, _depth = struct.unpack("<IHHHHH", header)
        if magic != 0xA5E0 or frames != 1 or (width, height) != (32, 32):
            raise ArtifactError("원본 파일이 32×32, 1프레임 Aseprite 문서가 아닙니다.")
        if size != self.source.stat().st_size:
            raise ArtifactError("Aseprite 파일이 완전히 저장되지 않았습니다.")
        with Image.open(self.png) as image:
            if image.format != "PNG" or image.size != (32, 32):
                raise ArtifactError("32×32 PNG 파일이 아닙니다.")
            rgba = image.convert("RGBA")
            minimum, maximum = rgba.getchannel("A").getextrema()
            if check_content and maximum == 0:
                raise ImageContentError("내보낸 이미지가 완전히 투명합니다.")
            if check_content and minimum != 0:
                raise ImageContentError("투명 배경이 확인되지 않았습니다.")
            rgba.resize((256, 256), Image.Resampling.NEAREST).save(self.preview)


async def create_document(mcp) -> str:
    from .mcp_client import AsepriteError, payload

    await mcp.preflight(require_ready=True)
    created = payload(await mcp.call("sprite_manage", {
        "op": "new", "width": 32, "height": 32, "colorMode": "rgb",
    }))
    document_id = created.get("id")
    if not isinstance(document_id, int):
        raise AsepriteError("새 문서의 고유 ID를 확인할 수 없습니다.")
    sprite = f"#{document_id}"
    await verify_document(mcp, sprite)
    return sprite


async def verify_document(mcp, sprite: str) -> None:
    from .mcp_client import payload

    info = payload(await mcp.call("sprite_info", {"sprite": sprite}))
    if (info.get("width"), info.get("height"), info.get("frameCount")) != (32, 32, 1):
        raise ArtifactError("작업 문서가 32×32, 1프레임 명세를 벗어났습니다.")


async def export_document(mcp, sprite: str, artifacts: Artifacts, *, check_content: bool = True) -> None:
    await verify_document(mcp, sprite)
    await mcp.call("sprite_manage", {
        "op": "save_as", "sprite": sprite, "path": artifacts.source.as_posix(),
    })
    await mcp.call("export", {
        "op": "png", "sprite": sprite, "frame": 1, "scale": 1,
        "trim": False, "path": artifacts.png.as_posix(),
    })
    artifacts.validate_and_preview(check_content=check_content)
