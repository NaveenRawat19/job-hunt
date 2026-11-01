"""FastAPI app: accounts, resume upload, profile, and the job-hunt run.

    POST /auth/register   create an account
    POST /auth/login      email + password -> JWT (form-encoded, OAuth2 password flow)
    POST /auth/logout     end the session;  GET /auth/me  who am I
    POST /resume          upload a PDF/TXT/MD -> extracts your profile; every upload is kept
    GET /resume           download the newest resume;  GET /resumes lists all of them
    GET|DELETE /resumes/{id}   download or delete one of them
    POST /resume/reextract     rebuild the profile from the newest stored resume
    GET/PUT /profile      read or correct the stored profile
    POST /study-plan      store study plan requirements;  GET /study-plan the latest
    POST /search          OPERATOR ONLY (X-API-Key = SEARCH_API_KEY): scrape the portals, for a cron
    GET /runs/latest|{id} OPERATOR ONLY: follow a scrape
    GET /jobs             PUBLIC: jobs from the last 24h; ?window=1h needs sign-in
    GET /matches          compare the last 24h of jobs with your resume's keywords, best first
    POST /jobs/{id}/applied, GET /jobs/applied   the only per-user job data (user_jobs)

Jobs older than JOB_RETENTION_HOURS (24) are deleted. /jobs, /jobs/{id}, /stats and
/insights/skills need no sign-in.
Everything except /health and /auth/register|login needs `Authorization: Bearer <jwt>`.
Sessions end after SESSION_IDLE_MINUTES (30) without a request, whatever the JWT's
own expiry says; SESSION_MAX_HOURS (12) caps a session however active it is.
"""
from __future__ import annotations

import hashlib
import hmac
import io
import asyncio
import logging
import os
import threading
from collections import Counter
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal
from urllib.parse import quote

import jwt
import yaml
from fastapi import BackgroundTasks, Depends, FastAPI, Header, HTTPException, Query, Response, UploadFile
from starlette.concurrency import run_in_threadpool
from fastapi.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm
from pydantic import ValidationError

from .. import llm
from .. import skills as skills_mod
from .. import studyplan
from ..pipeline import PipelineError, run_pipeline
from ..providers import LLMError, resolve
from .auth import (AuthNotConfigured, AuthSettings, burn_password_check, create_token,
                   decode_token, load_settings)
from .db import Database, RunBlocked
from ..services.auth_service import AuthService
from .schemas import (AppliedJob, AppliedJobList, JobDetail, JobList, MatchList, MeOut, Profile,
                      ProfileOut, ProfileUpdate,
                      RegisterRequest, ResumeListItem, RunOut, SearchRequest, SkillInsights, StatsOut,
                      StudyPlanImport, StudyPlanRequirement, StudyPlanOut, StudyPlanText, TokenOut, UserOut)
from fastapi.middleware.cors import CORSMiddleware
from dotenv import load_dotenv

load_dotenv()

log = logging.getLogger("jobhunt.api")
ROOT = Path(__file__).resolve().parent.parent.parent

RESUME_EXTENSIONS = {
    ".pdf": "application/pdf",
    ".txt": "text/plain; charset=utf-8",
    ".md": "text/markdown; charset=utf-8",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
}

MAX_RESUME_BYTES = 5 * 1024 * 1024
MAX_PASSWORD_LEN = 128
INSIGHTS_MAX_JOBS = 2000


def check_plan(plan: str, requirements: dict) -> None:
    """422 with the first few format problems if `plan` doesn't fit its requirements."""
    problems = studyplan.validate_plan(plan, requirements)
    if problems:
        shown = problems[:studyplan.MAX_PROBLEMS_SHOWN]
        raise HTTPException(422, "plan does not match the expected format: " + "; ".join(shown)
                            + (f"; and {len(problems) - len(shown)} more" if len(problems) > len(shown) else ""))
MAX_PAGE_SIZE = 500

BEARER = {"WWW-Authenticate": "Bearer"}

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/auth/login")
optional_oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/auth/login", auto_error=False)


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, default))
    except ValueError:
        return default


def _safe_filename(raw: str | None) -> str:
    """Client-supplied names are only labels: no paths, no control characters."""
    name = Path((raw or "resume").replace("\\", "/")).name
    name = "".join(ch for ch in name if ch.isprintable() and ch not in '"<>|:*?').strip(" .")
    return name[:150] or "resume"


