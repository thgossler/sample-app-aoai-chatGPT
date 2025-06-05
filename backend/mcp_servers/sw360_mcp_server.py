#!/usr/bin/env python3
"""
SW360 MCP Server
================
Expose SW360 REST‑API data as Model Context Protocol (MCP) tools so that any MCP‑enabled chat client can query components, releases and vulnerabilities in real‑time.

See also: https://sw360.example.com/resource/docs/api-guide.html

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
            "command": "${workspaceFolder}/.venv/Scripts/python.exe",
            "args": [
                "${workspaceFolder}/sw360_mcp_server.py"
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
    headers = {"Authorization": f"Token {api_key}", "Accept": "application/json"}
    resp = requests.get(url, headers=headers, timeout=30)
    if resp.status_code != 200:
        raise RuntimeError(f"GET {url} -> {resp.status_code}: {resp.text[:200]}")
    return resp.json()

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
        if params:
            url = _build_query_url(url, params)
        return _send_get(url, self.api_key)

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

    # -------------------------- public API -----------------------------
    def get_project(self, project_id: str):
        url = f"{self.url_root}/resource/api/projects/{project_id}"
        return self._get(url, {"allDetails": "true"})

    def get_projects_by_name(self, project_name: str):
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
        projects = self._get(url, params)
        if isinstance(projects, dict) and '_embedded' in projects and 'sw360:projects' in projects['_embedded']:
            projects = projects['_embedded']['sw360:projects']
        elif isinstance(projects, dict) and 'content' in projects:
            projects = projects['content']
        if not projects:
            raise ValueError(f"No project found with name '{project_name}'")
        page_url = f"{self.url_root}/group/guest/projects/-/project/detail/"
        for project in projects:
            if "name" in project and "version" in project:
                project["name"] = f"{project['name']} ({project['version']})"
            self._add_html_url(project, f"{page_url}{project['id']}")
        return projects

    def get_releases(self, project_id: str):
        url = f"{self.url_root}/resource/api/projects/{project_id}/releases"
        return self._get(url, {"transitive": "false", "page": "0", "page_entries": "250", "sort": "name,desc"})

    def get_vulnerabilities(self, project_id: str):
        url = f"{self.url_root}/resource/api/projects/{project_id}/vulnerabilities"
        vulnerabilities = self._get(url, {"page": "0", "page_entries": "250", "sort": "externalId"})
        if isinstance(vulnerabilities, dict) and '_embedded' in vulnerabilities and 'sw360:vulnerabilityDTOes' in vulnerabilities['_embedded']:
            vulnerabilities = vulnerabilities['_embedded']['sw360:vulnerabilityDTOes']
        elif isinstance(vulnerabilities, dict) and 'content' in vulnerabilities:
            vulnerabilities = vulnerabilities['content']
        if not vulnerabilities:
            raise ValueError(f"No vulnerabilities found for project '{project_id}'")
        page_url = f"{self.url_root}/group/guest/vulnerabilities?p_p_id=sw360_portlet_vulnerabilitites&p_p_lifecycle=0&_sw360_portlet_vulnerabilitites_pagename=detail&_sw360_portlet_vulnerabilitites_vulnerabilityId=$(VUL_ID)#/tab-Summary"
        for vulnerability in vulnerabilities:
            extId = vulnerability.get("externalId", None)
            self._add_html_url(vulnerability, page_url.replace("$(VUL_ID)", vulnerability.get("externalId", ""))) if extId else None
        vulnerabilities.sort(key=lambda v: (v.get("priority", ""), v.get("externalId", "")), reverse=True)
        return vulnerabilities

    def get_vulnerability_tracking_status(self, project_id: str):
        url = f"{self.url_root}/resource/api/vulnerabilities/trackingStatus/{project_id}"
        vulnerabilityTrackingStatuses = self._get(url, {"page": "0", "page_entries": "250", "sort": "name,asc"})
        if isinstance(vulnerabilityTrackingStatuses, dict) and 'vulnerabilityTrackingStatus' in vulnerabilityTrackingStatuses:
            vulnerabilityTrackingStatuses = vulnerabilityTrackingStatuses['vulnerabilityTrackingStatus']
        if not vulnerabilityTrackingStatuses:
            raise ValueError(f"No vulnerability tracking status found for project '{project_id}'")
        page_url = f"{self.url_root}/group/guest/projects/-/project/detail/$(PROJECT_ID)#/tab-VulnerabilityTrackingsStatus"
        for vulnerabilityTrackingStatus in vulnerabilityTrackingStatuses:
            self._add_html_url(vulnerabilityTrackingStatus, page_url.replace("$(PROJECT_ID)", project_id))
        return vulnerabilityTrackingStatuses

    def search_package(self, name: str, version: str | None = None, package_manager: str | None = None, package_url: str | None = None):
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
        packages = self._get(url, params)
        if isinstance(packages, dict) and '_embedded' in packages and 'sw360:packages' in packages['_embedded']:
            packages = packages['_embedded']['sw360:packages']
        elif isinstance(packages, dict) and 'content' in packages:
            packages = packages['content']
        if not packages:
            raise ValueError(f"No packages found with name '{name}'")
        page_url = f"{self.url_root}/group/guest/packages?p_p_id=sw360_portlet_packages&p_p_lifecycle=0&_sw360_portlet_packages_pagename=detail&_sw360_portlet_packages_packageId=$(PACKAGE_ID)#/tab-Summary"
        for package in packages:
            self._add_html_url(package, page_url.replace("$(PACKAGE_ID)", package.get("id", "")))
        packages.sort(key=lambda p: p.get("version", ""), reverse=True)
        return packages

    def get_package(self, href: str):
        return self._get(href, {"allDetails": "true"})

    def get_release(self, release_id: str):
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
    version="0.1.1",
)


def _client() -> SW360Client:
    return SW360Client(_req_url_root(), _req_api_key())


# --------------------------- MCP tools -------------------------------------

@mcp.tool(name="get_project", description="Return the SW360 project object given its ID (with full details including a list of linked packages).")
def get_project(project_id: str):
    """Fetch a project with allDetails=true"""
    return _client().get_project(project_id)


@mcp.tool(name="get_projects_by_name", description="Return the list of SW360 project objects which contain the given name in their name (only the first 250 results, and not all object details).")
def get_projects_by_name(project_name: str):
    """Fetch a list of projects matching the given name"""
    return _client().get_projects_by_name(project_name)


@mcp.tool(name="get_releases", description="Return releases attached to a project.")
def get_releases(project_id: str):
    json_response = _client().get_releases(project_id)
    if isinstance(json_response, dict) and '_embedded' in json_response and 'sw360:releases' in json_response['_embedded']:
        json_response = json_response['_embedded']['sw360:releases']
    elif isinstance(json_response, dict) and 'content' in json_response:
        json_response = json_response['content']
    return json_response


@mcp.tool(name="get_vulnerabilities", description="Return the vulnerabilities for the given project.")
def get_vulnerabilities(project_id: str):
    json_response = _client().get_vulnerabilities(project_id)
    if isinstance(json_response, dict) and '_embedded' in json_response and 'sw360:vulnerabilityDTOes' in json_response['_embedded']:
        json_response = json_response['_embedded']['sw360:vulnerabilityDTOes']
    elif isinstance(json_response, dict) and 'content' in json_response:
        json_response = json_response['content']
    return json_response


@mcp.tool(name="get_vulnerability_tracking_status", description="Return the vulnerability tracking status for the given project with all linked packages.")
def get_vulnerability_tracking_status(project_id: str):
    json_response = _client().get_vulnerability_tracking_status(project_id)
    if isinstance(json_response, dict) and '_embedded' in json_response and 'sw360:vulnerabilityTrackingStatus' in json_response['_embedded']:
        json_response = json_response['_embedded']['sw360:vulnerabilityTrackingStatus']
    elif isinstance(json_response, dict) and 'content' in json_response:
        json_response = json_response['content']
    return json_response


@mcp.tool(name="search_package", description="Search packages by name (optionally version, packageManager, packageUrl).")
def search_package(name: str, version: str | None = None, package_manager: str | None = None, package_url: str | None = None):
    json_response = _client().search_package(name, version, package_manager, package_url)
    # extract the list of items from the response where it is wrapped in { '_embedded': { 'sw360:packages': [ { ... }, ... ] } }
    if isinstance(json_response, dict) and 'content' in json_response:
        json_response = json_response['content']
    elif isinstance(json_response, dict) and '_embedded' in json_response and 'sw360:packages' in json_response['_embedded']:
        json_response = json_response['_embedded']['sw360:packages']
    return json_response


@mcp.tool(name="get_package", description="Return a full package record given its self‑link HREF.")
def get_package(package_href: str):
    return _client().get_package(package_href)


@mcp.tool(name="get_release", description="Return the release object for a given release ID.")
def get_release(release_id: str):
    return _client().get_release(release_id)


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

        # get_project
        print_start_test("get_project")
        try:
            result = get_project("0145bc3754bd42e2902043d8cc2369f7")
            success = bool(result) and "name" in result and "version" in result
            print_result(success, "get_project", result=result)
            all_success = all_success and success
        except Exception as e:
            print_result(False, "get_project", e)
            all_success = False

        # get_projects_by_name
        print_start_test("get_projects_by_name")
        try:
            result = get_projects_by_name("CorePlatform AuditService")
            success = bool(result) and isinstance(result, list) and len(result) > 0 and all("name" in project and "version" in project for project in result)
            print_result(success, "get_projects_by_name", result=result)
            all_success = all_success and success
        except Exception as e:
            print_result(False, "get_projects_by_name", e)
            all_success = False

        # get_releases
        print_start_test("get_releases")
        try:
            result = get_releases("0145bc3754bd42e2902043d8cc2369f7")
            success = bool(result) and isinstance(result, list) and len(result) > 0 and all("id" in release and "name" in release for release in result)
            print_result(success, "get_releases", result=result)
            all_success = all_success and success
        except Exception as e:
            print_result(False, "get_releases", e)
            all_success = False
        
        # get_vulnerabilities
        print_start_test("get_vulnerabilities")
        try:
            result = get_vulnerabilities("0145bc3754bd42e2902043d8cc2369f7")
            success = isinstance(result, list) and len(result) > 0 and all("externalId" in vuln for vuln in result)
            print_result(success, "get_vulnerabilities", result=result)
            all_success = all_success and success
        except Exception as e:
            print_result(False, "get_vulnerabilities", e)
            all_success = False

        # search_package
        print_start_test("search_package")
        first_package = None
        try:
            packages = search_package("Microsoft.AspNetCore.Authentication.Core")
            first_package = next((pkg for pkg in packages if "releaseId" in pkg), None)
            first_package = first_package or packages[0] if packages else None
            success = bool(packages) and isinstance(packages, list) and len(packages) > 0 and all("name" in pkg and "version" in pkg for pkg in packages)
            print_result(success, "search_package", result=packages)
            all_success = all_success and success
        except Exception as e:
            print_result(False, "search_package", e)
            all_success = False

        # get_package
        print_start_test("get_package")
        try:
            if first_package:
                result = get_package(first_package["_links"]["self"]["href"])
                success = bool(result) and "name" in result and "version" in result
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
                result = get_release(first_package["releaseId"])
                success = bool(result) and "id" in result and "name" in result and "version" in result
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
