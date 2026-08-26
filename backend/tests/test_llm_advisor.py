"""LLM advisor tests (§5.1).

Two things carry the weight here:

* The daily cap must actually *block* a call, not merely discourage one.
* The advisor must have no structural path into order placement. That is
  asserted against the real import graph, because a comment saying "context
  only" is not a control.
"""

import ast
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest
import pytest_asyncio
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.db.models import Config, LLMAdvisory
from app.services import llm_advisor as advisor

NOW = datetime(2026, 3, 1, 12, 0, tzinfo=timezone.utc)
DB_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql+asyncpg://postgres:postgres@127.0.0.1:5432/trading_pipeline",
)

SERVICES = Path(__file__).resolve().parents[1] / "app" / "services"


# --------------------------------------------------------------------------
# 3. Structural isolation from order placement
# --------------------------------------------------------------------------


def _imports_of(module_path: Path) -> set[str]:
    """Module names imported by a file, from its AST."""
    tree = ast.parse(module_path.read_text())
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
            # `from app.services import trading_engine` — the module is a name.
            names.update(f"{node.module}.{a.name}" for a in node.names)
    return names


def _reachable_modules(start: str, seen: set[str] | None = None) -> set[str]:
    """Transitively collect app.services modules reachable from `start`.

    Follows the whole chain, so an indirect route into order placement is
    caught as well as a direct import.
    """
    seen = seen if seen is not None else set()
    if start in seen:
        return seen
    seen.add(start)

    path = SERVICES / f"{start}.py"
    if not path.exists():
        return seen

    for imported in _imports_of(path):
        if not imported.startswith("app.services."):
            continue
        tail = imported[len("app.services.") :]
        # Handles both `app.services.x` and `app.services.x.symbol`.
        for candidate in (tail, tail.split(".")[0]):
            if (SERVICES / f"{candidate}.py").exists():
                _reachable_modules(candidate, seen)
    return seen


class TestIsolationFromTrading:
    def test_advisor_cannot_reach_order_placement(self):
        """The advisory is context (§5.1). If it could import the trading
        engine, an advisory could place or cancel an order — the exact
        coupling the spec forbids."""
        reachable = _reachable_modules("llm_advisor")

        assert "trading_engine" not in reachable, (
            f"llm_advisor can reach trading_engine via {sorted(reachable)}"
        )

    def test_advisor_cannot_reach_the_risk_engine_either(self):
        """It must not be able to veto a trade any more than place one."""
        assert "risk_engine" not in _reachable_modules("llm_advisor")

    def test_advisor_module_names_no_trading_symbols(self):
        source = (SERVICES / "llm_advisor.py").read_text()
        for forbidden in ("place_order", "_submit_order", "create_order"):
            assert forbidden not in source

    def test_import_graph_helper_actually_detects_a_path(self):
        """Guards the test above from silently passing on a broken walker:
        the scheduler genuinely does reach trading_engine."""
        assert "trading_engine" in _reachable_modules("scheduler")

    def test_trading_engine_reads_advisories_without_importing_the_advisor(self):
        """The sanctioned direction is trading -> advisory *data*.

        Checked against the import graph, not the text: the config key
        `llm_advisory_max_age_hours` contains "llm_advisor" as a substring,
        so a naive string search reports a dependency that is not there.
        """
        assert "llm_advisor" not in _reachable_modules("trading_engine")
        assert "LLMAdvisory" in (SERVICES / "trading_engine.py").read_text()


# --------------------------------------------------------------------------
# Prompt and parsing (no DB)
# --------------------------------------------------------------------------