def _attachment(filename: str) -> str:
    ascii_name = filename.encode("ascii", "replace").decode().replace("?", "_")
    return f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(filename)}"


def create_app(db_path: str | Path | None = None, config_path: str | Path | None = None) -> FastAPI:
    database_url = os.getenv("DATABASE_URL")

    if not database_url:
        raise RuntimeError("DATABASE_URL environment variable is required")

    db = Database(database_url)
    config_file = Path(config_path or os.getenv("JOBHUNT_CONFIG", ROOT / "config.yaml"))

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        await db.init()
        await db.purge_expired()
        try:
            load_settings()
        except AuthNotConfigured as e:
            log.warning("%s: sign-in is disabled (503) until it is", e)
        try:
            yield
        finally:
            await db.close()

    app = FastAPI(title="jobhunt", version="2.0", lifespan=lifespan)

    origins = [
        "http://localhost:5173",
        "https://naveenrawat19.github.io",
    ]

    app.add_middleware(
        CORSMiddleware,
        allow_origins=origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.state.db = db
    plan_slots = threading.BoundedSemaphore(max(1, _env_int("MAX_CONCURRENT_PLANS", 2)))

    def load_cfg() -> dict:
        if not config_file.exists():
            raise HTTPException(500, f"config not found: {config_file.name}")
        return yaml.safe_load(config_file.read_text(encoding="utf-8")) or {}

    def load_companies(cfg: dict) -> list[dict]:
        path = Path(cfg.get("companies_file", "companies.yaml"))
        path = path if path.is_absolute() else config_file.parent / path
        if not path.exists():
            raise HTTPException(500, f"companies file not found: {path.name}")
        return (yaml.safe_load(path.read_text(encoding="utf-8")) or {}).get("companies") or []

    # --------------------------------------------------------------------- auth
    def auth_settings() -> AuthSettings:
        try:
            return load_settings()
        except AuthNotConfigured:
            raise HTTPException(503, "server auth is not configured (JWT_SECRET)")

    async def authenticate(token: str, settings: AuthSettings) -> dict:
        try:
            claims = decode_token(settings, token)
            user_id = int(claims["sub"])
            session_id = str(claims["sid"])
        except jwt.ExpiredSignatureError:
            raise HTTPException(401, "session expired", headers=BEARER)
        except (jwt.PyJWTError, ValueError):
            raise HTTPException(401, "invalid token", headers=BEARER)

        # This is the inactivity timeout: the JWT alone would stay valid for
        # SESSION_MAX_HOURS, so every request must also find a recently-used session.
        user = await db.touch_session(session_id, user_id, settings.idle_seconds)
        if not user:
            raise HTTPException(401, "session expired", headers=BEARER)
        return {**user, "session_id": session_id}

    async def current_user(token: str = Depends(oauth2_scheme),
                     settings: AuthSettings = Depends(auth_settings)) -> dict:
        return await authenticate(token, settings)

    async def optional_user(token: str | None = Depends(optional_oauth2_scheme)) -> dict | None:
        """For public endpoints that add detail when you are signed in."""
        return await authenticate(token, auth_settings()) if token else None

    def get_auth_service(settings: AuthSettings = Depends(auth_settings)) -> AuthService:
        return AuthService(db, settings)

    @app.post("/auth/register", response_model=UserOut, status_code=201, tags=["auth"])
    async def register(body: RegisterRequest, service: AuthService = Depends(get_auth_service)) -> dict:
        if os.getenv("ALLOW_REGISTRATION", "true").lower() in ("0", "false", "no"):
            raise HTTPException(403, "registration is closed")
        user = await service.register(body.username, body.email, body.password)
        if not user:
            raise HTTPException(409, "an account with this email already exists")
        return user

    @app.post("/auth/login", response_model=TokenOut, tags=["auth"])
    async def login(
        form: OAuth2PasswordRequestForm = Depends(),
        auth_service: AuthService = Depends(get_auth_service),
    ) -> dict:
        return await auth_service.login(form.username, form.password)

    @app.post("/auth/logout", status_code=204, tags=["auth"])
    async def logout(user: dict = Depends(current_user)) -> None:
        await db.revoke_session(user["session_id"])

    @app.get("/auth/me", response_model=MeOut, tags=["auth"])
    async def me(user: dict = Depends(current_user),
           settings: AuthSettings = Depends(auth_settings)) -> dict:
        """Also a cheap keep-alive: any authenticated call resets the idle timer."""
        return {**user, "idle_timeout_seconds": settings.idle_seconds,
                "has_profile": (await db.get_profile(user["id"])) is not None}

    # ------------------------------------------------------------------ health
    @app.get("/health")
    def health() -> dict:
        return {"status": "ok"}

    # ----------------------------------------------------------------- profile
    def extract_profile(data: bytes, ext: str) -> Profile:
        """Extract structured profile data from PDF, DOCX, TXT or Markdown."""
        is_pdf = ext == ".pdf"
        try:
            if ext == ".docx":
                try:
                    from docx import Document
                    document = Document(io.BytesIO(data))
                    parts = [p.text.strip() for p in document.paragraphs if p.text.strip()]
                    for table in document.tables:
                        for row in table.rows:
                            cells = [cell.text.strip() for cell in row.cells]
                            if any(cells):
                                parts.append(" | ".join(cells))
                    resume_text = "\\n".join(parts)
                    if not resume_text.strip():
                        raise HTTPException(422, "DOCX contains no extractable text")
                except HTTPException:
                    raise
                except Exception as e:
                    raise HTTPException(422, "invalid or unreadable DOCX file") from e
            elif is_pdf:
                resume_text = None
            else:
                resume_text = data.decode("utf-8-sig", errors="replace")

            provider, model = resolve("draft")
            raw = llm.build_profile(
                resume_bytes=data if is_pdf else None,
                resume_text=resume_text,
                is_pdf=is_pdf, provider=provider, model=model)
            return Profile.model_validate(raw)
        except LLMError as e:
            raise HTTPException(502, f"resume extraction failed: {e}")
        except (ValueError, ValidationError) as e:
            raise HTTPException(502, f"model returned an unusable profile: {e}")

    @app.post("/resume", response_model=ProfileOut, tags=["resume"])
    async def upload_resume(file: UploadFile, user: dict = Depends(current_user)) -> dict:
        """Upload a resume, extract its profile and retain the file as a version.
        Manually edited profile fields are preserved across subsequent uploads."""
        name = _safe_filename(file.filename)
        ext = Path(name).suffix.lower()
        if ext not in RESUME_EXTENSIONS:
            raise HTTPException(415, f"unsupported file type {ext or '(none)'}; use .pdf, .docx, .txt or .md")
        data = await file.read(MAX_RESUME_BYTES + 1)
        if len(data) > MAX_RESUME_BYTES:
            raise HTTPException(413, "resume is larger than 5 MB")
        if not data.strip():
            raise HTTPException(422, "resume file is empty")
        if ext == ".pdf" and not data.startswith(b"%PDF"):
            raise HTTPException(415, "file is not a valid PDF")
        if ext == ".docx" and not data.startswith(b"PK"):
            raise HTTPException(415, "file is not a valid DOCX document")
        # Checked before extraction so a refused upload does not spend LLM tokens.
        limit = _env_int("MAX_RESUMES_PER_USER", 50)
        if (await db.count_resumes(user["id"]) >= limit
                and not await db.has_resume_hash(user["id"], hashlib.sha256(data).hexdigest())):
            raise HTTPException(409, f"you already have {limit} stored resumes; "
                                     "delete some with DELETE /resumes/{id}")

        profile = await run_in_threadpool(extract_profile, data, ext)
        # Stored only after extraction succeeds, so a failed upload adds nothing.
        return await db.save_profile(user["id"], profile.model_dump(), name,
                               resume={"filename": name, "media_type": RESUME_EXTENSIONS[ext],
                                       "data": data})

    def resume_response(stored: dict | None) -> Response:
        if not stored:
            raise HTTPException(404, "no such resume; upload one with POST /resume")
        return Response(
            stored["data"], media_type=stored["media_type"],
            headers={"Content-Disposition": _attachment(stored["filename"]),
                     "X-Content-Type-Options": "nosniff",
                     "Cache-Control": "private, no-store"})

    download_docs = {200: {"content": {"application/pdf": {}, "text/plain": {}, "text/markdown": {}},
                           "description": "The resume file, as an attachment."}}

    @app.get("/resume", tags=["resume"], response_class=Response, responses=download_docs)
    async def download_resume(user: dict = Depends(current_user)) -> Response:
        """The newest uploaded resume."""
        return resume_response(await db.get_resume(user["id"]))

    @app.get("/resumes", response_model=list[ResumeListItem], tags=["resume"])
    async def list_resumes(user: dict = Depends(current_user)) -> list[dict]:
        """Every resume you have uploaded, newest first (metadata only)."""
        items = await db.list_resumes(user["id"])
        return [{**r, "is_current": i == 0} for i, r in enumerate(items)]

    @app.get("/resumes/{resume_id}", tags=["resume"], response_class=Response,
             responses=download_docs)
    async def download_resume_version(resume_id: int, user: dict = Depends(current_user)) -> Response:
        return resume_response(await db.get_resume(user["id"], resume_id))

    @app.delete("/resumes/{resume_id}", status_code=204, tags=["resume"])
    async def delete_resume(resume_id: int, user: dict = Depends(current_user)) -> None:
        """Delete one stored resume. The extracted profile and your jobs are kept."""
        if not await db.delete_resume(user["id"], resume_id):
            raise HTTPException(404, "no such resume")

    @app.post("/resume/reextract", response_model=ProfileOut, tags=["resume"])
    async def reextract_resume(user: dict = Depends(current_user)) -> dict:
        """Re-run profile extraction on the newest stored resume, e.g. after switching model.
        This overwrites the profile, including any corrections made with PUT /profile."""
        stored = await db.get_resume(user["id"])
        if not stored:
            raise HTTPException(404, "no stored resume; upload one with POST /resume")
        profile = await run_in_threadpool(
            extract_profile, stored["data"], Path(stored["filename"]).suffix.lower()
        )
        # Re-extraction is an explicit user action, so replace extracted and manually
        # corrected values with the newly extracted result.
        return await db.save_profile(
            user["id"], profile.model_dump(), stored["filename"],
            clear_manual_fields=True
        )

    @app.get("/profile", response_model=ProfileOut)
    async def get_profile(user: dict = Depends(current_user)) -> dict:
        stored = await db.get_profile(user["id"])
        if not stored:
            raise HTTPException(404, "no profile yet; POST /resume first")
        return stored

    @app.put("/profile", response_model=ProfileOut)
    async def update_profile(update: ProfileUpdate, user: dict = Depends(current_user)) -> dict:
        """Correct fields the extraction got wrong (e.g. years_experience)."""
        stored = await db.get_profile(user["id"])
        if not stored:
            raise HTTPException(404, "no profile yet; POST /resume first")
        merged = {**stored["profile"], **update.model_dump(exclude_unset=True, exclude_none=True)}
        changed_fields = set(update.model_dump(exclude_unset=True, exclude_none=True))
        return await db.save_profile(
            user["id"], Profile.model_validate(merged).model_dump(), None,
            mark_manual_fields=changed_fields
        )

    # -------------------------------------------------------------- study plan
    @app.post("/study-plan", response_model=StudyPlanOut, status_code=201, tags=["study plan"])
    async def create_study_plan(body: StudyPlanRequirement, user: dict = Depends(current_user)) -> dict:
        """Store the requirements and generate the plan with the draft-stage LLM.

        `plan` is plain text in the planner app's import format. Long plans are built
        in several model calls, so expect this to take a minute or more."""
        data = body.model_dump()
        days = studyplan.plan_days(data)
        max_days = _env_int("MAX_PLAN_DAYS", 120)
        if days > max_days:
            raise HTTPException(422, f"a plan can cover at most {max_days} days; this asks for {days}")
        try:
            provider, model = resolve("draft")
        except LLMError as e:
            raise HTTPException(503, f"plan generation is not configured: {e}")
        if not plan_slots.acquire(blocking=False):
            raise HTTPException(503, "too many plans are being generated; try again shortly",
                                headers={"Retry-After": "30"})
        try:
            stored = await db.save_study_plan(user["id"], data)
            try:
                stored["plan"] = await run_in_threadpool(studyplan.generate_plan, data, provider, model)
            except LLMError as e:
                raise HTTPException(502, f"plan generation failed ({e}); your requirements "
                                         f"were saved as study plan {stored['id']}")
            await db.set_study_plan_text(user["id"], stored["id"], stored["plan"])
            return stored
        finally:
            plan_slots.release()

    @app.get("/study-plan", response_model=StudyPlanOut, tags=["study plan"])
    async def get_study_plan(user: dict = Depends(current_user)) -> dict:
        """The most recently stored requirements and plan."""
        stored = await db.latest_study_plan(user["id"])
        if not stored:
            raise HTTPException(404, "no study plan requirements yet; POST /study-plan first")
        return stored

    @app.post("/study-plan/import", response_model=StudyPlanOut, status_code=201, tags=["study plan"])
    async def import_study_plan(body: StudyPlanImport, user: dict = Depends(current_user)) -> dict:
        """Save a plan you already have (e.g. imported in the planner app) together with its
        requirements. Nothing is generated; the text must match the plan format, the
        `tracks` and the duration, otherwise 422 lists what is wrong."""
        data = body.model_dump(exclude={"plan"})
        max_days = _env_int("MAX_PLAN_DAYS", 120)
        if studyplan.plan_days(data) > max_days:
            raise HTTPException(422, f"a plan can cover at most {max_days} days")
        plan = body.plan.strip() + "\n"
        check_plan(plan, data)
        stored = await db.save_study_plan(user["id"], data)
        await db.set_study_plan_text(user["id"], stored["id"], plan)
        return {**stored, "plan": plan}

    @app.put("/study-plan/{plan_id}/plan", response_model=StudyPlanOut, tags=["study plan"])
    async def save_study_plan_text(plan_id: int, body: StudyPlanText,
                                   user: dict = Depends(current_user)) -> dict:
        """Replace the plan text of a stored study plan, e.g. after editing or importing it."""
        stored = await db.get_study_plan(user["id"], plan_id)
        if not stored:
            raise HTTPException(404, "no such study plan")
        plan = body.plan.strip() + "\n"
        check_plan(plan, stored)
        await db.set_study_plan_text(user["id"], plan_id, plan)
        return {**stored, "plan": plan}

    # ------------------------------------------------------------------ search
    async def execute_run(run_id: str, cfg: dict, companies: list[dict],
                          req: SearchRequest) -> None:
        loop = asyncio.get_running_loop()
        async def progress(stage: str, message: str) -> None:
            await db.update_run(run_id, stage, message)
        def progress_from_worker(stage: str, message: str) -> None:
            asyncio.run_coroutine_threadsafe(progress(stage, message), loop).result()
        try:
            seen = await db.seen_ids()
            result = await run_in_threadpool(
                run_pipeline, cfg, seen, companies, limit=req.limit,
                max_age_hours=db.retention_hours(), progress=progress_from_worker)
            await db.save_jobs(result.jobs, run_id)
            expired = await db.purge_expired()
            await db.finish_run(run_id, warnings=result.warnings, counts={
                "scanned": result.scanned, "passed_filters": result.passed_filters,
                "new": result.candidates, "saved": len(result.jobs), "expired": expired})
        except PipelineError as e:
            await db.finish_run(run_id, error=str(e))
        except Exception as e:
            log.exception("run %s crashed", run_id)
            await db.finish_run(run_id, error=f"internal error: {type(e).__name__}")

    async def search_guard(x_api_key: str | None = Header(None)) -> None:
        """Scraping is operator-only: it needs SEARCH_API_KEY in the X-API-Key header and is
        switched off while that variable is unset."""
        key = os.getenv("SEARCH_API_KEY")
        if not key:
            raise HTTPException(403, "scraping is disabled: SEARCH_API_KEY is not set")
        if not (x_api_key and hmac.compare_digest(x_api_key, key)):
            raise HTTPException(401, "missing or invalid X-API-Key")

    @app.post("/search", response_model=RunOut, status_code=202, tags=["scraper"],
              dependencies=[Depends(search_guard)])
    async def search(background: BackgroundTasks, req: SearchRequest | None = None) -> dict:
        """Operator-only (X-API-Key), meant for a cron: scrape the job portals in the background
        and add every new posting that passes the config filters to the shared catalog. Jobs older
        than JOB_RETENTION_HOURS (24) are skipped and deleted. One run at a time (409) and at most
        one per MIN_RUN_INTERVAL_MIN minutes (429)."""
        req = req or SearchRequest()
        cfg = load_cfg()
        companies = load_companies(cfg)
        if req.companies:
            wanted = {c.lower() for c in req.companies}
            names = {(c.get("name") or c.get("slug") or "").lower() for c in companies}
            unknown = sorted(wanted - names)
            if unknown:
                raise HTTPException(422, f"unknown companies: {', '.join(unknown)}")
            companies = [c for c in companies
                         if (c.get("name") or c.get("slug") or "").lower() in wanted]

        try:
            run = await db.start_run(req.model_dump(), _env_int("MIN_RUN_INTERVAL_MIN", 30) * 60)
        except RunBlocked as e:
            if e.kind == "running":
                raise HTTPException(409, f"a run is already in progress: {e.run_id}")
            raise HTTPException(429, "last run was too recent",
                                headers={"Retry-After": str(e.retry_after)})
        background.add_task(execute_run, run["run_id"], cfg, companies, req)
        return run

    @app.get("/runs/latest", response_model=RunOut, tags=["scraper"], dependencies=[Depends(search_guard)])
    async def latest_run() -> dict:
        run = await db.latest_run()
        if not run:
            raise HTTPException(404, "no runs yet")
        return run

    @app.get("/runs/{run_id}", response_model=RunOut, tags=["scraper"], dependencies=[Depends(search_guard)])
    async def get_run(run_id: str) -> dict:
        run = await db.get_run(run_id)
        if not run:
            raise HTTPException(404, "unknown run")
        return run

    # -------------------------------------------------------------------- jobs
    @app.get("/jobs", response_model=JobList, tags=["jobs"])
    async def list_jobs(
        company: str | None = None,
        q: str | None = Query(None, description="Substring of title or company."),
        location: str | None = Query(None, description="Substring of location."),
        window: Literal["1h", "24h"] = Query("24h", description="How recently the job appeared. "
                                             "`1h` needs a signed-in user."),
        limit: int = Query(20, ge=1, le=MAX_PAGE_SIZE),
        offset: int = Query(0, ge=0),
        user: dict | None = Depends(optional_user),
    ) -> dict:
        """Jobs that appeared in the last 24 hours (newer jobs first). Public. Older jobs are
        deleted. Appearance time is when the hourly scraper first saw the posting."""
        if window == "1h" and user is None:
            raise HTTPException(401, "sign in to filter by the last hour", headers=BEARER)
        total, items = await db.list_jobs(company=company, q=q, location=location,
                                          hours=1 if window == "1h" else None,
                                          limit=limit, offset=offset)
        return {"total": total, "limit": limit, "offset": offset, "items": items}

    @app.get("/jobs/applied", response_model=AppliedJobList, tags=["jobs"])
    async def list_applied(limit: int = Query(20, ge=1, le=MAX_PAGE_SIZE),
                           offset: int = Query(0, ge=0),
                           user: dict = Depends(current_user)) -> dict:
        """The jobs you marked as applied, most recent first."""
        total, items = await db.list_applied(user["id"], limit=limit, offset=offset)
        return {"total": total, "limit": limit, "offset": offset, "items": items}

    @app.get("/matches", response_model=MatchList, tags=["matching"])
    async def match_jobs(
        min_match: float = Query(0, ge=0, le=100, description="Minimum match percent."),
        company: str | None = None,
        q: str | None = Query(None, description="Substring of title or company."),
        location: str | None = Query(None, description="Substring of location."),
        status: str | None = Query(None, pattern="^(new|applied)$"),
        window: Literal["1h", "24h"] = Query("24h", description="How recently the job appeared."),
        limit: int = Query(20, ge=1, le=MAX_PAGE_SIZE),
        offset: int = Query(0, ge=0),
        user: dict = Depends(current_user),
    ) -> dict:
        """Compare the keywords in the stored jobs with the keywords in your resume profile.

        Deterministic: skills from the shared vocabulary found in each posting versus the
        ones in your profile's core_skills and domains. `match_percent` is the share of a
        job's recognised skills that you have; jobs naming none score 0. Best first."""
        stored = await db.get_profile(user["id"])
        if not stored:
            raise HTTPException(409, "upload a resume first (POST /resume)")
        have = skills_mod.extract(" ; ".join(
            [*stored["profile"]["core_skills"], *stored["profile"]["domains"]]))
        cap = INSIGHTS_MAX_JOBS
        rows = await db.jobs_for_matching(user["id"], company=company, q=q, location=location,
                                          status=status, hours=1 if window == "1h" else None,
                                          cap=cap + 1)
        truncated = len(rows) > cap
        results = []
        for r in rows[:cap]:
            wanted = skills_mod.extract(f"{r['title']}\n{r['description']}")
            matched = sorted(wanted & have)
            pct = round(100 * len(matched) / len(wanted), 1) if wanted else 0.0
            if pct < min_match:
                continue
            results.append({**{k: v for k, v in r.items() if k != "description"},
                            "match_percent": pct, "matched_skills": matched,
                            "missing_skills": sorted(wanted - have)})
        results.sort(key=lambda m: (-m["match_percent"], -len(m["matched_skills"])))
        return {"total": len(results), "limit": limit, "offset": offset, "truncated": truncated,
                "resume_skills": sorted(have), "items": results[offset:offset + limit]}

    @app.get("/jobs/{job_id}", response_model=JobDetail, tags=["jobs"])
    async def get_job(job_id: str) -> dict:
        job = await db.get_job(job_id)
        if not job:
            raise HTTPException(404, "unknown job")
        return job

    @app.post("/jobs/{job_id}/applied", response_model=AppliedJob, tags=["jobs"])
    async def mark_applied(job_id: str, user: dict = Depends(current_user)) -> dict:
        """Mark a job as applied by you. Idempotent; the first date is kept."""
        applied_on = await db.mark_applied(user["id"], job_id)
        if applied_on is None:
            raise HTTPException(404, "unknown job")
        return {**(await db.get_job(job_id)), "applied_on": applied_on}  # type: ignore[arg-type]

    @app.get("/stats", response_model=StatsOut, tags=["jobs"])
    async def stats(user: dict | None = Depends(optional_user)) -> dict:
        return await db.stats(user["id"] if user else None)

    @app.get("/insights/skills", response_model=SkillInsights, tags=["insights"])
    async def skill_insights(
        role: str | None = Query(None, description=f"Role family: {', '.join(skills_mod.ROLES)}."),
        category: str | None = Query(None, description=f"Only one kind: {', '.join(skills_mod.CATEGORIES)}."),
        company: str | None = None,
        q: str | None = Query(None, description="Substring of title or company."),
        location: str | None = Query(None, description="Substring of location."),
        top: int = Query(15, ge=1, le=100),
        user: dict | None = Depends(optional_user),
    ) -> dict:
        """What the stored postings ask for, as counts and percentages. Public; sign in to
        also get `in_profile` and `gaps` against your resume profile.
        Deterministic keyword matching over the stored descriptions, so every number
        comes from real postings."""
        if role and role not in skills_mod.ROLES:
            raise HTTPException(422, f"unknown role {role!r}; use one of {', '.join(skills_mod.ROLES)}")
        if category and category not in skills_mod.CATEGORIES:
            raise HTTPException(422, f"unknown category {category!r}; use one of {', '.join(skills_mod.CATEGORIES)}")
        cap = INSIGHTS_MAX_JOBS
        rows = await db.postings_for_insights(company=company, q=q, location=location, cap=cap + 1)
        truncated = len(rows) > cap
        rows = rows[:cap]
        role_counts = Counter(skills_mod.role_of(r["title"]) for r in rows)
        if role:
            rows = [r for r in rows if skills_mod.role_of(r["title"]) == role]

        n, counts = skills_mod.count_skills(f"{r['title']}\n{r['description']}" for r in rows)
        stored = await db.get_profile(user["id"]) if user else None
        have = skills_mod.extract(" ; ".join(
            [*stored["profile"]["core_skills"], *stored["profile"]["domains"]])) if stored else None

        ranked = [(name, c) for name, c in counts.most_common()
                  if not category or skills_mod.BY_NAME[name].category == category]
        stats = [{"skill": name, "category": skills_mod.BY_NAME[name].category, "jobs": c,
                  "percent": round(100 * c / n, 1), "in_profile": None if have is None else name in have}
                 for name, c in ranked[:top]]
        gaps = [] if have is None else [s["skill"] for s in stats if not s["in_profile"]][:10]
        with_desc = sum(1 for r in rows if r["description"].strip())
        note = None
        if n == 0:
            note = "no stored postings match; run POST /search first or loosen the filters"
        elif n < 20:
            note = f"small sample ({n} postings); percentages will move a lot as you run more searches"
        elif with_desc < n * 0.8:
            note = "many postings have no description text, so percentages are understated"
        return {"sample_size": n, "with_description": with_desc, "truncated": truncated, "note": note,
                "roles": [{"role": r, "jobs": role_counts[r]} for r in skills_mod.ROLES if role_counts[r]],
                "skills": stats, "gaps": gaps}

    return app


# Importing the package (for tests, CLI tools, or migrations) must not require
# production secrets or a live database. The ASGI entrypoint is fully wired when
# DATABASE_URL is present in the deployment environment.
app = create_app() if os.getenv("DATABASE_URL") else FastAPI(title="jobhunt", version="2.0")
