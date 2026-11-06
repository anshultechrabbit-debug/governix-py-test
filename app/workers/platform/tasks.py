"""Background work: AI summaries, the daily policy-expiry sweep, and the Support Center SLA sweep."""

import logging
import uuid
from datetime import UTC, datetime, time, timedelta

from app.infrastructure.ai.llm.base import LLMUnavailableError
from app.infrastructure.queue.base import Queue
from app.infrastructure.queue.registry import JobContext, task
from app.modules.notifications.service import expiry_sweep
from app.modules.policies.model import PolicyVersion
from app.modules.versions.summary import SUMMARIZE, summarize_version
from app.workers.runtime import get_runtime

logger = logging.getLogger(__name__)

EXPIRY_SWEEP = "notifications.expiry_sweep"
SUPPORT_SWEEP = "support.sla_sweep"
SWEEP_TIME = time(0, 5)  # UTC


@task(SUMMARIZE)
def summarize(payload: dict, ctx: JobContext) -> None:
    rt = get_runtime()
    if not rt.settings.AI_SUMMARY_ENABLED:
        return
    with rt.session_factory() as session:
        version = session.get(PolicyVersion, uuid.UUID(payload["version_id"]))
        if version is None:
            return
        try:
            summarize_version(session, rt.llm, version)
        except LLMUnavailableError:
            # A summary is optional; the policy is fully usable without one.
            logger.warning("No AI summary for version %s: the model is unavailable", version.id)
            return
        session.commit()


@task(EXPIRY_SWEEP)
def sweep(payload: dict, ctx: JobContext) -> None:
    rt = get_runtime()
    day = datetime.fromisoformat(payload["date"]).date()
    with rt.session_factory() as session:
        sent = expiry_sweep(session, day)
        session.commit()
    logger.info("Expiry sweep for %s: %s notifications", day, sent)
    schedule_daily(rt.queue, now=datetime.combine(day, SWEEP_TIME, tzinfo=UTC))


@task(SUPPORT_SWEEP)
def support_sweep(payload: dict, ctx: JobContext) -> None:
    # SUPPORT CENTER DISABLED (2026-11-06): still registered, so a sweep already in the queue finishes quietly; it does nothing and
    # does not queue the next one.
    return
    # from app.modules.tickets.sla import sweep as sla_sweep
    #
    # rt = get_runtime()
    # with rt.session_factory() as session:
    #     counts = sla_sweep(session, rt.settings)
    #     session.commit()
    # if any(counts.values()):
    #     logger.info("Support SLA sweep: %s", counts)
    # schedule_support_sweep(rt.queue, rt.settings.SUPPORT_SWEEP_MINUTES, after=datetime.fromisoformat(payload["slot"]))


def schedule_support_sweep(queue: Queue, minutes: int, after: datetime | None = None) -> None:
    """Queue the next SLA sweep at the next slot of `minutes`. One job per slot, however often this is called."""
    now = datetime.now(UTC)
    step = timedelta(minutes=max(1, minutes))
    start = datetime.combine(now.date(), time(0), tzinfo=UTC)
    slot = start + step * (int((max(now, after or now) - start) / step) + 1)
    queue.enqueue(SUPPORT_SWEEP, {"slot": slot.isoformat()}, idempotency_key=f"support-sweep:{slot.isoformat()}",
                  delay_seconds=max(0.0, (slot - now).total_seconds()))


def schedule_daily(queue: Queue, now: datetime | None = None) -> None:
    """Make sure today's sweep ran or is queued, and tomorrow's is waiting. Safe to call repeatedly.
    Also keeps the Support Center's SLA sweep going."""
    # SUPPORT CENTER DISABLED (2026-11-06): the SLA sweep is not scheduled.
    # try:
    #     minutes = get_runtime().settings.SUPPORT_SWEEP_MINUTES
    # except RuntimeError:  # no runtime configured (a script)
    #     minutes = 15
    # schedule_support_sweep(queue, minutes)
    now = now or datetime.now(UTC)
    today = now.date()
    queue.enqueue(EXPIRY_SWEEP, {"date": today.isoformat()}, idempotency_key=f"expiry-sweep:{today.isoformat()}")
    tomorrow = today + timedelta(days=1)
    at = datetime.combine(tomorrow, SWEEP_TIME, tzinfo=UTC)
    queue.enqueue(
        EXPIRY_SWEEP, {"date": tomorrow.isoformat()}, idempotency_key=f"expiry-sweep:{tomorrow.isoformat()}",
        delay_seconds=max(0.0, (at - datetime.now(UTC)).total_seconds()),
    )