class TestPromptAndParsing:
    def test_prompt_requests_macro_and_per_symbol_view(self):
        prompt = advisor.PROMPT_TEMPLATE.format(
            symbols="BTCUSDT", symbol_lines='    "BTCUSDT": {}'
        )
        lowered = prompt.lower()
        assert "macro" in lowered
        assert "world-economy" in lowered or "world economy" in lowered
        assert "crypto" in lowered
        assert "uncertainty" in lowered

    def test_prompt_states_the_advisory_cannot_trade(self):
        prompt = advisor.PROMPT_TEMPLATE.format(symbols="X", symbol_lines="")
        assert "not being asked to place" in prompt

    def test_parses_plain_json(self):
        parsed = advisor.parse_response('{"uncertainty": "high"}')
        assert parsed["uncertainty"] == "high"
        assert parsed["_parsed"] is True

    def test_parses_json_inside_a_code_fence(self):
        raw = 'Sure!\n```json\n{"uncertainty": "low"}\n```\nHope that helps.'
        parsed = advisor.parse_response(raw)
        assert parsed["uncertainty"] == "low"
        assert parsed["_parsed"] is True

    def test_parses_json_with_surrounding_prose(self):
        parsed = advisor.parse_response('Here you go: {"uncertainty": "normal"} ok')
        assert parsed["uncertainty"] == "normal"

    def test_unparseable_response_is_kept_not_discarded(self):
        """A model that ignores the format still produced something a human
        may want to read."""
        parsed = advisor.parse_response("the market feels choppy today")
        assert parsed["_parsed"] is False
        assert "choppy" in parsed["_raw"]

    def test_empty_response_does_not_raise(self):
        assert advisor.parse_response("")["_parsed"] is False

    def test_raw_text_always_retained(self):
        raw = '{"uncertainty": "high"}'
        assert advisor.parse_response(raw)["_raw"] == raw


# --------------------------------------------------------------------------
# Provider selection and failure handling (no DB)
# --------------------------------------------------------------------------


class TestProviderSelection:
    async def test_uses_ollama_by_default(self):
        async def ok(prompt, model, timeout):
            return '{"uncertainty":"low"}'

        with patch.object(advisor, "_call_ollama", ok):
            provider, _ = await advisor.call_provider("p", "ollama", {}, 5)
        assert provider == advisor.PROVIDER_OLLAMA

    async def test_falls_back_to_gemini_when_ollama_is_unreachable(self):
        async def down(prompt, model, timeout):
            raise advisor.ProvidersUnavailable("connection refused")

        async def ok(prompt, model, timeout):
            return "{}"

        with patch.object(advisor, "_call_ollama", down), \
             patch.object(advisor, "_call_gemini", ok):
            provider, _ = await advisor.call_provider("p", "ollama", {}, 5)

        assert provider == advisor.PROVIDER_GEMINI

    async def test_gemini_preferred_when_configured(self):
        async def ok(prompt, model, timeout):
            return "{}"

        async def should_not_run(prompt, model, timeout):
            raise AssertionError("Ollama tried first despite gemini preference")

        with patch.object(advisor, "_call_gemini", ok), \
             patch.object(advisor, "_call_ollama", should_not_run):
            provider, _ = await advisor.call_provider("p", "gemini", {}, 5)

        assert provider == advisor.PROVIDER_GEMINI

    async def test_both_down_raises_with_both_reasons(self):
        async def down(prompt, model, timeout):
            raise advisor.ProvidersUnavailable("nope")

        with patch.object(advisor, "_call_ollama", down), \
             patch.object(advisor, "_call_gemini", down):
            with pytest.raises(advisor.ProvidersUnavailable) as exc:
                await advisor.call_provider("p", "ollama", {}, 5)

        assert "ollama" in str(exc.value) and "gemini" in str(exc.value)


# --------------------------------------------------------------------------
# Daily cap and generation (DB)
# --------------------------------------------------------------------------


async def _db_reachable() -> bool:
    engine = create_async_engine(DB_URL)
    try:
        async with engine.connect():
            return True
    except Exception:
        return False
    finally:
        await engine.dispose()


@pytest_asyncio.fixture
async def db():
    if not await _db_reachable():
        pytest.skip(f"No database reachable at {DB_URL}")

    engine = create_async_engine(DB_URL)
    maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with maker() as session:
        await session.execute(delete(LLMAdvisory))
        await session.commit()
        try:
            yield session
        finally:
            await session.rollback()
            await session.execute(delete(LLMAdvisory))
            await session.commit()
    await engine.dispose()


