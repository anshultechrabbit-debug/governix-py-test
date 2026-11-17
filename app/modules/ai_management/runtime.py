"""Every AI call of an organization goes through here: its model, its key, its limits, its ledger.

`organization_llm` / `organization_embedder` build the provider the organization may use and wrap it in a meter.
Before each call the meter reserves an upper estimate under the organization's advisory lock (held for
milliseconds, not for the call), so concurrent requests can never spend the same remaining balance; after it,
the provider's actual usage replaces the estimate and the alert thresholds are checked. `reserve_question`
charges one question to the shared quota before an answer starts.
"""

from __future__ import annotations

import copy
import logging
import math
import uuid
from collections.abc import Callable, Iterator

from pydantic import SecretStr
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Settings
from app.infrastructure.ai.embeddings.base import Embedded, EmbeddingProvider, EmbeddingUnavailableError
from app.infrastructure.ai.llm.base import LLMProvider, LLMResult
from app.modules.ai_management.model import AIProvider, ModelService
from app.modules.ai_management.service import AIBlocked, AIEmbeddingBlocked, AIManagementService, AIModelNotConfigured

logger = logging.getLogger(__name__)

# Below this many output tokens a call is not worth making: the limit counts as reached.
MIN_OUTPUT_TOKENS = 256


def _estimate(text: str) -> int:
    """An upper bound of the tokens in `text` (English averages about 4 characters a token)."""
    return math.ceil(len(text) / 3) + 16


def _free(model: str) -> bool:
    return model.startswith("local-")


def organization_llm(db: Session, session_factory: sessionmaker, settings: Settings, organization_id: uuid.UUID,
                     *, fallback: Callable[[], LLMProvider] | None = None,
                     user_id: uuid.UUID | None = None) -> LLMProvider:
    """The organization's answering model, metered.

    Nothing assigned and no key of its own: the platform's model (`fallback`, the runtime's shared provider),
    still metered against the organization's limits. Raises AIModelNotConfigured when its assigned models are
    all disabled."""
    service = AIManagementService(db, settings)
    provider, model_id, row = service.active_model(organization_id, ModelService.ANSWERS)
    key = service.decrypted_key(organization_id, provider)
    if row is None and key is None and fallback is not None:
        return OrganizationLLM(fallback(), session_factory, settings, organization_id, provider=provider,
                               user_id=user_id)
    changes: dict = {"LLM_PROVIDER": provider, "LLM_MODEL": model_id}
    if provider == AIProvider.OPENAI and key:
        changes["OPENAI_API_KEY"] = SecretStr(key)
    elif provider == AIProvider.OPENCODE:
        changes["OPENCODE_MODEL"] = model_id
        if key:
            changes["OPENCODE_API_KEY"] = SecretStr(key)
    elif provider == AIProvider.OPENAI_COMPATIBLE and key:
        changes["LLM_API_KEY"] = SecretStr(key)
    from app.infrastructure.ai.llm.factory import create_llm

    inner = create_llm(settings.model_copy(update=changes))
    return OrganizationLLM(inner, session_factory, settings, organization_id, provider=provider, user_id=user_id)


