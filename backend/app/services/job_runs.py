"""Audit-trail rows for background jobs (§8.1, §9).

`job_runs` gives the dashboard a real "last run" answer for downloads and
training that survives a page navigation, a closed tab, or a missed
WebSocket event — matching the audit-trail pattern already used by
`risk_log` and `llm_advisories`: every run leaves a row, not just a
last-known blob.

Caller commits, same convention as `config_service`.
"""

import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import JobRun

JOB_DOWNLOAD = "download"
JOB_TRAINING = "training"

STATUS_RUNNING = "running"
STATUS_SUCCESS = "success"
STATUS_FAILED = "failed"


async def start_job(
    db: AsyncSession,
    job_type: str,
    *,
    symbol: str | None = None,
    detail: dict[str, Any] | None = None,
) -> JobRun:
    job = JobRun(
        id=uuid.uuid4(),
        job_type=job_type,
        symbol=symbol,
        status=STATUS_RUNNING,
        progress=0,
        detail=detail or {},
    )
    db.add(job)
    await db.flush()
    return job


async def update_job(
    db: AsyncSession,
    job_id: uuid.UUID,
    *,
    progress: float | None = None,
    detail: dict[str, Any] | None = None,
) -> None:
    job = await db.get(JobRun, job_id)
    if job is None:
        return
    if progress is not None:
        job.progress = progress
    if detail is not None:
        job.detail = detail


async def finish_job(
    db: AsyncSession,
    job_id: uuid.UUID,
    *,
    status: str,
    error: str | None = None,
    detail: dict[str, Any] | None = None,
) -> None:
    job = await db.get(JobRun, job_id)
    if job is None:
        return
    job.status = status
    job.error = error
    job.progress = 1.0
    if detail is not None:
        job.detail = detail
    job.finished_at = datetime.now(timezone.utc)


async def latest_job(db: AsyncSession, job_type: str) -> JobRun | None:
    result = await db.execute(
        select(JobRun)
        .where(JobRun.job_type == job_type)
        .order_by(JobRun.started_at.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


def to_dict(job: JobRun | None) -> dict[str, Any] | None:
    if job is None:
        return None
    return {
        "job_id": str(job.id),
        "job_type": job.job_type,
        "symbol": job.symbol,
        "status": job.status,
        "progress": float(job.progress) if job.progress is not None else None,
        "detail": job.detail,
        "error": job.error,
        "started_at": job.started_at,
        "finished_at": job.finished_at,
    }
