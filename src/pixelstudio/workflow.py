"""같은 문서를 순서대로 편집하는 LangGraph 검토·수정 워크플로."""

import asyncio
from collections.abc import Awaitable, Callable
import hashlib
import json
import logging
from pathlib import Path
import shutil
from typing import TypedDict

from langgraph.graph import END, START, StateGraph

from .agent import AgentError, ArtistTools, run_artist
from .artifacts import Artifacts, ImageContentError, create_document, export_document
from .harness import JobLedger, MeteredModel
from .roles import make_plan, review_image, role_prompt


logger = logging.getLogger(__name__)
ProgressCallback = Callable[[str], Awaitable[None]]


class JobState(TypedDict, total=False):
    job_id: str
    brief: str
    stage: str
    status: str
    plan: dict
    sprite: str
    revision: int
    summary: str
    candidate: dict
    snapshots: list[dict]
    technical_errors: list[dict]
    review: dict
    reviews: list[dict]
    unresolved: list[dict]
    stop_reason: str


STAGE_LABELS = {
    "planner": "Planner · 요청을 분석하고 제작 기준을 정하고 있습니다.",
    "artist": "Artist · 픽셀아트를 제작하고 있습니다.",
    "verify": "Python · 원본과 미리보기를 저장하고 검사하고 있습니다.",
    "reviewer": "Reviewer · 실제 이미지와 요청 내용을 비교하고 있습니다.",
    "fixer": "Fixer · 검토에서 발견한 문제를 수정하고 있습니다.",
    "finish": "검토 결과와 파일을 정리하고 있습니다.",
}


