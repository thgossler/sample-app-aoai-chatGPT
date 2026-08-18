"""
Server-side citation link resolver.

Mirrors the frontend ``onShowCitation()`` / ``updateCitation()`` logic
(Chat.tsx, Answer.tsx) in Python so that MCP tool results contain
fully-resolved, user-facing URLs — not raw blob storage URLs.

Resolution logic (same priority as frontend):

1. Extract embedded metadata from chunk content
   (source_url, source_title, source_file, chunk_index, chunk_total)
2. For Wiki / Markdown files  → construct a DevOps Wiki URL
3. For PDF files              → append a pre-signed SAS token
4. For other blob storage URLs→ return as-is (no SAS required)
5. For direct (non-blob) URLs → return the URL unchanged

Configuration comes from ``_CitationFileSettings``:
- ``storage_base_url``   — base URL of the blob container
                           (AZURE_SEARCH_CITATION_FILE_STORAGE_BASE_URL)
- ``link_base_url``      — user-facing base URL (DevOps Wiki, document portal…)
                           (AZURE_SEARCH_CITATION_FILE_LINK_BASE_URL)
- ``link_url_appendix``  — suffix appended to constructed links
                           (AZURE_SEARCH_CITATION_FILE_LINK_URL_APPENDIX)
- ``storage_account_key``— used for SAS token generation
                           (AZURE_SEARCH_CITATION_FILE_STORAGE_ACCOUNT_KEY)
"""

import logging
import re
import urllib.parse
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


