"""
Unit tests for ``backend.mcp_server.citation_resolver``.

Tests cover:
- Wiki / Markdown URL resolution (with .md strip for DevOps, URL encoding)
- PDF URL resolution with SAS token appended
- PDF URL returned as-is when SAS config is missing
- Blob storage URL pass-through (non-Wiki, non-PDF)
- Direct (non-blob) URL pass-through
- Embedded metadata extraction from chunk content
- Graceful handling of missing storage_base_url / link_base_url
"""

import pytest
from unittest.mock import MagicMock, patch

from backend.mcp_server.citation_resolver import CitationLinkResolver


def _make_settings(
    storage_base_url="https://mystorage.blob.core.windows.net/mycontainer",
    link_base_url="https://dev.azure.com/org/proj/_wiki/wikis/Wiki.wiki",
    link_url_appendix="",
    storage_account_key="dummy-key",
):
    s = MagicMock()
    s.storage_base_url = storage_base_url
    s.link_base_url = link_base_url
    s.link_url_appendix = link_url_appendix
    s.storage_account_key = storage_account_key
    return s


class TestWikiUrlResolution:
    def _resolver(self, **kw):
        return CitationLinkResolver(_make_settings(**kw))

    def test_md_file_resolved_to_wiki_url(self):
        r = self._resolver()
        blob_url = "https://mystorage.blob.core.windows.net/mycontainer/Architecture/Overview.md"
        citation = {"url": blob_url, "content": ""}
        resolved = r.resolve(citation)
        assert "source_url" in resolved
        src = resolved["source_url"]
        # .md extension should be stripped
        assert not src.endswith(".md")
        # Path should be URL-encoded
        assert "Architecture" in src or "%2F" in src or "Architecture%2F" in src

    def test_chunk_suffix_is_removed_from_wiki_url(self):
        r = self._resolver()
        blob_url = "https://mystorage.blob.core.windows.net/mycontainer/4977-Kubernetes-Guideline__001.md"
        resolved = r.resolve({"url": blob_url, "content": ""})
        assert resolved["source_url"].endswith("%2F4977-Kubernetes-Guideline")
        assert "__001" not in resolved["source_url"]

    def test_query_string_is_not_encoded_into_wiki_path(self):
        r = self._resolver()
        blob_url = "https://mystorage.blob.core.windows.net/mycontainer/Page__001.md?sv=1"
        resolved = r.resolve({"url": blob_url, "content": ""})
        assert resolved["source_url"].endswith("%2FPage")
        assert "%3F" not in resolved["source_url"]

    def test_underscore_wiki_in_url_triggers_wiki_resolution(self):
        r = self._resolver()
        blob_url = "https://mystorage.blob.core.windows.net/mycontainer/Docs/_wiki/Page.md"
        citation = {"url": blob_url, "content": ""}
        resolved = r.resolve(citation)
        src = resolved["source_url"]
        assert src.startswith("https://dev.azure.com")

    def test_md_extension_stripped_for_devops_wiki(self):
        r = self._resolver(link_base_url="https://dev.azure.com/org/_wiki/wiki")
        blob_url = "https://mystorage.blob.core.windows.net/mycontainer/Page.md"
        citation = {"url": blob_url, "content": ""}
        resolved = r.resolve(citation)
        assert ".md" not in resolved["source_url"]

    def test_appendix_added(self):
        r = self._resolver(link_url_appendix="?wikiVersion=GBmain")
        blob_url = "https://mystorage.blob.core.windows.net/mycontainer/Page.md"
        citation = {"url": blob_url, "content": ""}
        resolved = r.resolve(citation)
        assert resolved["source_url"].endswith("?wikiVersion=GBmain")

    def test_url_not_matching_base_returned_unchanged(self):
        r = self._resolver()
        foreign_url = "https://other.blob.core.windows.net/cont/Page.md"
        citation = {"url": foreign_url, "content": ""}
        resolved = r.resolve(citation)
        assert resolved["source_url"] == foreign_url


