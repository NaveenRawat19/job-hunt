"""fetch -> prefilter as a plain function.

Scraping the job portals is separate from judging the postings: this module only
collects them (no LLM, no resume). Comparing them with a resume happens later, on
demand. The API needs a callable that reports progress and raises instead of
exiting. No I/O besides the network calls the fetchers themselves make.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from .fetch import Job, fetch_all
from .prefilter import prefilter, reject_reason, stale

Progress = Callable[[str, str], None]


class PipelineError(RuntimeError):
    """A run that cannot produce anything useful (bad config, nothing fetched)."""


@dataclass
class PipelineResult:
    scanned: int = 0
    passed_filters: int = 0
    candidates: int = 0
    jobs: list[Job] = field(default_factory=list)       # new postings to store
    warnings: list[str] = field(default_factory=list)


def run_pipeline(cfg: dict, seen_ids: set[str], companies: list[dict], *,
                 limit: int | None = None, max_age_hours: float | None = None,
                 progress: Progress | None = None) -> PipelineResult:
    note = progress or (lambda stage, message: None)
    filters = cfg.get("filters") or {}
    result = PipelineResult()

    if not companies:
        raise PipelineError("no companies configured")

    def keep(job: Job) -> bool:
        return job.job_id not in seen_ids and not stale(job, max_age_hours) and \
            reject_reason(job, filters, location_unknown_ok=True) is None

    fetched: dict[str, Job] = {}
    for i, company in enumerate(companies, 1):
        name = company.get("name") or company.get("slug") or company.get("url")
        note("fetching", f"{i}/{len(companies)} {name}")
        for job in fetch_all([company], keep=keep, workday=cfg.get("workday") or {}):
            fetched.setdefault(job.job_id, job)
    result.scanned = len(fetched)
    if not fetched:
        result.warnings.append("no new postings fetched")
        return result

    note("filtering", f"{len(fetched)} postings")
    passed = prefilter(list(fetched.values()), filters)
    result.passed_filters = len(passed)
    jobs = [j for j in passed if j.job_id not in seen_ids and not stale(j, max_age_hours)]
    result.candidates = len(jobs)

    if limit and len(jobs) > limit:
        # Keep the newest, leave the rest unrecorded for the next run.
        jobs.sort(key=lambda j: j.posted_at or "", reverse=True)
        jobs = jobs[:limit]
    result.jobs = jobs
    return result
