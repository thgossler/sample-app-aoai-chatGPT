"""
Unit tests for ``backend.mcp_server.knowledge_base_tool``.

Tests cover:
- Basic search returning structured JSON results
- No rag_retriever → graceful error response
- top_k clamped to [1, 20]
- citation_resolver called for each result
- search_type override applied / restored on app_settings.datasource.query_type
- Empty results handled cleanly
- Exception inside retriever returned as error JSON
- get_system_context with and without app_settings
"""

import json
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from backend.mcp_server.knowledge_base_tool import get_system_context, search_knowledge_base


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_rag_doc(
    content="Doc content",
    title="Doc Title",
    url="https://blob.example.com/file.md",
    filename="file.md",
    score=0.95,
    chunk_id="c1",
):
    doc = MagicMock()
    doc.content = content
    doc.title = title
    doc.url = url
    doc.filename = filename
    doc.score = score
    doc.chunk_id = chunk_id
    doc.to_citation_dict.return_value = {
        "id": "1",
        "content": content,
        "title": title,
        "url": url,
        "filepath": filename,
        "chunk_id": chunk_id,
    }
    return doc


def _make_rag_retriever(docs=None):
    mock = MagicMock()
    mock.retrieve = AsyncMock(return_value=docs or [])
    return mock


def _make_app_settings(query_type="semantic"):
    settings = MagicMock()
    settings.azure_openai.system_message = "You are a helpful assistant."
    settings.base_settings.datasource_type = "AzureCognitiveSearch"
    ds = MagicMock()
    ds.query_type = query_type
    ds.index = "my-index"
    ds.top_k = 5
    ds._type = "AzureCognitiveSearch"  # avoid non-serializable MagicMock sub-attr
    ds.use_semantic_search = False
    ds.vector_columns = None
    settings.datasource = ds
    settings.ui.title = "Test Chat App"
    settings.ui.chat_description = "Ask me anything"
    return settings


# ---------------------------------------------------------------------------
# search_knowledge_base tests
# ---------------------------------------------------------------------------

