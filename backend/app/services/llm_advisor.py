"""LLM advisor (§5.1).

Produces a periodic qualitative read on macro conditions and the symbols
the active models trade. Ollama is the default provider; Gemini is the
fallback when it is selected explicitly or when Ollama is unreachable.

**This module is context-only and structurally isolated from trading.** It
imports nothing from `trading_engine`, so there is no code path by which an
advisory can place, resize or block an order. The only sanctioned link runs
the other way: the trading loop reads the latest advisory row and copies it
into `trades.llm_context` for audit. A test asserts that isolation over the
real import graph rather than trusting this comment.

**The daily cap is a reservation, not a courtesy check.** A row is written
*before* the provider is called, so two callers racing (the scheduled job
and a manual trigger) cannot both pass a "how many so far today" check and
make two calls. A failed call keeps its reservation and counts against the
cap: the limit bounds *calls made*, and releasing it would let a failing
provider be retried without limit.
"""

import asyncio
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db.models import LLMAdvisory, Model
from app.services.config_service import get_config

logger = logging.getLogger(__name__)

PROVIDER_OLLAMA = "ollama"
PROVIDER_GEMINI = "gemini"

COMPONENT = "llm_advisor"

STATUS_PENDING = "pending"
STATUS_OK = "ok"
STATUS_FAILED = "failed"

# Advisory lock id, so the cap check and its reservation are serialised
# across connections rather than only within one.
CAP_LOCK_ID = 815_231_004


class LLMAdvisorError(RuntimeError):
    """An advisory could not be produced."""


class DailyCapReached(LLMAdvisorError):
    """The configured calls-per-day limit is exhausted."""


class ProvidersUnavailable(LLMAdvisorError):
    """No configured provider could be reached."""


@dataclass
class AdvisoryResult:
    created: bool
    advisory_id: int | None = None
    provider: str | None = None
    response: dict | None = None
    reason: str = ""


# --------------------------------------------------------------------------
# Providers
# --------------------------------------------------------------------------


async def _call_ollama(prompt: str, model: str, timeout: float) -> str:
    """Ollama runs locally; a missing daemon is the common case, not an
    exceptional one, so the error is normalised for the caller."""
    try:
        import ollama
    except ImportError as exc:  # pragma: no cover - dependency is declared
        raise ProvidersUnavailable(f"ollama package unavailable: {exc}") from exc

    def _run():
        client = ollama.Client(host=settings.OLLAMA_BASE_URL, timeout=timeout)
        response = client.chat(
            model=model,
            messages=[{"role": "user", "content": prompt}],
        )
        return response["message"]["content"]

    try:
        return await asyncio.to_thread(_run)
    except Exception as exc:  # noqa: BLE001
        raise ProvidersUnavailable(
            f"Ollama at {settings.OLLAMA_BASE_URL} unreachable: {exc}"
        ) from exc


async def _call_gemini(prompt: str, model: str, timeout: float) -> str:
    if not settings.GEMINI_API_KEY:
        raise ProvidersUnavailable("GEMINI_API_KEY is not set.")

    try:
        import google.generativeai as genai
    except ImportError as exc:  # pragma: no cover
        raise ProvidersUnavailable(f"google-generativeai unavailable: {exc}") from exc

    def _run():
        genai.configure(api_key=settings.GEMINI_API_KEY)
        return genai.GenerativeModel(model).generate_content(prompt).text

    try:
        return await asyncio.to_thread(_run)
    except Exception as exc:  # noqa: BLE001
        raise ProvidersUnavailable(f"Gemini call failed: {exc}") from exc


async def call_provider(prompt: str, preferred: str, models: dict, timeout: float):
    """Try the preferred provider, then the other one.

    Returns (provider_name, raw_text). Raises ProvidersUnavailable when
    neither responds — the caller records that and skips the cycle rather
    than letting it reach the scheduler (§1.7).
    """
    order = (
        [PROVIDER_GEMINI, PROVIDER_OLLAMA]
        if preferred == PROVIDER_GEMINI
        else [PROVIDER_OLLAMA, PROVIDER_GEMINI]
    )

    failures = []
    for provider in order:
        try:
            if provider == PROVIDER_OLLAMA:
                text_out = await _call_ollama(
                    prompt, models.get(PROVIDER_OLLAMA, "llama3"), timeout
                )
            else:
                text_out = await _call_gemini(
                    prompt, models.get(PROVIDER_GEMINI, "gemini-1.5-flash"), timeout
                )
            return provider, text_out
        except ProvidersUnavailable as exc:
            logger.warning("Provider %s unavailable: %s", provider, exc)
            failures.append(f"{provider}: {exc}")

    raise ProvidersUnavailable("; ".join(failures))


