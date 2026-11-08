"""에이전트 사이에서 교환할 제작 명세와 검토 결과."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class Requirement(StrictModel):
    id: str = Field(pattern=r"^req-[1-9][0-9]*$")
    description: str = Field(min_length=1, max_length=500)
    source_quote: str = Field(min_length=1, max_length=500)


class Plan(StrictModel):
    summary: str = Field(min_length=1, max_length=1000)
    requirements: list[Requirement] = Field(min_length=1, max_length=8)
    approach: str = Field(min_length=1, max_length=2000)

    @model_validator(mode="after")
    def unique_ids(self):
        ids = [item.id for item in self.requirements]
        if len(ids) != len(set(ids)):
            raise ValueError("requirement의 id는 중복될 수 없습니다.")
        return self


class Check(StrictModel):
    criterion_id: str = Field(min_length=1, max_length=40)
    status: Literal["pass", "fail", "uncertain"]
    evidence: str = Field(min_length=1, max_length=1000)
    correction: str = Field(default="", max_length=1000)

    @model_validator(mode="after")
    def actionable_failure(self):
        if self.status == "fail" and not self.correction.strip():
            raise ValueError("fail 판정에는 실행 가능한 correction이 필요합니다.")
        return self


class Review(StrictModel):
    summary: str = Field(min_length=1, max_length=1000)
    checks: list[Check] = Field(min_length=1, max_length=9)
    suggestions: list[str] = Field(default_factory=list, max_length=3)

    @model_validator(mode="after")
    def unique_checks(self):
        ids = [item.criterion_id for item in self.checks]
        if len(ids) != len(set(ids)):
            raise ValueError("같은 criterion을 중복 판정할 수 없습니다.")
        return self


def criteria_for(plan: dict, brief: str) -> list[dict]:
    # Planner가 요청 일부를 놓쳐도 Reviewer가 원본 요청 전체를 다시 확인합니다.
    return [{"id": "user-request", "description": brief, "source_quote": brief},
            *plan["requirements"]]


def validate_plan(plan: Plan, brief: str) -> None:
    if any(item.source_quote not in brief for item in plan.requirements):
        raise ValueError("source_quote는 사용자 요청에서 그대로 인용해야 합니다.")


def validate_review(review: Review, criteria: list[dict]) -> None:
    expected = {item["id"] for item in criteria}
    if {item.criterion_id for item in review.checks} != expected:
        raise ValueError("고정된 모든 criterion을 한 번씩만 검사해야 합니다. 새 기준을 추가할 수 없습니다.")
