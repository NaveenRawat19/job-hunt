"""Request/response models."""
from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, EmailStr


class RegisterRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    username: str = Field(min_length=2, max_length=15)
    email: EmailStr
    password: str = Field(min_length=8, max_length=128)


class UserOut(BaseModel):
    id: int
    email: str
    created_at: str


class TokenOut(BaseModel):
    access_token: str
    token_type: Literal["bearer"] = "bearer"
    idle_timeout_seconds: int
    expires_at: str = Field(description="Hard session limit; the idle timeout usually ends it sooner.")
    has_profile: bool = Field(description="False until a resume has been uploaded (POST /resume).")


class MeOut(UserOut):
    idle_timeout_seconds: int
    has_profile: bool


class Profile(BaseModel):
    """Same shape as profile.json. Unknown keys from the LLM are dropped."""
    model_config = ConfigDict(extra="ignore")

    name: str = ""
    current_title: str = ""
    years_experience: float = Field(0, ge=0, le=60)
    core_skills: list[str] = []
    domains: list[str] = []
    notable_projects: list[str] = []
    education: str = ""
    target_titles: list[str] = []
    seniority: str = ""


class ProfileUpdate(BaseModel):
    """PUT /profile: only the fields you send are changed."""
    model_config = ConfigDict(extra="forbid")

    name: str | None = None
    current_title: str | None = None
    years_experience: float | None = Field(None, ge=0, le=60)
    core_skills: list[str] | None = None
    domains: list[str] | None = None
    notable_projects: list[str] | None = None
    education: str | None = None
    target_titles: list[str] | None = None
    seniority: str | None = None


class ResumeInfo(BaseModel):
    id: int = Field(description="Use with GET/DELETE /resumes/{id}.")
    filename: str
    media_type: str
    size: int = Field(description="Bytes.")
    sha256: str
    uploaded_at: str


class ResumeListItem(ResumeInfo):
    is_current: bool = Field(description="The newest upload, the one GET /resume returns and reextract uses.")


class ProfileOut(BaseModel):
    profile: Profile
    source_file: str | None
    updated_at: str
    resume: ResumeInfo | None = Field(
        None, description="The newest uploaded resume (download with GET /resume); null if none is stored.")


class SkillStat(BaseModel):
    skill: str
    category: str
    jobs: int = Field(description="Postings in the sample that mention it.")
    percent: float = Field(description="jobs / sample size, 0-100.")
    in_profile: bool | None = Field(None, description="Whether your profile lists it; null with no profile.")


class RoleCount(BaseModel):
    role: str
    jobs: int


class SkillInsights(BaseModel):
    sample_size: int = Field(description="Stored postings analysed. Percentages are out of this.")
    with_description: int = Field(description="Of those, how many had any description text.")
    truncated: bool = Field(description="True if more postings matched than were analysed.")
    note: str | None = None
    roles: list[RoleCount] = Field(description="Role families in the filtered sample, before `role` is applied.")
    skills: list[SkillStat]
    gaps: list[str] = Field(description="Top skills in demand that your profile does not list.")


class StudyPlanRequirement(BaseModel):
    """What a user wants from a study plan. Stored as submitted; nothing is generated yet."""
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    field: str = Field(min_length=1, max_length=100, examples=["tech"])
    field_name: str | None = Field(None, max_length=100, description="Name of the field when it is not one of the presets.")
    goal: str = Field(min_length=1, max_length=1000)
    level: str = Field(min_length=1, max_length=50, examples=["intermediate"])
    weak_areas: str = Field("", max_length=1000)
    duration_value: int = Field(ge=1, le=1000)
    duration_unit: Literal["days", "weeks", "months"]
    hours_per_day: float = Field(gt=0, le=24)
    review_days: bool = False
    tracks: list[Annotated[str, Field(min_length=1, max_length=100)]] = Field(
        min_length=1, max_length=30, description="Subjects to cover; the plan labels every task with one of them.")
    track_mode: str = Field("all", max_length=50)
    resources: str = Field("", max_length=5000, description="Free text; one resource per line.")
    free_only: bool = False
    task_styles: list[Annotated[str, Field(min_length=1, max_length=100)]] = Field([], max_length=30)
    extra: str = Field("", max_length=2000)


class StudyPlanPlanTask(BaseModel):
    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)

    track: str = Field(min_length=1, max_length=100)
    task: str = Field(min_length=1, max_length=5000)


class StudyPlanPlanDay(BaseModel):
    model_config = ConfigDict(extra="ignore")

    day: int = Field(ge=1)
    tasks: list[StudyPlanPlanTask] = Field(min_length=1, max_length=30)


class StudyPlanPlanWeek(BaseModel):
    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)

    week: int = Field(ge=1)
    title: str = Field(min_length=1, max_length=200)
    days: list[StudyPlanPlanDay] = Field(min_length=1, max_length=7)


class StudyPlanJson(BaseModel):
    """Structured equivalent of the planner app's week/day/task format."""
    model_config = ConfigDict(extra="ignore")

    weeks: list[StudyPlanPlanWeek] = Field(min_length=1, max_length=1000)


class StudyPlanText(BaseModel):
    model_config = ConfigDict(extra="ignore")

    plan: Annotated[str, Field(min_length=1, max_length=500_000)] | StudyPlanJson = Field(
        description="Planner-format text (### Week N: / **Day N** / - [ ] **TRACK:** ...), "
                    "structured JSON with weeks/days/tasks, or JSON text of either.")


class StudyPlanImport(StudyPlanRequirement):
    """Requirements plus a ready-made text or structured plan to store (no LLM)."""

    plan: str | StudyPlanJson = Field(
        description="The plan as planner-format text or structured JSON with weeks, days, and tasks.")


class StudyPlanOut(StudyPlanRequirement):
    id: int
    created_at: str
    plan: str | None = Field(
        None, description="The plan as plain text in the planner app's import format; "
                          "null if generation failed (the requirements are still stored).")


class SearchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    limit: int | None = Field(None, ge=1, le=300,
                              description="Max new jobs to store in this run (newest first).")
    companies: list[str] | None = Field(None, description="Only these companies, by name.")


class RunOut(BaseModel):
    run_id: str
    status: Literal["running", "succeeded", "failed"]
    stage: str | None
    message: str
    error: str | None
    counts: dict | None
    warnings: list[str]
    started_at: str
    finished_at: str | None


class JobOut(BaseModel):
    job_id: str
    company: str
    title: str
    location: str
    url: str
    posted_at: str | None
    salary: str | None
    first_seen: str


class JobDetail(JobOut):
    description: str


class AppliedJob(JobOut):
    applied_on: str


class AppliedJobList(BaseModel):
    total: int
    limit: int
    offset: int
    items: list[AppliedJob]


class StatsOut(BaseModel):
    tracked: int = Field(description="Jobs in the shared catalog.")
    applied: int | None = Field(None, description="Jobs you applied to; null when not signed in.")


class JobList(BaseModel):
    total: int
    limit: int
    offset: int
    items: list[JobOut]


class MatchOut(BaseModel):
    job_id: str
    company: str
    title: str
    location: str
    url: str
    posted_at: str | None
    salary: str | None
    status: Literal["new", "applied"]
    first_seen: str
    match_percent: float = Field(description="Share of the job's recognised skills that your profile has, 0-100.")
    matched_skills: list[str]
    missing_skills: list[str]


class MatchList(BaseModel):
    total: int
    limit: int
    offset: int
    truncated: bool = Field(description="True if more jobs are stored than were compared.")
    resume_skills: list[str] = Field(description="Vocabulary skills found in your profile.")
    items: list[MatchOut]
