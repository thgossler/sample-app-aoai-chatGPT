#!/usr/bin/env python3
"""
SW360 MCP Server
================
Expose SW360 REST‑API data as Model Context Protocol (MCP) tools so that any MCP‑enabled chat client can query components, releases and vulnerabilities in real‑time.

See also: https://sw360.example.com/resource/docs/api-guide.html, https://sw360.example.com/group/guest/preferences

Prerequisites
-------------
```bash
pip install fastmcp requests  # installs FastMCP 2
export SW360_API_KEY="<your token>"
export SW360_URL_ROOT="https://sw360.example.com"
```
Then configure your chat client to use the MCP server, e.g. in `mcp.json`:
```json
{
    "servers": {
        "sw360-mcp-server": {
            "type": "stdio",
            "command": "${workspaceFolder}/scripts/python.exe",
            "args": [
                "${workspaceFolder}/backend/mcp_servers/sw360_mcp_server.py"
            ],
            "env": {
                "SW360_API_KEY": "${env:SW360_API_KEY}",
                "SW360_URL_ROOT": "${env:SW360_URL_ROOT}"
            },
        }
    }
}
```
"""
from __future__ import annotations

import os
import re
import urllib.parse
from typing import Any, Dict, Optional

import requests
from fastmcp import FastMCP

SW360_API_KEY_ENV = "SW360_API_KEY"
SW360_URL_ROOT_ENV = "SW360_URL_ROOT"

# ---------------------------------------------------------------------------
# Custom Exceptions
# ---------------------------------------------------------------------------

class SW360RequestError(Exception):
    """Raised for actual HTTP request errors (network, server errors, auth failures)"""
    def __init__(self, message: str, status_code: int = None, response_text: str = None):
        super().__init__(message)
        self.status_code = status_code
        self.response_text = response_text

class SW360NotFoundError(Exception):
    """Raised when a specific resource is not found (404) - this is expected behavior"""
    def __init__(self, message: str, resource_type: str = None):
        super().__init__(message)
        self.resource_type = resource_type

# ---------------------------------------------------------------------------
# Low‑level helpers (HTTP, URL building)
# ---------------------------------------------------------------------------

def _req_api_key() -> str:
    key = os.getenv(SW360_API_KEY_ENV)
    if not key:
        raise RuntimeError(
            f"Environment variable {SW360_API_KEY_ENV} must be set to query SW360.")
    return key


def _req_url_root() -> str:
    root = os.getenv(SW360_URL_ROOT_ENV)
    if not root:
        raise RuntimeError(
            f"Environment variable {SW360_URL_ROOT_ENV} must be set (e.g. https://sw360.example.com)")
    return root.rstrip("/")


def _build_query_url(base: str, params: Dict[str, str]) -> str:
    parsed = urllib.parse.urlparse(base)
    q = urllib.parse.parse_qs(parsed.query)
    q.update(params)
    return parsed._replace(query=urllib.parse.urlencode(q, doseq=True)).geturl()


def _send_get(url: str, api_key: str):
    """
    Send GET request to SW360 API with proper error handling.
    
    Raises:
        SW360RequestError: For actual HTTP request errors (network, server errors, auth failures)
        SW360NotFoundError: When resource is not found (404) - expected behavior
    
    Returns:
        dict: JSON response for successful requests (200)
    """
    headers = {"Authorization": f"Token {api_key}", "Accept": "application/json"}
    
    try:
        resp = requests.get(url, headers=headers, timeout=30)
    except requests.exceptions.RequestException as e:
        raise SW360RequestError(f"Network error when accessing {url}: {str(e)}")
    
    if resp.status_code == 200:
        return resp.json()
    elif resp.status_code == 404:
        # 404 is expected when resource doesn't exist - don't raise exception
        raise SW360NotFoundError(f"Resource not found: {url}")
    elif resp.status_code in [401, 403]:
        # Authentication/authorization errors
        raise SW360RequestError(
            f"Authentication failed for {url}: {resp.status_code}", 
            resp.status_code, 
            resp.text[:200]
        )
    elif resp.status_code >= 500:
        # Server errors
        raise SW360RequestError(
            f"Server error for {url}: {resp.status_code}", 
            resp.status_code, 
            resp.text[:200]
        )
    else:
        # Other client errors (400, etc.)
        raise SW360RequestError(
            f"Client error for {url}: {resp.status_code}", 
            resp.status_code, 
            resp.text[:200]
        )