async def _set(db, key, value):
    await db.execute(delete(Config).where(Config.key == key))
    db.add(Config(key=key, value=value))
    await db.commit()


def _stub_call(text='{"uncertainty":"normal","macro_summary":"calm"}'):
    async def call(prompt, preferred, models, timeout):
        return "ollama", text
    return call


class TestDailyCap:
    async def test_cap_blocks_the_call_not_just_warns(self, db):
        """The point of the hard cap: the third attempt at a cap of 2 must
        never reach a provider."""
        await _set(db, "llm_calls_per_day", 2)

        calls = {"count": 0}

        async def counting(prompt, preferred, models, timeout):
            calls["count"] += 1
            return "ollama", '{"uncertainty":"low"}'

        with patch.object(advisor, "call_provider", counting):
            first = await advisor.generate_advisory(db)
            second = await advisor.generate_advisory(db)
            third = await advisor.generate_advisory(db)

        assert first.created and second.created
        assert not third.created
        assert "cap" in third.reason.lower()
        assert calls["count"] == 2, "a provider call was made past the cap"

    async def test_manual_trigger_shares_the_same_cap(self, db):
        """A manual trigger must not be a way to buy extra calls."""
        await _set(db, "llm_calls_per_day", 1)

        with patch.object(advisor, "call_provider", _stub_call()):
            await advisor.generate_advisory(db)
            manual = await advisor.generate_advisory(db)

        assert not manual.created

    async def test_reservation_is_written_before_the_provider_is_called(self, db):
        """Why the cap holds under a race: the slot exists on disk before the
        call, so a concurrent caller counting rows already sees it."""
        await _set(db, "llm_calls_per_day", 5)
        seen = {}

        async def inspect(prompt, preferred, models, timeout):
            engine = create_async_engine(DB_URL)
            maker = async_sessionmaker(engine, class_=AsyncSession)
            async with maker() as other:
                rows = (await other.execute(select(LLMAdvisory))).scalars().all()
                seen["rows"] = len(rows)
                seen["status"] = (rows[0].response or {}).get("status") if rows else None
            await engine.dispose()
            return "ollama", "{}"

        with patch.object(advisor, "call_provider", inspect):
            await advisor.generate_advisory(db)

        assert seen["rows"] == 1
        assert seen["status"] == advisor.STATUS_PENDING

    async def test_zero_cap_disables_advisories_entirely(self, db):
        await _set(db, "llm_calls_per_day", 0)

        async def must_not_run(*args, **kwargs):
            raise AssertionError("provider called with a cap of zero")

        with patch.object(advisor, "call_provider", must_not_run):
            result = await advisor.generate_advisory(db)

        assert not result.created

    async def test_rolling_window_blocks_a_backwards_clock_jump(self, db):
        """A clock jumping back over midnight resets the calendar-day count;
        the rolling 24h count is what stops a fresh batch of calls."""
        await _set(db, "llm_calls_per_day", 2)

        # It is now just after midnight UTC.
        early = NOW.replace(hour=2, minute=0)

        # Two calls made late *yesterday* — a different calendar day, but
        # only a few hours ago.
        for offset in (3, 4):
            db.add(LLMAdvisory(
                provider="ollama", prompt="p", response={"status": "ok"},
                created_at=early - timedelta(hours=offset),
            ))
        await db.commit()

        # The calendar-day count has reset; the rolling window has not.
        today, rolling = await advisor.count_calls_today(db, early)
        assert today == 0, "expected the calendar day to have rolled over"
        assert rolling == 2

        with pytest.raises(advisor.DailyCapReached, match="Rolling"):
            await advisor.reserve_call_slot(db, "ollama", "p", early)

    async def test_yesterdays_calls_do_not_block_today(self, db):
        await _set(db, "llm_calls_per_day", 2)
        db.add(LLMAdvisory(
            provider="ollama", prompt="p", response={"status": "ok"},
            created_at=NOW - timedelta(days=3),
        ))
        await db.commit()

        reservation = await advisor.reserve_call_slot(db, "ollama", "p", NOW)
        assert reservation.id is not None


