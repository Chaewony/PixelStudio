"""API 비용 없이 새 테스트 문서의 제작·저장 경로를 확인합니다."""

from .artifacts import Artifacts, create_document, export_document
from .mcp_client import AsepriteMCP
from .config import Settings


async def test_drawing(settings: Settings) -> Artifacts:
    artifacts = Artifacts.create(settings.root)
    async with AsepriteMCP(settings) as mcp:
        sprite = await create_document(mcp)
        await mcp.call("draw", {
            "sprite": sprite, "frame": 1,
            "ops": [{
                "kind": "grid", "x": 8, "y": 8,
                "rows": ["GGGGGGGGGGGGGGGG"] * 16,
                "legend": {"G": "#33aa55"},
            }],
        })
        await mcp.call("look", {"op": "preview", "sprite": sprite, "frame": 1, "scale": 8})
        await export_document(mcp, sprite, artifacts)
    artifacts.report({"kind": "mcp-smoke", "status": "completed", "sprite": sprite})
    return artifacts