class CitationLinkResolver:
    """
    Resolves raw citation blob URLs to user-facing links.

    Parameters
    ----------
    citation_file_settings:
        An ``_CitationFileSettings`` instance from ``backend.settings``.
    """

    # Regex patterns for embedded metadata lines injected by the
    # data-preparation scripts into chunk content.
    _META_PATTERNS = {
        "source_url": re.compile(r"^source_url:\s*(.+)$", re.MULTILINE),
        "source_title": re.compile(r"^source_title:\s*(.+)$", re.MULTILINE),
        "source_file": re.compile(r"^source_file:\s*(.+)$", re.MULTILINE),
        "chunk_index": re.compile(r"^chunk_index:\s*(\d+)$", re.MULTILINE),
        "chunk_total": re.compile(r"^chunk_total:\s*(\d+)$", re.MULTILINE),
    }

    # Pattern used to locate the first markdown heading in chunk content.
    _HEADING_RE = re.compile(r"^#{1,6}\s+(.*)$", re.MULTILINE)

    # Metadata/frontmatter line patterns to strip from content.
    _FRONTMATTER_RE = re.compile(
        r"^(?:---\s*$|source_url:\s*.+$|source_title:\s*.+$|source_file:\s*.+$"
        r"|chunk_index:\s*\d+$|chunk_total:\s*\d+$)",
        re.MULTILINE,
    )

    def __init__(self, citation_file_settings):
        self._settings = citation_file_settings

        raw_base = getattr(citation_file_settings, "storage_base_url", None) or ""
        self.storage_base_url: str = raw_base.rstrip("/")

        # Derive the blob container prefix the same way the frontend does:
        # protocol + host + '/' + first path segment.
        self.blob_container_prefix: str = self._derive_blob_container_prefix(
            self.storage_base_url
        )

        self.link_base_url: Optional[str] = (
            getattr(citation_file_settings, "link_base_url", None) or None
        )
        self.link_url_appendix: str = (
            getattr(citation_file_settings, "link_url_appendix", None) or ""
        )
        self.storage_account_key: Optional[str] = (
            getattr(citation_file_settings, "storage_account_key", None) or None
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def resolve(self, citation: Dict[str, Any]) -> Dict[str, Any]:
        """
        Resolve a single citation dict and return an enriched copy.

        The returned dict will have:
        - ``source_url``  — fully-resolved, user-facing URL
        - ``source_type`` — one of ``"wiki"``, ``"pdf"``, ``"blob"``,
          ``"url"`` (direct / non-blob)
        - ``clean_content`` — chunk text with frontmatter metadata stripped

        All original keys are preserved.
        """
        resolved = dict(citation)

        # 1. Extract embedded metadata (may override url/title/filepath)
        resolved = self._extract_embedded_metadata(resolved)

        raw_url: str = resolved.get("url") or citation.get("url") or ""

        if not raw_url:
            resolved["source_url"] = None
            resolved["source_type"] = None
            return resolved

        # 2. Resolve URL by document type
        if self._is_wiki_or_markdown(raw_url):
            resolved["source_url"] = self._resolve_wiki_url(raw_url)
            resolved["source_type"] = "wiki"
        elif raw_url.lower().endswith(".pdf"):
            resolved["source_url"] = self._resolve_pdf_url(raw_url)
            resolved["source_type"] = "pdf"
        elif self._is_blob_storage_url(raw_url):
            # Other blob file — return as-is (no SAS; not a sensitive URL)
            resolved["source_url"] = raw_url
            resolved["source_type"] = "blob"
        else:
            resolved["source_url"] = raw_url
            resolved["source_type"] = "url"

        # 3. Enrich title with heading from content (mirrors frontend updateCitation)
        resolved = self._enrich_title_with_heading(resolved)

        # 4. Provide clean content (strip frontmatter/metadata lines)
        resolved["clean_content"] = self._strip_frontmatter(
            citation.get("content", "")
        )

        return resolved

    def resolve_all(self, citations: list[Dict[str, Any]]) -> list[Dict[str, Any]]:
        """Resolve all citations in a list."""
        return [self.resolve(c) for c in citations]

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _derive_blob_container_prefix(storage_base_url: str) -> str:
        """Return ``protocol + host + '/' + first_path_segment``.

        The frontend (Chat.tsx) parses ``FileStorageBaseUrl`` as a ``URL``
        object and reconstructs the prefix this way.  We replicate that to
        guarantee consistent relative-path extraction.
        """
        if not storage_base_url:
            return ""
        parsed = urllib.parse.urlparse(storage_base_url)
        first_seg = (
            parsed.path.strip("/").split("/")[0] if parsed.path.strip("/") else ""
        )
        if first_seg:
            return f"{parsed.scheme}://{parsed.netloc}/{first_seg}"
        return f"{parsed.scheme}://{parsed.netloc}"

    # ------------------------------------------------------------------
    # Embedded metadata extraction
    # ------------------------------------------------------------------

    def _extract_embedded_metadata(self, citation: Dict[str, Any]) -> Dict[str, Any]:
        """
        Parse metadata lines from chunk content and promote them to top-level
        fields on the citation dict (overriding url / title / filepath).
        """
        content: str = citation.get("content", "")
        if not content:
            return citation

        result = dict(citation)

        for field, pattern in self._META_PATTERNS.items():
            match = pattern.search(content)
            if not match:
                continue
            value = match.group(1).strip()
            if field == "source_url":
                result["url"] = value
            elif field == "source_title":
                result["title"] = value
            elif field == "source_file":
                result["filepath"] = value
            elif field == "chunk_index":
                result["part_index"] = int(value)
            elif field == "chunk_total":
                result["chunk_total"] = int(value)

        return result

    def _enrich_title_with_heading(self, citation: Dict[str, Any]) -> Dict[str, Any]:
        """Append the first markdown heading to the title when the URL has a
        ``#`` fragment.  Mirrors the frontend ``updateCitation()`` behaviour in
        ``Answer.tsx``.
        """
        url: str = citation.get("source_url") or citation.get("url") or ""
        content: str = citation.get("content", "")
        title: str = citation.get("title") or ""

        if "#" not in url or not content:
            return citation

        match = self._HEADING_RE.search(content)
        if match:
            heading = match.group(1).strip()
            # Only append if the heading text isn't already present
            if heading and not title.endswith(heading):
                citation["title"] = f"{title} - {heading}" if title else heading

        return citation

    @staticmethod
    def _strip_frontmatter(content: str) -> str:
        """Remove metadata / YAML-frontmatter lines from chunk content."""
        if not content:
            return content
        cleaned = CitationLinkResolver._FRONTMATTER_RE.sub("", content)
        # Collapse leading blank lines left behind
        return cleaned.lstrip("\n")

    # ------------------------------------------------------------------
    # URL type detection
    # ------------------------------------------------------------------

    def _is_wiki_or_markdown(self, url: str) -> bool:
        parsed = urllib.parse.urlsplit(url)
        url_lower = parsed.path.lower()
        return (
            "_wiki" in url_lower
            or url_lower.endswith(".md")
        )

    def _is_blob_storage_url(self, url: str) -> bool:
        return "blob.core.windows.net" in url.lower()

    # ------------------------------------------------------------------
    # URL resolution strategies
    # ------------------------------------------------------------------

    def _resolve_wiki_url(self, blob_url: str) -> str:
        """
        Convert a blob storage URL to a DevOps Wiki / document portal URL.

        Mirrors frontend logic in Chat.tsx ``onShowCitation()`` — Wiki path.
        """
        if not self.blob_container_prefix or not self.link_base_url:
            logger.debug(
                "Wiki URL resolution skipped (missing storage_base_url or link_base_url)"
            )
            return blob_url

        # Compare the parsed origin and container path so query strings and
        # path-prefix lookalikes cannot affect relative-path extraction.
        blob_parts = urllib.parse.urlsplit(blob_url)
        prefix_parts = urllib.parse.urlsplit(self.blob_container_prefix)
        prefix_path = prefix_parts.path.rstrip("/")
        if (
            blob_parts.scheme.lower() != prefix_parts.scheme.lower()
            or blob_parts.netloc.lower() != prefix_parts.netloc.lower()
            or not blob_parts.path.lower().startswith(
                f"{prefix_path.lower()}/"
            )
        ):
            # URL doesn't match blob storage — open directly (same as frontend)
            return blob_url

        rel_path = blob_parts.path[len(prefix_path):]

        # DevOps Wiki convention: strip .md extension
        if "_wiki" in self.link_base_url.lower():
            rel_path = re.sub(r"__\d+(?=\.md$)", "", rel_path, flags=re.IGNORECASE)
            rel_path = re.sub(r"\.md$", "", rel_path, flags=re.IGNORECASE)

        encoded = urllib.parse.quote(rel_path, safe="")
        return f"{self.link_base_url}{encoded}{self.link_url_appendix}"

    def _resolve_pdf_url(self, blob_url: str) -> str:
        """
        Append a SAS token to a PDF blob URL.

        Mirrors frontend logic in Chat.tsx ``onShowCitation()`` — PDF path.
        Falls back to plain URL if SAS generation is not configured.
        """
        sas_token = self._generate_sas_token(blob_url)
        if sas_token:
            return f"{blob_url}?{sas_token}"
        return blob_url

    def _generate_sas_token(self, blob_url: str) -> Optional[str]:
        """
        Generate a container-level read SAS token valid for 1 hour.

        Returns ``None`` if not all required configuration is present.
        """
        if not self.storage_base_url or not self.storage_account_key:
            return None

        try:
            from azure.storage.blob import (
                ContainerSasPermissions,
                generate_container_sas,
            )
            from urllib.parse import urlparse

            parsed = urlparse(self.storage_base_url)
            # Extract account name from hostname (accountname.blob.core.windows.net)
            account_name = parsed.hostname.split(".")[0]
            # Container name is the first path segment
            container_name = parsed.path.strip("/").split("/")[0]

            expiry = datetime.now(tz=timezone.utc) + timedelta(hours=1)

            sas = generate_container_sas(
                account_name=account_name,
                container_name=container_name,
                account_key=self.storage_account_key,
                permission=ContainerSasPermissions(read=True),
                expiry=expiry,
            )
            return sas
        except Exception as exc:
            logger.warning("SAS token generation failed: %s", exc)
            return None