class WorkflowRuntime:
    """모델·MCP 연결은 그래프 상태 밖에 두고 한 작업 동안 공유합니다."""

    def __init__(self, settings, model, mcp, artifacts: Artifacts, ledger: JobLedger,
                 on_progress: ProgressCallback | None = None):
        self.settings, self.model, self.mcp = settings, model, mcp
        self.artifacts, self.ledger, self.on_progress = artifacts, ledger, on_progress
        self.tools = None
        self.notes = ""
        self.last_state: JobState = {}

    def persist(self, state):
        self.last_state = dict(state)
        path = self.artifacts.directory / "state.json"
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(path)

    async def notify(self, stage, state):
        if self.on_progress:
            label = STAGE_LABELS[stage]
            if stage == "fixer":
                label += f" ({state['revision'] + 1}/{self.settings.max_revisions}회)"
            try:
                await self.on_progress(label)
            except Exception:
                # Discord 통신 문제로 완료된 편집을 재실행하지 않습니다.
                logger.warning("진행 상황 전송 실패: %s", stage)

    def node(self, stage, action):
        async def execute(state):
            current = {**state, "stage": stage}
            self.persist(current)
            self.ledger.event(stage, "started", revision=state["revision"])
            await self.notify(stage, state)
            try:
                changes = await action(state)
            except BaseException as exc:
                status = "cancelled" if isinstance(exc, asyncio.CancelledError) else "failed"
                self.persist({**self.last_state, "status": status})
                self.ledger.event(stage, "failed", error_type=type(exc).__name__)
                raise
            self.ledger.event(stage, "finished", revision=changes.get("revision", state["revision"]))
            self.persist({**current, **changes})
            return {"stage": stage, **changes}
        return execute

    async def planner(self, state):
        plan = await make_plan(MeteredModel(self.model, self.ledger, "planner"), self.settings, state["brief"])
        (self.artifacts.directory / "plan.json").write_text(
            json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")
        return {"plan": plan}

    async def artist(self, state):
        sprite = await create_document(self.mcp)
        # 문서 생성 뒤 오류가 발생해도 고유 ID를 기록합니다.
        self.persist({**self.last_state, "sprite": sprite})
        self.tools = ArtistTools(self.mcp, sprite, self.settings.max_tool_calls)
        if "aseprite_draw" not in self.tools.schemas:
            raise AgentError("MCP 서버에 그리기 도구가 없습니다.")
        self.notes = await self.mcp.craft_notes()
        prompt = role_prompt("artist") + "\n\n제작 리소스:\n" + self.notes
        brief = json.dumps({"사용자_요청": state["brief"], "제작_명세": state["plan"]}, ensure_ascii=False)
        result = await run_artist(MeteredModel(self.model, self.ledger, "artist"), self.settings,
                                  self.tools, prompt, brief)
        return {"sprite": sprite, "summary": result.text}

    async def verify(self, state):
        directory = self.artifacts.directory / "iterations" / f"revision-{state['revision']:02d}"
        directory.mkdir(parents=True, exist_ok=False)
        snapshot = Artifacts(self.artifacts.job_id, directory)
        # 구조가 정상인 파일은 내용 검사에 실패해도 수정 전 증거로 보관합니다.
        await export_document(self.mcp, state["sprite"], snapshot, check_content=False)
        errors = []
        try:
            snapshot.validate_and_preview()
        except ImageContentError as exc:
            errors = [{"criterion_id": "technical-image", "status": "fail", "evidence": str(exc),
                       "correction": "그림이 보이는 픽셀과 투명 배경이 함께 있는 정상 이미지로 수정하세요."}]
        candidate = {"revision": state["revision"], "directory": str(directory),
                     "png_sha256": hashlib.sha256(snapshot.png.read_bytes()).hexdigest(),
                     "technical_valid": not errors}
        return {"candidate": candidate, "snapshots": [*state["snapshots"], candidate],
                "technical_errors": errors, "review": {}}

    async def reviewer(self, state):
        candidate = Artifacts(self.artifacts.job_id, Path(state["candidate"]["directory"]))
        review = await review_image(MeteredModel(self.model, self.ledger, "reviewer"), self.settings,
                                    state["brief"], state["plan"], candidate.png, candidate.preview)
        entry = {"revision": state["revision"], "png_sha256": state["candidate"]["png_sha256"], **review}
        (candidate.directory / "review.json").write_text(
            json.dumps(entry, ensure_ascii=False, indent=2), encoding="utf-8")
        return {"review": review, "reviews": [*state["reviews"], entry]}

    def pending(self, state):
        if state["technical_errors"]:
            return state["technical_errors"]
        return [check for check in state.get("review", {}).get("checks", []) if check["status"] != "pass"]

    def after_verify(self, state):
        if state["technical_errors"]:
            return "fixer" if state["revision"] < self.settings.max_revisions else "finish"
        return "reviewer"

    def after_review(self, state):
        pending = self.pending(state)
        # 불명확한 이미지를 추측으로 수정하지 않습니다.
        if not pending or any(check["status"] == "uncertain" for check in pending):
            return "finish"
        return "fixer" if state["revision"] < self.settings.max_revisions else "finish"

    async def fixer(self, state):
        self.tools.role = "fixer"
        brief = json.dumps({"사용자_요청": state["brief"], "고정_제작_명세": state["plan"],
                            "필수_수정": self.pending(state), "수정_회차": state["revision"] + 1},
                           ensure_ascii=False)
        prompt = role_prompt("fixer") + "\n\n제작 리소스:\n" + self.notes
        result = await run_artist(MeteredModel(self.model, self.ledger, "fixer"), self.settings,
                                  self.tools, prompt, brief)
        return {"revision": state["revision"] + 1, "summary": result.text}

    async def finish(self, state):
        pending = self.pending(state)
        passed = not pending and bool(state.get("review", {}).get("checks"))
        source = Path(state["candidate"]["directory"])
        for name in ("sprite.png", "sprite.aseprite", "preview.png"):
            shutil.copyfile(source / name, self.artifacts.directory / name)
        status = "completed" if passed else "needs_review"
        reason = "passed" if passed else ("uncertain" if any(
            check["status"] == "uncertain" for check in pending) else "revision_limit")
        return {"status": status, "unresolved": pending, "stop_reason": reason}


def build_workflow(runtime: WorkflowRuntime):
    graph = StateGraph(JobState)
    for name in STAGE_LABELS:
        graph.add_node(name, runtime.node(name, getattr(runtime, name)))
    graph.add_edge(START, "planner")
    graph.add_edge("planner", "artist")
    graph.add_edge("artist", "verify")
    graph.add_conditional_edges("verify", runtime.after_verify,
                                {"reviewer": "reviewer", "fixer": "fixer", "finish": "finish"})
    graph.add_conditional_edges("reviewer", runtime.after_review, {"fixer": "fixer", "finish": "finish"})
    graph.add_edge("fixer", "verify")
    graph.add_edge("finish", END)
    return graph.compile()


async def run_workflow(runtime: WorkflowRuntime, brief: str) -> JobState:
    initial: JobState = {"job_id": runtime.artifacts.job_id, "brief": brief, "status": "running",
                         "revision": 0, "snapshots": [], "reviews": [], "technical_errors": []}
    return await build_workflow(runtime).ainvoke(
        initial, config={"recursion_limit": 16 + 8 * runtime.settings.max_revisions})