# --------------------------------------------------------------------------
# Prompt
# --------------------------------------------------------------------------

PROMPT_TEMPLATE = """You are a macro analyst briefing an automated crypto \
trading system. The system trades these symbols with its own statistical \
models: {symbols}.

Give a concise briefing covering:
1. Current macro and world-economy conditions relevant to crypto markets \
(rates, liquidity, regulation, risk appetite, notable events).
2. A qualitative view on each of the symbols listed above.
3. Your overall assessment of how uncertain the current environment is.

Respond with ONLY a JSON object, no prose outside it, in exactly this shape:
{{
  "macro_summary": "<2-4 sentences on the macro backdrop>",
  "uncertainty": "<one of: low, normal, elevated, high>",
  "uncertainty_reason": "<1-2 sentences>",
  "symbols": {{
{symbol_lines}
  }},
  "key_risks": ["<short risk>", "<short risk>"]
}}

You are informing a human operator's judgement and an audit trail. You are \
not being asked to place, size or veto any trade, and nothing you return \
will do so."""


async def build_prompt(db: AsyncSession) -> tuple[str, list[str]]:
    """Prompt covering the symbols the *active* models actually trade (§5.1).

    Falls back to the configured symbol list when nothing is promoted yet,
    so an early-stage system still gets a briefing.
    """
    rows = (
        await db.execute(
            select(Model.symbol).where(Model.status == "active").distinct()
        )
    ).scalars().all()

    symbols = sorted(rows) or list(await get_config(db, "symbols"))

    symbol_lines = ",\n".join(
        f'    "{symbol}": {{"view": "<bullish|neutral|bearish>", '
        f'"comment": "<1-2 sentences>"}}'
        for symbol in symbols
    )

    return (
        PROMPT_TEMPLATE.format(
            symbols=", ".join(symbols), symbol_lines=symbol_lines
        ),
        symbols,
    )


def parse_response(raw: str) -> dict:
    """Parse the model's JSON, keeping the raw text either way.

    Models routinely wrap JSON in prose or code fences. Rather than
    discarding an otherwise-useful response, the object is extracted when
    possible and the raw text is always retained for audit.
    """
    text_out = (raw or "").strip()

    fenced = text_out
    if "```" in fenced:
        parts = fenced.split("```")
        for part in parts:
            candidate = part.strip()
            if candidate.startswith("json"):
                candidate = candidate[4:].strip()
            if candidate.startswith("{"):
                fenced = candidate
                break

    start, end = fenced.find("{"), fenced.rfind("}")
    if start != -1 and end > start:
        try:
            parsed = json.loads(fenced[start : end + 1])
            if isinstance(parsed, dict):
                parsed["_raw"] = text_out
                parsed["_parsed"] = True
                return parsed
        except json.JSONDecodeError:
            pass

    logger.warning("LLM response was not valid JSON; storing raw text.")
    return {"_raw": text_out, "_parsed": False, "macro_summary": text_out[:2000]}


# --------------------------------------------------------------------------
# Daily cap
# --------------------------------------------------------------------------


async def count_calls_today(db: AsyncSession, now: datetime) -> tuple[int, int]:
    """Calls in the current UTC day, and in the trailing 24 hours.

    Both are checked. The calendar-day count is what "per day" means; the
    rolling window is a backstop against a clock that jumps backwards over
    a day boundary, which would otherwise reset the calendar count and
    allow a fresh batch of calls.
    """
    start_of_day = now.replace(hour=0, minute=0, second=0, microsecond=0)

    today = (
        await db.execute(
            select(func.count(LLMAdvisory.id)).where(
                LLMAdvisory.created_at >= start_of_day
            )
        )
    ).scalar_one()

    rolling = (
        await db.execute(
            select(func.count(LLMAdvisory.id)).where(
                LLMAdvisory.created_at >= now - timedelta(hours=24)
            )
        )
    ).scalar_one()

    return int(today or 0), int(rolling or 0)