# ---------------------------------------------------------------------------
# SW360 Client (logic ported 1‑to‑1 from the original Golang module)
# ---------------------------------------------------------------------------

class SW360Client:
    _re_name_from_bom = re.compile(r"^.*:(.*)@.*$")
    _re_version_from_bom = re.compile(r"^.*@(.*?)(?:\?.*)?$")

    def __init__(self, url_root: str, api_key: str):
        self.url_root = url_root.rstrip("/")
        self.api_key = api_key

    # -------------------------- internal -------------------------------
    def _get(self, url: str, params: Optional[Dict[str, str]] | None = None):
        """
        Internal GET method that handles SW360 API responses.
        
        Returns:
            dict: JSON response for successful requests
            None: When resource is not found (404)
        
        Raises:
            SW360RequestError: For actual HTTP request errors
        """
        if params:
            url = _build_query_url(url, params)
        
        try:
            return _send_get(url, self.api_key)
        except SW360NotFoundError:
            # Resource not found is expected behavior, return None
            return None

    def _add_html_url(self, item: Any, html_url: str):
        success = False
        try:
            if isinstance(item, dict):
                item["html_url"] = html_url
                if "_links" in item:
                    item["_links"]["alternate"] = {
                        "href": html_url,
                        "type": "text/html"
                    }
                success = True
        except Exception as e:
            success = False
        return success

    def _strip_quotes(self, text: str) -> str:
        """
        Remove leading and trailing quotes from a string.
        """
        if text.startswith('"') and text.endswith('"'):
            text = text[1:-1]
        if text.startswith("'") and text.endswith("'"):
            text = text[1:-1]
        return text

    # -------------------------- public API -----------------------------
    def get_project(self, project_id: str):
        """
        Get project by ID.
        
        Returns:
            dict: Project data if found
            None: If project not found
        """
        project_id = self._strip_quotes(project_id)
        url = f"{self.url_root}/resource/api/projects/{project_id}"
        return self._get(url, {"allDetails": "true"})

    def get_projects_by_name(self, project_name: str):
        """
        Search projects by name.
        
        Returns:
            list: List of matching projects (empty list if none found)
        """
        project_name = self._strip_quotes(project_name)
        url = f"{self.url_root}/resource/api/projects"
        project_name = "+".join(project_name.split())
        if not project_name:
            raise ValueError("Project name must not be empty")
        params = {
            "luceneSearch": "true", 
            "name": project_name, 
            "allDetails": "false", 
            "sort": "name,desc", 
            "page": "0", 
            "page_entries": "250"}
        
        result = self._get(url, params)
        if result is None:
            return []
            
        projects = []
        if isinstance(result, dict) and '_embedded' in result and 'sw360:projects' in result['_embedded']:
            projects = result['_embedded']['sw360:projects']
        elif isinstance(result, dict) and 'content' in result:
            projects = result['content']
        
        if not projects:
            return []
            
        page_url = f"{self.url_root}/group/guest/projects/-/project/detail/"
        for project in projects:
            if "name" in project and "version" in project:
                project["name"] = f"{project['name']} ({project['version']})"
            self._add_html_url(project, f"{page_url}{project['id']}")
        return projects

    def get_releases(self, project_id: str):
        """
        Get releases for a project.
        
        Returns:
            list: List of releases (empty list if none found)
        """
        project_id = self._strip_quotes(project_id)
        url = f"{self.url_root}/resource/api/projects/{project_id}/releases"
        result = self._get(url, {"transitive": "false", "page": "0", "page_entries": "250", "sort": "name,desc"})
        
        if result is None:
            return []
            
        # Extract releases from the response structure
        if isinstance(result, dict) and '_embedded' in result and 'sw360:releases' in result['_embedded']:
            return result['_embedded']['sw360:releases']
        elif isinstance(result, dict) and 'content' in result:
            return result['content']
        elif isinstance(result, list):
            return result
        
        return []

    def get_vulnerabilities(self, project_id: str):
        """
        Get vulnerabilities for a project.
        
        Returns:
            list: List of vulnerabilities (empty list if none found)
        """
        project_id = self._strip_quotes(project_id)
        url = f"{self.url_root}/resource/api/projects/{project_id}/vulnerabilities"
        result = self._get(url, {"page": "0", "page_entries": "250", "sort": "externalId"})
        
        if result is None:
            return []
            
        vulnerabilities = []
        if isinstance(result, dict) and '_embedded' in result and 'sw360:vulnerabilityDTOes' in result['_embedded']:
            vulnerabilities = result['_embedded']['sw360:vulnerabilityDTOes']
        elif isinstance(result, dict) and 'content' in result:
            vulnerabilities = result['content']
        elif isinstance(result, list):
            vulnerabilities = result
            
        if not vulnerabilities:
            return []
            
        page_url = f"{self.url_root}/group/guest/vulnerabilities?p_p_id=sw360_portlet_vulnerabilitites&p_p_lifecycle=0&_sw360_portlet_vulnerabilitites_pagename=detail&_sw360_portlet_vulnerabilitites_vulnerabilityId=$(VUL_ID)#/tab-Summary"
        for vulnerability in vulnerabilities:
            extId = vulnerability.get("externalId", None)
            self._add_html_url(vulnerability, page_url.replace("$(VUL_ID)", vulnerability.get("externalId", ""))) if extId else None
        vulnerabilities.sort(key=lambda v: (v.get("priority", ""), v.get("externalId", "")), reverse=True)
        return vulnerabilities

    def get_vulnerability_tracking_status(self, project_id: str):
        """
        Get vulnerability tracking status for a project.
        
        Returns:
            list: List of vulnerability tracking statuses (empty list if none found)
        """
        project_id = self._strip_quotes(project_id)
        url = f"{self.url_root}/resource/api/vulnerabilities/trackingStatus/{project_id}"
        result = self._get(url, {"page": "0", "page_entries": "250", "sort": "name,asc"})
        
        if result is None:
            return []
            
        vulnerabilityTrackingStatuses = []
        if isinstance(result, dict) and 'vulnerabilityTrackingStatus' in result:
            vulnerabilityTrackingStatuses = result['vulnerabilityTrackingStatus']
        elif isinstance(result, list):
            vulnerabilityTrackingStatuses = result
            
        if not vulnerabilityTrackingStatuses:
            return []
            
        page_url = f"{self.url_root}/group/guest/projects/-/project/detail/$(PROJECT_ID)#/tab-VulnerabilityTrackingsStatus"
        for vulnerabilityTrackingStatus in vulnerabilityTrackingStatuses:
            self._add_html_url(vulnerabilityTrackingStatus, page_url.replace("$(PROJECT_ID)", project_id))
        return vulnerabilityTrackingStatuses

    def search_package(self, name: str, version: str | None = None, package_manager: str | None = None, package_url: str | None = None):
        """
        Search packages by name and optional filters.
        
        Returns:
            list: List of matching packages (empty list if none found)
        """
        name = self._strip_quotes(name)
        version = self._strip_quotes(version) if version else None
        package_manager = self._strip_quotes(package_manager) if package_manager else None
        package_url = self._strip_quotes(package_url) if package_url else None
        url = f"{self.url_root}/resource/api/packages"
        params = {"name": name, "allDetails": "true", "sort": "name,desc"}
        if not name:
            raise ValueError("Package name must not be empty")
        if version:
            params["version"] = version
        if package_manager:
            params["packageManager"] = package_manager
        if package_url:
            params["packageUrl"] = package_url
            
        result = self._get(url, params)
        if result is None:
            return []
            
        packages = []
        if isinstance(result, dict) and '_embedded' in result and 'sw360:packages' in result['_embedded']:
            packages = result['_embedded']['sw360:packages']
        elif isinstance(result, dict) and 'content' in result:
            packages = result['content']
        elif isinstance(result, list):
            packages = result
            
        if not packages:
            return []
            
        page_url = f"{self.url_root}/group/guest/packages?p_p_id=sw360_portlet_packages&p_p_lifecycle=0&_sw360_portlet_packages_pagename=detail&_sw360_portlet_packages_packageId=$(PACKAGE_ID)#/tab-Summary"
        for package in packages:
            self._add_html_url(package, page_url.replace("$(PACKAGE_ID)", package.get("id", "")))
        packages.sort(key=lambda p: p.get("version", ""), reverse=True)
        return packages

    def get_package(self, href: str):
        """
        Get package details by href.
        
        Returns:
            dict: Package data if found
            None: If package not found
        """
        href = self._strip_quotes(href)
        return self._get(href, {"allDetails": "true"})

    def get_release(self, release_id: str):
        """
        Get release details by ID.
        
        Returns:
            dict: Release data if found
            None: If release not found
        """
        release_id = self._strip_quotes(release_id)
        url = f"{self.url_root}/resource/api/releases/{release_id}"
        return self._get(url, {"allDetails": "true"})

    # ---------------------- convenience helpers -----------------------
    @classmethod
    def parse_bom_ref(cls, bom_ref: str):
        name = cls._re_name_from_bom.sub(r"\1", bom_ref)
        version = cls._re_version_from_bom.sub(r"\1", bom_ref)
        return urllib.parse.unquote(name), urllib.parse.unquote(version)

