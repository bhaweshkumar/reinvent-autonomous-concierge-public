"""Catalog search tool (SPEC-18 §18.1 split of agent/tools.py)."""

from typing import Optional

from ..venues import load_registry
from .contracts import SearchCatalogInput, SearchCatalogOutput
from .runner import _ToolRunner


class CatalogToolsMixin(_ToolRunner):
    async def search_catalog(
        self,
        query: str,
        level: Optional[int] = None,
        venue: Optional[str] = None,
        hero_only: bool = False,
        limit: int = 5,
        session_type: Optional[str] = None,
        topic: Optional[str] = None,
    ) -> SearchCatalogOutput:
        """Search the local catalog in plain English with optional filters."""
        arguments = {
            "query": query,
            "level": level,
            "venue": venue,
            "hero_only": hero_only,
            "limit": limit,
            "session_type": session_type,
            "topic": topic,
        }
        return await self._run_tool(
            "search_catalog", SearchCatalogInput, arguments, self._search_catalog
        )

    async def _search_catalog(self, params: SearchCatalogInput) -> SearchCatalogOutput:
        venue = params.venue
        if venue:
            venue = load_registry().canonical(venue) or venue
        sessions = await self.ingestion.search(
            query=params.query,
            level=params.level,
            venue=venue,
            hero_only=params.hero_only,
            limit=params.limit,
            session_type=params.session_type,
            topic=params.topic,
        )
        zone = self._zone()
        localized = [summary.localized(zone) for summary in sessions]
        return SearchCatalogOutput(sessions=localized, total_found=len(localized))