class TestPdfUrlResolution:
    def test_sas_appended_to_pdf_url(self):
        r = CitationLinkResolver(_make_settings())
        blob_url = "https://mystorage.blob.core.windows.net/mycontainer/Doc.pdf"
        citation = {"url": blob_url, "content": ""}

        mock_sas = "sv=2023-01-01&se=2023-01-01T01%3A00%3A00Z&sig=abc"
        with patch.object(r, "_generate_sas_token", return_value=mock_sas):
            resolved = r.resolve(citation)

        src = resolved["source_url"]
        assert src.startswith(blob_url)
        assert "?" in src
        assert mock_sas in src

    def test_pdf_returned_as_is_when_no_sas_config(self):
        settings = _make_settings(storage_account_key=None)
        r = CitationLinkResolver(settings)
        blob_url = "https://mystorage.blob.core.windows.net/mycontainer/Doc.pdf"
        citation = {"url": blob_url, "content": ""}
        resolved = r.resolve(citation)
        assert resolved["source_url"] == blob_url


class TestBlobUrlPassThrough:
    def test_non_wiki_non_pdf_blob_returned_as_is(self):
        r = CitationLinkResolver(_make_settings())
        blob_url = "https://mystorage.blob.core.windows.net/mycontainer/file.docx"
        citation = {"url": blob_url, "content": ""}
        resolved = r.resolve(citation)
        assert resolved["source_url"] == blob_url


class TestDirectUrlPassThrough:
    def test_non_blob_url_returned_unchanged(self):
        r = CitationLinkResolver(_make_settings())
        direct_url = "https://docs.example.com/page"
        citation = {"url": direct_url, "content": ""}
        resolved = r.resolve(citation)
        assert resolved["source_url"] == direct_url


class TestMissingConfig:
    def test_wiki_url_unchanged_when_no_storage_base_url(self):
        settings = _make_settings(storage_base_url=None, link_base_url=None)
        r = CitationLinkResolver(settings)
        blob_url = "https://mystorage.blob.core.windows.net/cont/Page.md"
        citation = {"url": blob_url, "content": ""}
        resolved = r.resolve(citation)
        # Should fall back to raw URL unchanged since config is missing
        assert resolved["source_url"] == blob_url

    def test_no_url_results_in_none_source_url(self):
        r = CitationLinkResolver(_make_settings())
        citation = {"content": "Some content"}
        resolved = r.resolve(citation)
        assert resolved.get("source_url") is None


class TestEmbeddedMetadataExtraction:
    def test_source_url_extracted_from_content(self):
        r = CitationLinkResolver(_make_settings())
        content = (
            "Some text\n"
            "source_url: https://dev.azure.com/org/_wiki/page\n"
            "More text\n"
        )
        citation = {"content": content}
        resolved_citation = r._extract_embedded_metadata(citation)
        assert resolved_citation["url"] == "https://dev.azure.com/org/_wiki/page"

    def test_source_title_extracted(self):
        r = CitationLinkResolver(_make_settings())
        content = "source_title: My Document Title\nContent here"
        citation = {"content": content}
        result = r._extract_embedded_metadata(citation)
        assert result["title"] == "My Document Title"

    def test_source_file_extracted(self):
        r = CitationLinkResolver(_make_settings())
        content = "source_file: /docs/architecture.md\nContent"
        citation = {"content": content}
        result = r._extract_embedded_metadata(citation)
        assert result["filepath"] == "/docs/architecture.md"

    def test_chunk_index_and_total_extracted(self):
        r = CitationLinkResolver(_make_settings())
        content = "chunk_index: 3\nchunk_total: 10\nContent"
        citation = {"content": content}
        result = r._extract_embedded_metadata(citation)
        assert result["part_index"] == 3
        assert result["chunk_total"] == 10

    def test_no_metadata_returns_unchanged(self):
        r = CitationLinkResolver(_make_settings())
        citation = {"content": "Plain content with no metadata lines", "url": "http://example.com"}
        result = r._extract_embedded_metadata(citation)
        assert result["url"] == "http://example.com"

    def test_embedded_source_url_takes_priority_in_resolve(self):
        """Embedded source_url in content should override the citation url field."""
        r = CitationLinkResolver(_make_settings())
        content = "source_url: https://direct.example.com/doc\nContent"
        citation = {"url": "https://some-blob.blob.core.windows.net/cont/file.md", "content": content}
        resolved = r.resolve(citation)
        # The embedded source_url is a direct URL (non-blob), so it's returned as-is
        assert resolved["source_url"] == "https://direct.example.com/doc"

    def test_resolve_all(self):
        r = CitationLinkResolver(_make_settings())
        citations = [
            {"url": "https://example.com/a", "content": ""},
            {"url": "https://example.com/b", "content": ""},
        ]
        results = r.resolve_all(citations)
        assert len(results) == 2
        assert all("source_url" in r for r in results)