# ---------------------------------------------------------------------------
# FastMCP Server Setup
# ---------------------------------------------------------------------------

mcp = FastMCP(
    name="SW360 MCP Server",
    description="SW360 tools for projects, packages, releases and vulnerabilities",
    version="0.1.2",
)


def _client() -> SW360Client:
    return SW360Client(_req_url_root(), _req_api_key())


# --------------------------- MCP tools -------------------------------------

@mcp.tool(name="get_project", description="Return the SW360 project object given its ID (with full details including a list of linked packages).")
def get_project(project_id: str):
    """Fetch a project with allDetails=true"""
    result = _client().get_project(project_id)
    if result is None:
        return {"error": "Project not found", "project_id": project_id}
    return result


@mcp.tool(name="get_projects_by_name", description="Return the list of SW360 project objects which contain the given name in their name (only the first 250 results, and not all object details).")
def get_projects_by_name(project_name: str):
    """Fetch a list of projects matching the given name"""
    return _client().get_projects_by_name(project_name)


@mcp.tool(name="get_releases", description="Return releases attached to a project.")
def get_releases(project_id: str):
    json_response = _client().get_releases(project_id)
    # The client method now returns a list directly or empty list
    return json_response


@mcp.tool(name="get_vulnerabilities", description="Return the vulnerabilities for the given project.")
def get_vulnerabilities(project_id: str):
    json_response = _client().get_vulnerabilities(project_id)
    # The client method now returns a list directly or empty list
    return json_response