class TestFailureHandling:
    async def test_both_providers_down_does_not_raise(self, db):
        """§1.7: the scheduler must survive a provider outage."""
        await _set(db, "llm_calls_per_day", 2)

        async def down(prompt, preferred, models, timeout):
            raise advisor.ProvidersUnavailable("ollama: refused; gemini: no key")

        with patch.object(advisor, "call_provider", down):
            result = await advisor.generate_advisory(db)

        assert not result.created
        assert "refused" in result.reason

    async def test_failed_call_is_recorded_not_dropped(self, db):
        await _set(db, "llm_calls_per_day", 2)

        async def down(prompt, preferred, models, timeout):
            raise advisor.ProvidersUnavailable("unreachable")

        with patch.object(advisor, "call_provider", down):
            await advisor.generate_advisory(db)

        rows = (await db.execute(select(LLMAdvisory))).scalars().all()
        assert len(rows) == 1
        assert rows[0].response["status"] == advisor.STATUS_FAILED
        assert "unreachable" in rows[0].response["error"]

    async def test_failed_call_still_consumes_its_slot(self, db):
        """The cap bounds calls attempted. Releasing a failed slot would let
        a broken provider be retried without limit."""
        await _set(db, "llm_calls_per_day", 1)

        async def down(prompt, preferred, models, timeout):
            raise advisor.ProvidersUnavailable("unreachable")

        with patch.object(advisor, "call_provider", down):
            await advisor.generate_advisory(db)
            second = await advisor.generate_advisory(db)

        assert "cap" in second.reason.lower()

    async def test_failed_advisory_is_never_attached_to_a_trade(self, db):
        """Attaching a failed call would imply context that was never got."""
        db.add(LLMAdvisory(
            provider="ollama", prompt="p",
            response={"status": advisor.STATUS_FAILED, "error": "down"},
            created_at=NOW,
        ))
        await db.commit()

        assert await advisor.get_latest_usable_advisory(db) is None

    async def test_pending_reservation_is_not_usable_either(self, db):
        db.add(LLMAdvisory(
            provider="ollama", prompt="p",
            response={"status": advisor.STATUS_PENDING}, created_at=NOW,
        ))
        await db.commit()
        assert await advisor.get_latest_usable_advisory(db) is None


class TestStorage:
    async def test_full_response_stored_in_llm_advisories(self, db):
        await _set(db, "llm_calls_per_day", 2)
        raw = (
            '{"macro_summary":"Liquidity tightening.",'
            '"uncertainty":"elevated","uncertainty_reason":"Rate decision due.",'
            '"symbols":{"BTCUSDT":{"view":"neutral","comment":"Range-bound."}},'
            '"key_risks":["CPI print","ETF outflows"]}'
        )

        with patch.object(advisor, "call_provider", _stub_call(raw)):
            result = await advisor.generate_advisory(db)

        assert result.created
        row = (await db.execute(select(LLMAdvisory))).scalars().one()
        assert row.provider == "ollama"
        assert row.prompt  # the exact prompt is retained
        assert row.response["uncertainty"] == "elevated"
        assert row.response["symbols"]["BTCUSDT"]["view"] == "neutral"
        assert row.response["key_risks"] == ["CPI print", "ETF outflows"]
        assert row.response["_raw"] == raw

    async def test_recent_advisories_newest_first(self, db):
        for i in range(3):
            db.add(LLMAdvisory(
                provider="ollama", prompt=f"p{i}",
                response={"status": "ok", "n": i},
                created_at=NOW - timedelta(hours=i),
            ))
        await db.commit()

        rows = await advisor.get_recent_advisories(db, limit=2)
        assert [r.response["n"] for r in rows] == [0, 1]


# --------------------------------------------------------------------------
# Optional confidence-floor adjustment (§5.1) — OFF by default
# --------------------------------------------------------------------------


