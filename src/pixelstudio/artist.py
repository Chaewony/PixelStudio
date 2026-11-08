"""프로젝트 API 인증과 로컬 MCP만 사용하는 픽셀아트 제작 경로."""

import asyncio
from dataclasses import dataclass, field
from pathlib import Path

from .artifacts import Artifacts
from .config import Settings
from .harness import ExecutionLimitError, JobLedger
from .mcp_client import AsepriteError, AsepriteMCP
from .model_client import APIRequestError, model_session
from .workflow import ProgressCallback, WorkflowRuntime, run_workflow


@dataclass
class CreationResult:
    job_id: str
    png: Path
    preview: Path
    source: Path
    status: str
    revisions: int = 0
    unresolved: list[dict] = field(default_factory=list)

    def __iter__(self):
        # 기존 CLI에서 사용하는 4개 값의 순서도 유지합니다.
        return iter((self.job_id, self.png, self.preview, self.source))


async def create_sprite(brief: str, settings: Settings | None = None,
                        *, on_progress: ProgressCallback | None = None):
    settings = settings or Settings.load()
    # 인증 누락 시 Aseprite에 빈 문서를 만들기 전에 실패합니다.
    settings.require_api()
    if not brief.strip() or len(brief) > 500:
        raise ValueError("그림 설명은 1~500자로 입력해주세요.")
    artifacts = Artifacts.create(settings.root)
    record = {"job_id": artifacts.job_id, "agent": "multi-agent", "model": settings.model,
              "provider": settings.provider,
              "brief": brief, "status": "running"}
    artifacts.report(record)
    ledger = JobLedger(settings)
    runtime = None
    try:
        async with asyncio.timeout(settings.job_timeout):
            async with model_session(settings) as model, AsepriteMCP(settings) as mcp:
                runtime = WorkflowRuntime(settings, model, mcp, artifacts, ledger, on_progress)
                runtime.persist({"job_id": artifacts.job_id, "brief": brief, "status": "running",
                                 "stage": "connection", "revision": 0, "snapshots": [], "reviews": []})
                ledger.event("connection", "started")
                if on_progress:
                    try:
                        await on_progress("Aseprite · 확장 연결을 확인하고 있습니다.")
                    except Exception:
                        pass
                # 유료 Planner 요청 전에 연결을 확인합니다. 편집 직전 검사도 그대로 유지합니다.
                await mcp.wait_until_ready()
                ledger.event("connection", "finished")
                result = await run_workflow(runtime, brief)
                record.update(result)
    except BaseException as exc:
        record["status"] = "cancelled" if isinstance(exc, asyncio.CancelledError) else "failed"
        record["error_type"] = type(exc).__name__
        if isinstance(exc, APIRequestError):
            record.update(error=str(exc), error_code=exc.code, http_status=exc.status_code)
        elif isinstance(exc, (ExecutionLimitError, AsepriteError)):
            record["error"] = str(exc)
        raise
    finally:
        if runtime:
            # 실패해도 마지막 단계·검토·미리보기와 누적 사용량을 보존합니다.
            record.update({key: value for key, value in runtime.last_state.items()
                           if key not in {"status", "stage"}})
            record["stage"] = runtime.last_state.get("stage", "setup")
            tools = runtime.tools
            record["tool_calls"] = tools.call_count if tools else 0
            record["tools"] = tools.trace if tools else []
            runtime.persist({**runtime.last_state, "status": record["status"]})
        record.update(ledger.report())
        artifacts.report(record)
    return CreationResult(artifacts.job_id, artifacts.png, artifacts.preview, artifacts.source,
                          result["status"], result["revision"], result["unresolved"])