@mcp.tool(name="get_vulnerability_tracking_status", description="Return the vulnerability tracking status for the given project with all linked packages.")
def get_vulnerability_tracking_status(project_id: str):
    json_response = _client().get_vulnerability_tracking_status(project_id)
    # The client method now returns a list directly or empty list
    return json_response


@mcp.tool(name="search_package", description="Search packages by name (optionally version, packageManager, packageUrl).")
def search_package(name: str, version: str | None = None, package_manager: str | None = None, package_url: str | None = None):
    json_response = _client().search_package(name, version, package_manager, package_url)
    # The client method now returns a list directly or empty list
    return json_response


@mcp.tool(name="get_package", description="Return a full package record given its self‑link HREF.")
def get_package(package_href: str):
    result = _client().get_package(package_href)
    if result is None:
        return {"error": "Package not found", "href": package_href}
    return result


@mcp.tool(name="get_release", description="Return the release object for a given release ID.")
def get_release(release_id: str):
    result = _client().get_release(release_id)
    if result is None:
        return {"error": "Release not found", "release_id": release_id}
    return result


# ---------------------------------------------------------------------------
# Entrypoint – run via STDIO transport (works with most local MCP clients)
#
# Usage:
#   python sw360_mcp_server.py           # Start as MCP server (default)
#   python sw360_mcp_server.py --test    # Run built-in MCP tool tests
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys
    def _test_mcp_tools():
        import sys
        import traceback
        def print_start_test(label):
            BLUE = "\033[94m"
            RESET = "\033[0m"
            print(f"\n{BLUE}[TEST]{RESET} {label}")

        def print_result(success, label, error=None, result=None):
            GREEN = "\033[92m"
            RED = "\033[91m"
            RESET = "\033[0m"
            GRAY = "\033[90m"
            if result:
                print(f"{GRAY}{result}{RESET}")
            if success:
                print(f"{GREEN}[SUCCESS]{RESET} {label}")
            else:
                print(f"{RED}[FAILED]{RESET} {label}")
                if error:
                    print(f"    {error}")

        print("Testing MCP tools...")
        all_success = True

        # Create client instance for testing
        client = _client()

        # get_project
        print_start_test("get_project")
        try:
            result = client.get_project("0145bc3754bd42e2902043d8cc2369f7")
            if result is None:
                result = {"error": "Project not found", "project_id": "0145bc3754bd42e2902043d8cc2369f7"}
            success = bool(result) and ("name" in result and "version" in result) or ("error" in result)
            print_result(success, "get_project", result=result)
            all_success = all_success and success
        except Exception as e:
            print_result(False, "get_project", e)
            all_success = False

        # get_projects_by_name
        print_start_test("get_projects_by_name")
        try:
            result = client.get_projects_by_name("CorePlatform AuditService")
            success = isinstance(result, list) and (len(result) == 0 or all("name" in project for project in result))
            print_result(success, "get_projects_by_name", result=result)
            all_success = all_success and success
        except Exception as e:
            print_result(False, "get_projects_by_name", e)
            all_success = False

        # get_releases
        print_start_test("get_releases")
        try:
            result = client.get_releases("0145bc3754bd42e2902043d8cc2369f7")
            success = isinstance(result, list) and (len(result) == 0 or all("id" in release for release in result))
            print_result(success, "get_releases", result=result)
            all_success = all_success and success
        except Exception as e:
            print_result(False, "get_releases", e)
            all_success = False
        
        # get_vulnerabilities
        print_start_test("get_vulnerabilities")
        try:
            result = client.get_vulnerabilities("0145bc3754bd42e2902043d8cc2369f7")
            success = isinstance(result, list) and (len(result) == 0 or all("externalId" in vuln for vuln in result))
            print_result(success, "get_vulnerabilities", result=result)
            all_success = all_success and success
        except Exception as e:
            print_result(False, "get_vulnerabilities", e)
            all_success = False

        # search_package
        print_start_test("search_package")
        first_package = None
        try:
            packages = client.search_package("Microsoft.AspNetCore.Authentication.Core")
            first_package = next((pkg for pkg in packages if "releaseId" in pkg), None)
            first_package = first_package or packages[0] if packages else None
            success = isinstance(packages, list) and (len(packages) == 0 or all("name" in pkg for pkg in packages))
            print_result(success, "search_package", result=packages)
            all_success = all_success and success
        except Exception as e:
            print_result(False, "search_package", e)
            all_success = False

        # get_package
        print_start_test("get_package")
        try:
            if first_package:
                result = client.get_package(first_package["_links"]["self"]["href"])
                if result is None:
                    result = {"error": "Package not found", "href": first_package["_links"]["self"]["href"]}
                success = bool(result) and (("name" in result and "version" in result) or "error" in result)
                print_result(success, "get_package", result=result)
                all_success = all_success and success
            else:
                print_result(False, "get_package", "No package found to test get_package.")
                all_success = False
        except Exception as e:
            print_result(False, "get_package", e)
            all_success = False

        # get_release
        print_start_test("get_release")
        try:
            if first_package and "releaseId" in first_package:
                result = client.get_release(first_package["releaseId"])
                if result is None:
                    result = {"error": "Release not found", "release_id": first_package["releaseId"]}
                success = bool(result) and (("id" in result and "name" in result) or "error" in result)
                print_result(success, "get_release", result=result)
                all_success = all_success and success
            else:
                print_result(False, "get_release", "No package found to test get_release.")
                all_success = False
        except Exception as e:
            print_result(False, "get_release", e)
            all_success = False

        print("\nMCP tool tests completed.")
        if all_success:
            print("\033[92mAll tests passed.\033[0m")
            sys.exit(0)
        else:
            print("\033[91mSome tests failed.\033[0m")
            sys.exit(-1)

    if len(sys.argv) > 1 and sys.argv[1] == "--test":
        _test_mcp_tools()
    else:
        mcp.run()