class TestConfidenceFloorAdjustment:
    """A real change to step 7's entry rules, so it stays opt-in and can
    only ever tighten the floor."""

    @staticmethod
    async def _advisory(db, uncertainty, status="ok", age_hours=1):
        db.add(LLMAdvisory(
            provider="ollama", prompt="p",
            response={"status": status, "uncertainty": uncertainty},
            created_at=datetime.now(timezone.utc) - timedelta(hours=age_hours),
        ))
        await db.commit()

    async def test_disabled_by_default(self, db):
        from app.services.risk_engine import load_limits

        await _set(db, "llm_confidence_adjustment_enabled", False)
        await self._advisory(db, "high")

        limits = await load_limits(db)
        assert limits["llm_floor_adjustment"] is None
        assert limits["min_confidence"] == 0.6

    async def test_raises_the_floor_when_enabled_and_uncertain(self, db):
        from app.services.risk_engine import load_limits

        await _set(db, "llm_confidence_adjustment_enabled", True)
        await _set(db, "llm_uncertainty_confidence_bonus", 0.05)
        await self._advisory(db, "high")

        limits = await load_limits(db)
        assert limits["min_confidence"] == pytest.approx(0.65)
        assert limits["llm_floor_adjustment"]["uncertainty"] == "high"

        await _set(db, "llm_confidence_adjustment_enabled", False)

    async def test_calm_advisory_leaves_the_floor_alone(self, db):
        from app.services.risk_engine import load_limits

        await _set(db, "llm_confidence_adjustment_enabled", True)
        await self._advisory(db, "low")

        limits = await load_limits(db)
        assert limits["min_confidence"] == 0.6
        assert limits["llm_floor_adjustment"] is None

        await _set(db, "llm_confidence_adjustment_enabled", False)

    async def test_never_lowers_the_floor_even_with_a_negative_bonus(self, db):
        """Defence in depth: the config route rejects a negative bonus, and
        this clamps it too. An LLM opinion must not be able to loosen a risk
        threshold."""
        from app.services.risk_engine import load_limits

        await _set(db, "llm_confidence_adjustment_enabled", True)
        await _set(db, "llm_uncertainty_confidence_bonus", -0.5)
        await self._advisory(db, "high")

        limits = await load_limits(db)
        assert limits["min_confidence"] == 0.6

        await _set(db, "llm_confidence_adjustment_enabled", False)
        await _set(db, "llm_uncertainty_confidence_bonus", 0.05)

    async def test_stale_advisory_is_ignored(self, db):
        from app.services.risk_engine import load_limits

        await _set(db, "llm_confidence_adjustment_enabled", True)
        await _set(db, "llm_advisory_max_age_hours", 12)
        await self._advisory(db, "high", age_hours=48)

        limits = await load_limits(db)
        assert limits["min_confidence"] == 0.6

        await _set(db, "llm_confidence_adjustment_enabled", False)
        await _set(db, "llm_advisory_max_age_hours", 36)

    async def test_failed_advisory_never_adjusts_the_floor(self, db):
        from app.services.risk_engine import load_limits

        await _set(db, "llm_confidence_adjustment_enabled", True)
        await self._advisory(db, "high", status="failed")

        limits = await load_limits(db)
        assert limits["min_confidence"] == 0.6

        await _set(db, "llm_confidence_adjustment_enabled", False)

    async def test_floor_is_capped_at_one(self, db):
        from app.services.risk_engine import load_limits

        await _set(db, "llm_confidence_adjustment_enabled", True)
        await _set(db, "min_confidence", 0.98)
        await _set(db, "llm_uncertainty_confidence_bonus", 0.5)
        await self._advisory(db, "elevated")

        limits = await load_limits(db)
        assert limits["min_confidence"] == 1.0

        await _set(db, "llm_confidence_adjustment_enabled", False)
        await _set(db, "min_confidence", 0.6)
        await _set(db, "llm_uncertainty_confidence_bonus", 0.05)
