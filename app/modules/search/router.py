from typing import Annotated

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.dependencies import get_cache
from app.core.responses import ApiResponse, ok
from app.infrastructure.cache.base import Cache
from app.modules.auth.dependencies import CurrentPrincipal, require
from app.modules.auth.permissions import Permission, Principal
from app.modules.search.schema import SearchRequest, SearchResponse
from app.modules.search.service import SearchService
from app.infrastructure.ai.llm.base import LLMUnavailableError
from app.modules.ai_management.model import AIFeature
from app.modules.ai_management.runtime import organization_llm, search_embedder
from app.workers.runtime import get_runtime

router = APIRouter(prefix="/search", tags=["search"])

Searcher = Annotated[Principal, Depends(require(Permission.SEARCH))]


def get_service(
    request: Request, db: Annotated[Session, Depends(get_db)], cache: Annotated[Cache, Depends(get_cache)],
    principal: CurrentPrincipal,
) -> SearchService:
    runtime = get_runtime()
    settings = request.app.state.settings
    factory = request.app.state.session_factory
    if not principal.organization_id:
        return SearchService(db, factory, runtime.embedder, cache, llm_factory=lambda: runtime.llm)
    # The caller's organization: its model, its key, its shared limits (ai_management.runtime).
    try:
        llm = organization_llm(db, factory, settings, principal.organization_id, fallback=lambda: runtime.llm,
                               user_id=principal.user_id, feature=AIFeature.SEARCH)
    except LLMUnavailableError:  # AI not available to the organization: search still works, on its own
        llm = None
    embedder = search_embedder(db, factory, settings, principal.organization_id, runtime.embedder,
                               user_id=principal.user_id, feature=AIFeature.SEARCH)
    return SearchService(db, factory, embedder, cache, llm_factory=(lambda: llm) if llm is not None else None)


@router.post("", response_model=ApiResponse[SearchResponse])
def search(body: SearchRequest, principal: Searcher, service: Annotated[SearchService, Depends(get_service)]):
    """Hybrid search over authorised, effective content (passages carry full provenance)."""
    return ok(service.search(principal, body))