class OrganizationLLM(LLMProvider):
    """An organization's model: each call reserved against its limits first, settled with actual usage after."""

    def __init__(self, inner: LLMProvider, session_factory: sessionmaker, settings: Settings,
                 organization_id: uuid.UUID, *, provider: str, user_id: uuid.UUID | None) -> None:
        self.inner = inner
        self.session_factory = session_factory
        self.settings = settings
        self.organization_id = organization_id
        self.provider_name = provider
        self.user_id = user_id
        self.model_id = getattr(inner, "model_id", None) or "unknown"

    @property
    def model(self) -> str:
        return getattr(self.inner, "model", None) or self.model_id

    def _attempts(self, streaming: bool) -> int:
        """How many output budgets one call may spend: a truncated reply is asked again with twice the budget."""
        if streaming or getattr(self.inner, "schema_in_prompt", False):
            return 1 if streaming else 2
        retries = int(getattr(self.inner, "truncation_retries", 0) or 0)
        return 2 ** (retries + 1) - 1

    def _reserve(self, system: str, user: str, *, streaming: bool) -> tuple[uuid.UUID, LLMProvider]:
        """Reserve the call's worst case, shrinking its output budget to what the limits leave."""
        input_tokens = _estimate(system) + _estimate(user)
        budget = int(getattr(self.inner, "max_output_tokens", 0) or self.settings.LLM_MAX_OUTPUT_TOKENS)
        factor = self._attempts(streaming)
        with self.session_factory() as session:
            service = AIManagementService(session, self.settings)
            service.lock(self.organization_id)
            room = service.headroom(self.organization_id, save=True)
            caps = [budget]
            if room.output is not None:
                caps.append(int(room.output // factor))
            if room.total is not None:
                caps.append(int((room.total - input_tokens) // factor))
            if room.credit is not None and not _free(self.model):
                price = service.price(self.model, self.provider_name, service._models(self.organization_id))
                if price and price[1] > 0:
                    caps.append(int((room.credit - input_tokens * price[0] / 1e6) / (price[1] / 1e6) / factor))
            budget = min(caps)
            if budget < min(MIN_OUTPUT_TOKENS, caps[0]):
                code = "CREDIT_LIMIT_REACHED" if room.credit is not None and budget == caps[-1] else "TOKEN_LIMIT_REACHED"
                raise AIBlocked("Your organization has reached its AI limit for this period.", code=code)
            row = service.reserve(self.organization_id, service="answers", provider=self.provider_name,
                                  model=self.model, input_tokens=input_tokens, output_tokens=budget * factor,
                                  embedding_tokens=0, user_id=self.user_id)
            session.commit()
            usage_id = row.id
        inner = self.inner
        if hasattr(inner, "max_output_tokens") and budget < inner.max_output_tokens:
            inner = copy.copy(inner)  # this call only: other threads keep the full budget
            inner.max_output_tokens = budget
        return usage_id, inner

    def _settle(self, usage_id: uuid.UUID, result: LLMResult | None, error: Exception | None = None, *,
                keep_estimate: bool = False) -> None:
        try:
            with self.session_factory() as session:
                service = AIManagementService(session, self.settings)
                if keep_estimate:
                    service.settle_estimate(usage_id)
                elif result is None or _free(result.model or ""):
                    # Failed, or the provider fell back to the local stand-in: nothing was spent.
                    service.settle(usage_id, model=self.model, error=type(error).__name__ if error else None)
                else:
                    service.settle(usage_id, model=result.model or self.model,
                                   input_tokens=result.input_tokens or 0, output_tokens=result.output_tokens or 0)
                session.commit()
                service.evaluate_alerts(self.organization_id)
                session.commit()
        except Exception:  # accounting never fails the answer it describes
            logger.exception("Could not settle AI usage %s", usage_id)

    def generate_json(self, system: str, user: str, schema: dict, *, context=None) -> LLMResult:
        usage_id, inner = self._reserve(system, user, streaming=False)
        try:
            result = inner.generate_json(system, user, schema, context=context)
        except Exception as exc:
            self._settle(usage_id, None, exc)
            raise
        self._settle(usage_id, result)
        return result

    def stream_json(self, system: str, user: str, schema: dict, *, context=None) -> Iterator:
        usage_id, inner = self._reserve(system, user, streaming=True)
        result: LLMResult | None = None
        try:
            for part in inner.stream_json(system, user, schema, context=context):
                if isinstance(part, LLMResult):
                    result = part
                yield part
        except GeneratorExit:
            # The reader left mid-answer: the provider was still paid, so the reserved estimate stands.
            self._settle(usage_id, result, keep_estimate=result is None)
            raise
        except Exception as exc:
            self._settle(usage_id, result, exc if result is None else None)
            raise
        self._settle(usage_id, result)


def organization_embedder(db: Session, session_factory: sessionmaker, settings: Settings,
                          organization_id: uuid.UUID, fallback: EmbeddingProvider | None,
                          *, user_id: uuid.UUID | None = None) -> EmbeddingProvider | None:
    """The organization's embedding model, metered. Nothing assigned: the platform's embedder."""
    service = AIManagementService(db, settings)
    try:
        provider, model_id, row = service.active_model(organization_id, ModelService.EMBEDDINGS)
        key = service.decrypted_key(organization_id, provider) if row is not None else None
    except AIModelNotConfigured as exc:  # an embedding error, so searches fall back to keywords
        raise AIEmbeddingBlocked(exc.message, code=exc.code) from None
    if row is None:
        if fallback is None:
            return None
        return OrganizationEmbedding(fallback, session_factory, settings, organization_id, provider, user_id=user_id)
    changes: dict = {"EMBEDDING_PROVIDER": provider, "EMBEDDING_MODEL": model_id}
    if provider == AIProvider.OPENAI and key:
        changes["OPENAI_API_KEY"] = SecretStr(key)
    elif provider == AIProvider.OPENAI_COMPATIBLE and key:
        changes["EMBEDDING_API_KEY"] = SecretStr(key)
    elif provider not in (AIProvider.OPENAI, AIProvider.OPENAI_COMPATIBLE, AIProvider.OLLAMA, AIProvider.LOCAL):
        raise AIEmbeddingBlocked(f"{provider} cannot create embeddings; assign an embedding model of another provider.")
    from app.infrastructure.ai.embeddings.factory import create_embedder

    inner = create_embedder(settings.model_copy(update=changes))
    return OrganizationEmbedding(inner, session_factory, settings, organization_id, provider,
                                 user_id=user_id) if inner else None


def search_embedder(db: Session, session_factory: sessionmaker, settings: Settings, organization_id: uuid.UUID,
                    fallback: EmbeddingProvider | None, *, user_id: uuid.UUID | None = None) -> EmbeddingProvider | None:
    """The organization's embedder for searching, or None (keyword lanes only) when it may not embed now."""
    try:
        return organization_embedder(db, session_factory, settings, organization_id, fallback, user_id=user_id)
    except EmbeddingUnavailableError as exc:
        logger.warning("Organization %s searches without vectors: %s", organization_id, exc)
        return None


class OrganizationEmbedding(EmbeddingProvider):
    def __init__(self, inner: EmbeddingProvider, session_factory: sessionmaker, settings: Settings,
                 organization_id: uuid.UUID, provider: str, *, user_id: uuid.UUID | None = None) -> None:
        self.inner = inner
        self.session_factory = session_factory
        self.settings = settings
        self.organization_id = organization_id
        self.provider_name = provider
        self.user_id = user_id
        self.model_id = inner.model_id
        self.dimensions = inner.dimensions

    @property
    def model(self) -> str:
        return getattr(self.inner, "model", self.model_id)

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return self.embed(texts).vectors

    def embed(self, texts: list[str]) -> Embedded:
        estimate = sum(_estimate(t) for t in texts)
        with self.session_factory() as session:
            service = AIManagementService(session, self.settings)
            service.lock(self.organization_id)
            row = service.reserve(self.organization_id, service="embeddings", provider=self.provider_name,
                                  model=self.model, input_tokens=0, output_tokens=0, embedding_tokens=estimate,
                                  user_id=self.user_id)
            session.commit()
            usage_id = row.id
        try:
            embedded = self.inner.embed(texts)
        except Exception as exc:
            self._settle(usage_id, None, exc)
            raise
        self._settle(usage_id, embedded)
        return embedded

    def _settle(self, usage_id: uuid.UUID, embedded: Embedded | None, error: Exception | None = None) -> None:
        try:
            with self.session_factory() as session:
                service = AIManagementService(session, self.settings)
                if embedded is None or embedded.model_id != self.model_id:
                    # Failed, or a stand-in produced the vectors: the provider was not paid.
                    service.settle(usage_id, model=self.model, error=type(error).__name__ if error else "fallback")
                else:
                    service.settle(usage_id, model=self.model, embedding_tokens=embedded.input_tokens or 0)
                session.commit()
                service.evaluate_alerts(self.organization_id)
                session.commit()
        except Exception:
            logger.exception("Could not settle AI usage %s", usage_id)


def reserve_question(session: Session, settings: Settings, organization_id: uuid.UUID,
                     user_id: uuid.UUID | None = None) -> None:
    """Charge one question to the organization's shared quota, or refuse it (AIBlocked, HTTP 429) when the
    question, token or credit limit is used up or no model may be used. Commits."""
    service = AIManagementService(session, settings)
    service.lock(organization_id)
    service.check_question(organization_id)
    service.record_question(organization_id, user_id)
    session.commit()
    try:
        service.evaluate_alerts(organization_id)
        session.commit()
    except Exception:
        session.rollback()
        logger.exception("Could not check AI alerts for organization %s", organization_id)


__all__ = ["AIBlocked", "AIEmbeddingBlocked", "AIModelNotConfigured", "organization_embedder", "organization_llm",
           "reserve_question", "search_embedder"]