async def reserve_call_slot(
    db: AsyncSession, provider: str, prompt: str, now: datetime | None = None
) -> LLMAdvisory:
    """Claim one of today's calls, or raise DailyCapReached.

    Takes a transaction-scoped advisory lock so a concurrent caller cannot
    read the same count and reserve a second slot. The reservation row is
    committed before the provider is called, which is what makes the cap a
    hard limit rather than a check that a race can slip past.
    """
    now = now or datetime.now(timezone.utc)
    cap = int(await get_config(db, "llm_calls_per_day"))

    await db.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": CAP_LOCK_ID})

    today, rolling = await count_calls_today(db, now)

    if cap <= 0:
        raise DailyCapReached(f"llm_calls_per_day is {cap}; advisories are disabled.")
    if today >= cap:
        raise DailyCapReached(
            f"Daily cap reached: {today}/{cap} calls already made today (UTC)."
        )
    if rolling >= cap:
        raise DailyCapReached(
            f"Rolling 24h cap reached: {rolling}/{cap} calls in the last 24 hours."
        )

    reservation = LLMAdvisory(
        provider=provider,
        prompt=prompt,
        response={"status": STATUS_PENDING, "reserved_at": now.isoformat()},
        created_at=now,
    )
    db.add(reservation)
    await db.commit()

    logger.info("Reserved LLM call slot %d/%d for today.", today + 1, cap)
    return reservation


# --------------------------------------------------------------------------
# Advisory generation
# --------------------------------------------------------------------------


async def generate_advisory(
    db: AsyncSession, now: datetime | None = None
) -> AdvisoryResult:
    """Produce and store one advisory (§5.1). Never raises to the caller."""
    now = now or datetime.now(timezone.utc)

    preferred = await get_config(db, "llm_provider")
    timeout = float(await get_config(db, "llm_timeout_seconds"))
    models = await get_config(db, "llm_models")

    prompt, symbols = await build_prompt(db)

    try:
        reservation = await reserve_call_slot(db, preferred, prompt, now)
    except DailyCapReached as exc:
        logger.info("Advisory skipped: %s", exc)
        return AdvisoryResult(created=False, reason=str(exc))

    try:
        provider, raw = await call_provider(prompt, preferred, models, timeout)
    except ProvidersUnavailable as exc:
        # The reservation stays and counts against the cap: the limit bounds
        # calls attempted, and freeing it would let a broken provider be
        # retried without bound.
        reservation.response = {
            "status": STATUS_FAILED,
            "error": str(exc),
            "symbols": symbols,
        }
        await db.commit()
        logger.error("All LLM providers unavailable: %s", exc)
        return AdvisoryResult(
            created=False, advisory_id=reservation.id, reason=str(exc)
        )

    parsed = parse_response(raw)
    parsed["status"] = STATUS_OK
    parsed["symbols_requested"] = symbols

    reservation.provider = provider
    reservation.response = parsed
    await db.commit()

    logger.info(
        "Advisory %s stored from %s (uncertainty=%s).",
        reservation.id, provider, parsed.get("uncertainty"),
    )
    return AdvisoryResult(
        created=True,
        advisory_id=reservation.id,
        provider=provider,
        response=parsed,
        reason="Advisory stored.",
    )


# --------------------------------------------------------------------------
# Read-only accessors (the sanctioned direction)
# --------------------------------------------------------------------------


async def get_recent_advisories(db: AsyncSession, limit: int = 2) -> list[LLMAdvisory]:
    """Most recent advisories, newest first (§8.1 panel)."""
    return list(
        (
            await db.execute(
                select(LLMAdvisory)
                .order_by(LLMAdvisory.created_at.desc())
                .limit(limit)
            )
        ).scalars().all()
    )


async def get_latest_usable_advisory(db: AsyncSession) -> LLMAdvisory | None:
    """The newest advisory that actually has a response.

    Pending reservations and failed calls are skipped — attaching either to
    a trade would imply context that was never obtained.
    """
    rows = (
        await db.execute(
            select(LLMAdvisory).order_by(LLMAdvisory.created_at.desc()).limit(10)
        )
    ).scalars().all()

    for row in rows:
        if (row.response or {}).get("status") == STATUS_OK:
            return row
    return None
