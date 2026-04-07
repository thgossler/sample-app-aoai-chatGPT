"""
MCP tools that provide knowledge base access (RAG) and system context.

Exposed tools
-------------
search_knowledge_base
    Query Azure AI Search (same index as the web chat) and return structured
    results with resolved citation links.  Uses ``AzureSearchRAGRetriever``
    from ``backend.rag_service`` — the same code path used for reasoning
    models in the web chat (manual RAG, not OYD).

get_system_context
    Return the configured system message and data source information so that
    the MCP client can ground its own system prompt.

These functions are registered on the FastMCP instance in
``remote_mcp_server.py``.
"""

import json
import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


async def search_knowledge_base(
    query: str,
    top_k: int = 5,
    search_type: Optional[str] = None,
    rag_retriever=None,
    citation_resolver=None,
    app_settings=None,
) -> str:
    """
    Search the knowledge base and return relevant results with source links.

    Parameters
    ----------
    query:
        The search query string.
    top_k:
        Maximum number of results to return (1–20).  Defaults to 5.
    search_type:
        Optional override for the search mode.  One of:
        ``"simple"``, ``"semantic"``, ``"vector"``,
        ``"vector_simple_hybrid"``, ``"vector_semantic_hybrid"``.
        When omitted the datasource default is used.
    rag_retriever:
        Injected ``AzureSearchRAGRetriever`` instance.
    citation_resolver:
        Injected ``CitationLinkResolver`` instance.
    app_settings:
        Injected ``_AppSettings`` instance (for datasource info).

    Returns
    -------
    str
        JSON-encoded search result object.
    """
    if rag_retriever is None:
        return json.dumps(
            {
                "error": "Knowledge base search is not available — no data source is configured.",
                "results": [],
            }
        )

    top_k = max(1, min(top_k, 20))

    try:
        # Override query_type on the retriever if caller specified one
        original_query_type = None
        if search_type and app_settings and app_settings.datasource:
            valid_types = {
                "simple",
                "semantic",
                "vector",
                "vector_simple_hybrid",
                "vector_semantic_hybrid",
            }
            if search_type in valid_types:
                original_query_type = app_settings.datasource.query_type
                app_settings.datasource.query_type = search_type
            else:
                logger.warning("Ignoring unknown search_type: %s", search_type)

        documents = await rag_retriever.retrieve(query, top_k=top_k)

        # Restore original query type
        if original_query_type is not None and app_settings and app_settings.datasource:
            app_settings.datasource.query_type = original_query_type

        if not documents:
            return json.dumps(
                {
                    "query": query,
                    "total_results": 0,
                    "results": [],
                    "search_type": search_type or "default",
                }
            )

        results: List[Dict[str, Any]] = []
        for i, doc in enumerate(documents):
            citation = doc.to_citation_dict()
            citation["id"] = str(i + 1)

            # Resolve citation links server-side
            if citation_resolver is not None:
                citation = citation_resolver.resolve(citation)

            # Use resolved metadata (from frontmatter extraction) with
            # fallback to the original document fields.  The resolver
            # promotes source_title → citation["title"], source_url →
            # citation["url"], source_file → citation["filepath"], so
            # those should take precedence over the raw index fields.
            result: Dict[str, Any] = {
                "index": i + 1,
                "content": citation.get("clean_content") or doc.content,
                "title": citation.get("title") or doc.title or doc.filename or "Document",
                "source_url": citation.get("source_url") or doc.url,
                "source_type": citation.get("source_type"),
                "filepath": citation.get("filepath") or doc.filename,
                "relevance_score": doc.score,
                "chunk_id": citation.get("chunk_id"),
            }

            # Include part_index / chunk_total when available
            if citation.get("part_index"):
                result["part_index"] = citation["part_index"]
            if citation.get("chunk_total"):
                result["chunk_total"] = citation["chunk_total"]

            results.append(result)

        # Build markdown references section for the LLM to include
        references_lines = []
        for result in results:
            idx = result["index"]
            title = result.get("title") or result.get("filepath") or "Document"
            url = result.get("source_url") or ""
            if url:
                references_lines.append(f"[{idx}] [{title}]({url})")
            else:
                references_lines.append(f"[{idx}] {title}")
        references_markdown = "\n".join(references_lines)

        logger.info(
            "search_knowledge_base: query=%r top_k=%d results=%d",
            query[:80],
            top_k,
            len(results),
        )

        return json.dumps(
            {
                "query": query,
                "total_results": len(results),
                "search_type": search_type
                    or (
                        app_settings.datasource.query_type
                        if app_settings and app_settings.datasource
                        else "default"
                    ),
                "results": results,
                "references_markdown": references_markdown,
                "citation_instructions": (
                    "When referencing these search results use [N] notation "
                    "(e.g. [1], [2]) matching the result index numbers. "
                    "At the end of your response, include a 'References' "
                    "section with each cited source on its own line using "
                    "the exact format from references_markdown above."
                ),
            },
            ensure_ascii=False,
            indent=2,
        )

    except Exception as exc:
        logger.error("search_knowledge_base error: %s", exc, exc_info=True)
        return json.dumps(
            {
                "error": f"Search failed: {str(exc)}",
                "query": query,
                "results": [],
            }
        )


async def get_system_context(app_settings=None) -> str:
    """
    Return the configured system message and data source information.

    MCP clients can use this to ground their own system prompt with the same
    persona and knowledge-base context that the web chat uses.

    Returns
    -------
    str
        JSON-encoded context object.
    """
    context: Dict[str, Any] = {}

    if app_settings:
        context["system_message"] = app_settings.azure_openai.system_message or ""

        if app_settings.datasource:
            ds = app_settings.datasource
            context["knowledge_base"] = {
                "type": getattr(ds, "_type", "unknown"),
                "index": getattr(ds, "index", None),
                "search_modes": _get_available_search_modes(app_settings),
            }
        else:
            context["knowledge_base"] = None

        context["ui"] = {
            "title": app_settings.ui.title if app_settings.ui else "",
            "chat_description": (
                app_settings.ui.chat_description if app_settings.ui else ""
            ),
        }
    else:
        context["system_message"] = ""
        context["knowledge_base"] = None

    return json.dumps(context, ensure_ascii=False, indent=2)


def _get_available_search_modes(app_settings) -> List[str]:
    """Return the search modes available given the current datasource config."""
    if not app_settings or not app_settings.datasource:
        return []

    ds = app_settings.datasource
    modes = ["simple"]

    has_semantic = bool(getattr(ds, "use_semantic_search", False))
    has_vectors = bool(getattr(ds, "vector_columns", None))

    if has_semantic:
        modes.append("semantic")
    if has_vectors:
        modes.extend(["vector", "vector_simple_hybrid"])
    if has_semantic and has_vectors:
        modes.append("vector_semantic_hybrid")

    return modes