class TestSearchKnowledgeBase:
    @pytest.mark.asyncio
    async def test_no_retriever_returns_error_json(self):
        result = await search_knowledge_base(query="test", rag_retriever=None)
        data = json.loads(result)
        assert "error" in data
        assert data["results"] == []

    @pytest.mark.asyncio
    async def test_basic_search_returns_results(self):
        doc = _make_rag_doc()
        retriever = _make_rag_retriever([doc])
        result = await search_knowledge_base(query="architecture", rag_retriever=retriever)
        data = json.loads(result)
        assert data["total_results"] == 1
        assert data["results"][0]["content"] == "Doc content"
        assert data["results"][0]["title"] == "Doc Title"

    @pytest.mark.asyncio
    async def test_results_indexed_from_1(self):
        docs = [
            _make_rag_doc(
                content=f"Content {i}",
                url=f"https://blob.example.com/file{i}.md",
                filename=f"file{i}.md",
            )
            for i in range(3)
        ]
        retriever = _make_rag_retriever(docs)
        result = await search_knowledge_base(query="q", rag_retriever=retriever)
        data = json.loads(result)
        indices = [r["index"] for r in data["results"]]
        assert indices == [1, 2, 3]

    @pytest.mark.asyncio
    async def test_top_k_min_clamped_to_1(self):
        retriever = _make_rag_retriever([_make_rag_doc()])
        await search_knowledge_base(query="q", top_k=-5, rag_retriever=retriever)
        # retrieve must be called with top_k=1 (clamped)
        retriever.retrieve.assert_called_once()
        called_top_k = retriever.retrieve.call_args[1].get("top_k") or retriever.retrieve.call_args[0][1]
        assert called_top_k == 1

    @pytest.mark.asyncio
    async def test_top_k_max_clamped_to_20(self):
        retriever = _make_rag_retriever([_make_rag_doc()])
        await search_knowledge_base(query="q", top_k=100, rag_retriever=retriever)
        retriever.retrieve.assert_called_once()
        called_top_k = retriever.retrieve.call_args[1].get("top_k") or retriever.retrieve.call_args[0][1]
        assert called_top_k == 20

    @pytest.mark.asyncio
    async def test_empty_results_handled(self):
        retriever = _make_rag_retriever([])
        result = await search_knowledge_base(query="nothing", rag_retriever=retriever)
        data = json.loads(result)
        assert data["total_results"] == 0
        assert data["results"] == []

    @pytest.mark.asyncio
    async def test_citation_resolver_called_per_result(self):
        doc = _make_rag_doc()
        retriever = _make_rag_retriever([doc])
        resolver = MagicMock()
        resolver.resolve.return_value = {
            "source_url": "https://resolved.example.com/page",
            "title": "Resolved Title",
            "filepath": "file.md",
        }
        result = await search_knowledge_base(
            query="q", rag_retriever=retriever, citation_resolver=resolver
        )
        data = json.loads(result)
        resolver.resolve.assert_called_once()
        assert data["results"][0]["url"] == "https://resolved.example.com/page"
        assert data["results"][0]["source_url"] == "https://resolved.example.com/page"

    @pytest.mark.asyncio
    async def test_results_are_aggregated_by_source_file(self):
        first_doc = _make_rag_doc(
            content="First chunk",
            url="https://docs.example.com/guide__001.md#first",
            filename="guide__001.md",
        )
        second_doc = _make_rag_doc(
            content="Second chunk",
            url="https://docs.example.com/guide__002.md#second",
            filename="guide__002.md",
        )
        retriever = _make_rag_retriever([first_doc, second_doc])

        result = await search_knowledge_base(query="q", rag_retriever=retriever)

        data = json.loads(result)
        assert data["total_results"] == 1
        assert len(data["results"]) == 1
        assert "First chunk" in data["results"][0]["content"]
        assert "Second chunk" in data["results"][0]["content"]
        assert data["references_markdown"].count("[1]") == 1

    @pytest.mark.asyncio
    async def test_search_type_override_set_and_restored(self):
        doc = _make_rag_doc()
        retriever = _make_rag_retriever([doc])
        settings = _make_app_settings(query_type="semantic")
        await search_knowledge_base(
            query="q",
            search_type="vector",
            rag_retriever=retriever,
            app_settings=settings,
        )
        # query_type should be restored after the call
        assert settings.datasource.query_type == "semantic"

    @pytest.mark.asyncio
    async def test_invalid_search_type_ignored(self):
        doc = _make_rag_doc()
        retriever = _make_rag_retriever([doc])
        settings = _make_app_settings(query_type="semantic")
        # Should not raise; invalid type is silently ignored
        result = await search_knowledge_base(
            query="q",
            search_type="NOT_VALID",
            rag_retriever=retriever,
            app_settings=settings,
        )
        data = json.loads(result)
        assert "results" in data

    @pytest.mark.asyncio
    async def test_retriever_exception_returns_error_json(self):
        retriever = MagicMock()
        retriever.retrieve = AsyncMock(side_effect=RuntimeError("Search service down"))
        result = await search_knowledge_base(query="q", rag_retriever=retriever)
        data = json.loads(result)
        assert "error" in data
        assert "Search service down" in data["error"]

    @pytest.mark.asyncio
    async def test_relevance_score_included(self):
        doc = _make_rag_doc(score=0.87)
        retriever = _make_rag_retriever([doc])
        result = await search_knowledge_base(query="q", rag_retriever=retriever)
        data = json.loads(result)
        assert data["results"][0]["relevance_score"] == pytest.approx(0.87)

    @pytest.mark.asyncio
    async def test_result_includes_query_field(self):
        retriever = _make_rag_retriever([_make_rag_doc()])
        result = await search_knowledge_base(query="my search query", rag_retriever=retriever)
        data = json.loads(result)
        assert data["query"] == "my search query"


# ---------------------------------------------------------------------------
# get_system_context tests
# ---------------------------------------------------------------------------

class TestGetSystemContext:
    @pytest.mark.asyncio
    async def test_returns_valid_json(self):
        settings = _make_app_settings()
        result = await get_system_context(app_settings=settings)
        data = json.loads(result)  # must not raise
        assert isinstance(data, dict)

    @pytest.mark.asyncio
    async def test_system_message_included(self):
        settings = _make_app_settings()
        result = await get_system_context(app_settings=settings)
        data = json.loads(result)
        assert data["system_message"] == "You are a helpful assistant."

    @pytest.mark.asyncio
    async def test_knowledge_base_info_present_when_datasource_configured(self):
        settings = _make_app_settings()
        result = await get_system_context(app_settings=settings)
        data = json.loads(result)
        assert "knowledge_base" in data
        kb = data["knowledge_base"]
        assert kb["type"] == "AzureCognitiveSearch"
        assert kb["index"] == "my-index"

    @pytest.mark.asyncio
    async def test_no_app_settings_returns_valid_json(self):
        result = await get_system_context(app_settings=None)
        data = json.loads(result)
        assert isinstance(data, dict)
        # Should not crash; may return empty context
