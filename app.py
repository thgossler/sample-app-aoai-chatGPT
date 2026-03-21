import datetime
import copy
import json
import os
import logging
import uuid
import asyncio
import httpx
from flask import Flask, Response, request, jsonify, send_from_directory
from dotenv import load_dotenv
from azure.identity import DefaultAzureCredential
from azure.storage.blob import ContainerClient, BlobServiceClient, generate_container_sas, ContainerSasPermissions
from urllib.parse import urlparse
from quart import (
    Blueprint,
    Quart,
    jsonify,
    make_response,
    request,
    send_from_directory,
    render_template,
    current_app,
)
from openai import AsyncAzureOpenAI
from azure.identity.aio import (
    DefaultAzureCredential,
    get_bearer_token_provider
)
from backend.auth.auth_utils import get_authenticated_user_details
from backend.security.ms_defender_utils import get_msdefender_user_json
from backend.history.cosmosdbservice import CosmosConversationClient
from backend.settings import (
    app_settings,
    MINIMUM_SUPPORTED_AZURE_OPENAI_PREVIEW_API_VERSION
)
from backend.utils import (
    format_as_ndjson,
    format_stream_response,
    format_non_streaming_response,
    convert_to_pf_format,
    format_pf_non_streaming_response,
)
from fastmcp import Client
from backend.mcp_manager import MCPServerManager
from backend.rag_service import init_rag_service, get_rag_service
from backend.mcp_server.remote_mcp_server import RemoteMCPServer
from backend.mcp_server.citation_resolver import CitationLinkResolver


class _MCPASGIDispatch:
    """
    ASGI middleware that routes ``/mcp`` (Streamable HTTP) and
    ``/sse`` / ``/messages`` (SSE legacy) requests to the FastMCP
    Starlette app and forwards everything else to the Quart ASGI app.

    This is needed because FastMCP uses Starlette's streaming / SSE transport
    which must own the full ASGI lifecycle for ``/mcp`` requests — Quart's
    router cannot support SSE streaming natively via its route handlers.

    **Lifespan handling**: the ``lifespan`` ASGI scope is forwarded to the
    Quart app only (Quart drives startup/shutdown).  The FastMCP Starlette app's
    lifespan is bootstrapped separately via ``bootstrap_lifespan()`` which must
    be called inside Quart's ``before_serving`` hook.

    The ``/.well-known/oauth-protected-resource`` discovery endpoint continues
    to be served by Quart (no auth required, simple JSON response).
    """

    def __init__(self, quart_asgi, mcp_asgi, sse_asgi=None):
        self._quart = quart_asgi
        self._mcp = mcp_asgi
        self._sse = sse_asgi  # optional SSE backward-compat app
        self._mcp_lifespan_ctx = None  # holds the running lifespan context

    async def bootstrap_lifespan(self) -> None:
        """Enter the FastMCP Starlette app's lifespan context manager.

        FastMCP's ``StreamableHTTPSessionManager`` starts an anyio task group
        during ASGI lifespan startup.  Because Quart owns the outer ASGI
        lifespan, we must manually trigger the FastMCP startup here instead
        of relying on ASGI scope forwarding.

        Call this exactly once from Quart's ``before_serving`` hook, *after*
        the ASGI middleware has been installed on the app.
        """
        mcp_app = self._mcp
        # StarletteWithLifespan exposes a .lifespan property which is an
        # async context manager.  Entering it runs all on_startup handlers,
        # including the task group initialisation in StreamableHTTPSessionManager.
        lifespan_cm = mcp_app.lifespan(mcp_app)  # type: ignore[arg-type]
        self._mcp_lifespan_ctx = lifespan_cm
        await lifespan_cm.__aenter__()
        logging.info("FastMCP Starlette lifespan started")

        if self._sse is not None:
            sse_lifespan_cm = self._sse.lifespan(self._sse)  # type: ignore[arg-type]
            self._sse_lifespan_ctx = sse_lifespan_cm
            await sse_lifespan_cm.__aenter__()
            logging.info("FastMCP SSE lifespan started")

    async def shutdown_lifespan(self) -> None:
        """Exit the FastMCP lifespan context (call from Quart's after_serving)."""
        if self._mcp_lifespan_ctx is not None:
            await self._mcp_lifespan_ctx.__aexit__(None, None, None)
            self._mcp_lifespan_ctx = None
        if getattr(self, "_sse_lifespan_ctx", None) is not None:
            await self._sse_lifespan_ctx.__aexit__(None, None, None)
            self._sse_lifespan_ctx = None

    async def __call__(self, scope, receive, send):
        path: str = scope.get("path", "")
        if scope.get("type") == "http":
            if path == "/mcp" or path.startswith("/mcp/"):
                await self._mcp(scope, receive, send)
                return
            if self._sse is not None and (
                path == "/sse"
                or path.startswith("/sse/")
                or path == "/messages"
                or path.startswith("/messages/")
            ):
                await self._sse(scope, receive, send)
                return
        await self._quart(scope, receive, send)


bp = Blueprint("routes", __name__, static_folder="static", template_folder="static")

cosmos_db_ready = asyncio.Event()

def create_app():
    app = Quart(__name__)
    app.register_blueprint(bp)
    app.config["TEMPLATES_AUTO_RELOAD"] = True
    
    @app.before_serving
    async def init():
        try:
            app.cosmos_conversation_client = await init_cosmosdb_client()
            cosmos_db_ready.set()
        except Exception as e:
            logging.exception("Failed to initialize CosmosDB client")
            app.cosmos_conversation_client = None
            raise e
        
        # Initialize MCP servers (non-blocking)
        try:
            await init_mcp_servers()
        except Exception as e:
            logging.warning(f"MCP server initialization failed (optional): {e}")
        
        # Initialize RAG service (non-blocking)
        try:
            await init_rag_service(app_settings)
        except Exception as e:
            logging.warning(f"RAG service initialization failed (optional): {e}")

        # Initialize Remote MCP Server (non-blocking)
        try:
            await init_remote_mcp_server()
        except Exception as e:
            logging.warning(f"Remote MCP server initialization failed (optional): {e}")
            return

        # Bootstrap the FastMCP Starlette lifespan so that the
        # StreamableHTTPSessionManager's anyio task group is started.
        # This must happen AFTER the _MCPASGIDispatch middleware is installed.
        try:
            dispatch = getattr(app, "_mcp_dispatch", None)
            if dispatch is not None:
                await dispatch.bootstrap_lifespan()
        except Exception as e:
            logging.exception("FastMCP lifespan bootstrap failed")

        # Log all endpoints together so they are easy to find in the console
        web_url = "http://127.0.0.1:8081"
        print(f"\n{'=' * 60}")
        print(f"  Web UI endpoint:       {web_url}")
        if remote_mcp_server is not None:
            cfg = app_settings.remote_mcp_server
            mcp_path = cfg.server_path or "/mcp"
            mcp_url = cfg.server_url or f"{web_url}{mcp_path}"
            print(f"  Remote MCP endpoint:   {mcp_url}")
        print(f"{'=' * 60}\n")

    @app.after_serving
    async def shutdown():
        dispatch = getattr(app, "_mcp_dispatch", None)
        if dispatch is not None:
            try:
                await dispatch.shutdown_lifespan()
            except Exception as e:
                logging.warning(f"FastMCP lifespan shutdown failed: {e}")
    
    return app

@bp.route("/")
async def index():
    return await render_template(
        "index.html",
        title=app_settings.ui.title,
        favicon=app_settings.ui.favicon
    )

@bp.route("/favicon.ico")
async def favicon():
    return await bp.send_static_file("favicon.ico")

@bp.route("/assets/<path:path>")
async def assets(path):
    return await send_from_directory("static/assets", path)

# Debug settings
DEBUG = os.environ.get("DEBUG", "false")
if DEBUG.lower() == "true":
    logging.basicConfig(level=logging.DEBUG)

USER_AGENT = "GitHubSampleWebApp/AsyncAzureOpenAI/1.0.0"

# Frontend Settings via Environment Variables
frontend_settings = {
    "auth_enabled": app_settings.base_settings.auth_enabled,
    "feedback_enabled": (
        app_settings.chat_history and
        app_settings.chat_history.enable_feedback
    ),
    "ui": {
        "title": app_settings.ui.title,
        "logo": app_settings.ui.logo,
        "chat_logo": app_settings.ui.chat_logo or app_settings.ui.logo,
        "chat_title": app_settings.ui.chat_title,
        "chat_description": app_settings.ui.chat_description,
        "show_share_button": app_settings.ui.show_share_button,
        "show_chat_history_button": app_settings.ui.show_chat_history_button,
        "footer_html_left": app_settings.ui.footer_html_left,
        "footer_html_middle": app_settings.ui.footer_html_middle,
        "footer_html_right": app_settings.ui.footer_html_right,
    },
    "sanitize_answer": app_settings.base_settings.sanitize_answer,
    "oyd_enabled": app_settings.base_settings.datasource_type,
}

# MCP Server Manager
mcp_manager = MCPServerManager()
mcp_tools_initialized = False

# Remote MCP Server (Streamable HTTP + Entra ID auth)
remote_mcp_server: RemoteMCPServer = None

# Azure OpenAI Client cache
azure_openai_client_cache = None

# Initialize Azure OpenAI Client
async def init_openai_client():
    global azure_openai_client_cache
    
    # Return cached client if available
    if azure_openai_client_cache is not None:
        return azure_openai_client_cache
    
    azure_openai_client = None
    
    try:
        # API version check
        if (
            app_settings.azure_openai.preview_api_version
            < MINIMUM_SUPPORTED_AZURE_OPENAI_PREVIEW_API_VERSION
        ):
            raise ValueError(
                f"The minimum supported Azure OpenAI preview API version is '{MINIMUM_SUPPORTED_AZURE_OPENAI_PREVIEW_API_VERSION}', but the configured version is '{app_settings.azure_openai.preview_api_version}'. Please update your configuration."
            )

        # Endpoint
        if (
            not app_settings.azure_openai.endpoint and
            not app_settings.azure_openai.resource
        ):
            raise ValueError(
                "AZURE_OPENAI_ENDPOINT or AZURE_OPENAI_RESOURCE is required"
            )

        endpoint = (
            app_settings.azure_openai.endpoint
            if app_settings.azure_openai.endpoint
            else f"https://{app_settings.azure_openai.resource}.openai.azure.com/"
        )

        # Authentication
        aoai_api_key = app_settings.azure_openai.key
        ad_token_provider = None
        if not aoai_api_key:
            logging.debug("No AZURE_OPENAI_KEY found, using Azure Entra ID auth")
            async with DefaultAzureCredential() as credential:
                ad_token_provider = get_bearer_token_provider(
                    credential,
                    "https://cognitiveservices.azure.com/.default"
                )

        # Deployment
        deployment = app_settings.azure_openai.model
        if not deployment:
            raise ValueError("AZURE_OPENAI_MODEL is required")

        # Default Headers
        default_headers = {"x-ms-useragent": USER_AGENT}
        
        azure_openai_client = AsyncAzureOpenAI(
            api_version=app_settings.azure_openai.preview_api_version,
            api_key=aoai_api_key,
            azure_ad_token_provider=ad_token_provider,
            default_headers=default_headers,
            azure_endpoint=endpoint,
        )

        # Cache the client
        azure_openai_client_cache = azure_openai_client
        return azure_openai_client
    except Exception as e:
        logging.exception("Exception in Azure OpenAI initialization", e)
        azure_openai_client = None
        raise e

async def init_mcp_servers():
    """Initialize all configured MCP servers"""
    global mcp_manager, mcp_tools_initialized
    
    try:
        # Initialize all MCP servers
        initialized_count = await mcp_manager.initialize_all_servers()
        
        if initialized_count > 0:
            logging.info(f"Successfully initialized {initialized_count} MCP servers")
            
            # Get MCP tools from the manager
            mcp_tools = mcp_manager.get_tools()
            logging.info(f"Available {len(mcp_tools)} MCP tools")
        else:
            logging.info("No MCP servers were initialized")
        
        # Mark MCP tools as initialized
        mcp_tools_initialized = True
        
    except Exception as e:
        logging.exception(f"Failed to initialize MCP servers: {e}")
        raise e

async def init_remote_mcp_server():
    """Initialize the remote MCP server if enabled in configuration."""
    global remote_mcp_server

    cfg = app_settings.remote_mcp_server
    if not cfg or not cfg.server_enabled:
        logging.info("Remote MCP server is disabled (REMOTE_MCP_SERVER_ENABLED not set)")
        return

    rag_svc = await get_rag_service()
    rag_retriever = rag_svc.retriever if rag_svc else None

    citation_resolver = None
    if app_settings.citation_file:
        citation_resolver = CitationLinkResolver(app_settings.citation_file)

    remote_mcp_server = RemoteMCPServer(
        app_settings=app_settings,
        mcp_manager=mcp_manager,
        rag_retriever=rag_retriever,
        citation_resolver=citation_resolver,
    )
    await remote_mcp_server.initialize()

    # Mount the FastMCP Starlette ASGI app at /mcp via ASGI middleware dispatch.
    # This replaces the Quart route handlers for POST/GET/DELETE /mcp so that
    # FastMCP's streaming SSE transport can own the full ASGI lifecycle for
    # those paths (Quart route handlers cannot stream SSE reliably).
    from quart import current_app as _cur_app
    _app_obj = _cur_app._get_current_object()
    mcp_asgi = remote_mcp_server.get_asgi_app(base_path="/mcp")
    sse_asgi = remote_mcp_server.get_sse_asgi_app()  # backward-compat /sse + /messages
    dispatch = _MCPASGIDispatch(_app_obj.asgi_app, mcp_asgi, sse_asgi=sse_asgi)
    _app_obj.asgi_app = dispatch
    # Store reference so before_serving can call bootstrap_lifespan() on it
    _app_obj._mcp_dispatch = dispatch

    logging.info(
        "Remote MCP server initialized on path %s",
        cfg.server_path or "/mcp",
    )


async def init_cosmosdb_client():
    cosmos_conversation_client = None
    if app_settings.chat_history:
        try:
            cosmos_endpoint = (
                f"https://{app_settings.chat_history.account}.documents.azure.com:443/"
            )

            if not app_settings.chat_history.account_key:
                async with DefaultAzureCredential() as cred:
                    credential = cred
                    
            else:
                credential = app_settings.chat_history.account_key

            cosmos_conversation_client = CosmosConversationClient(
                cosmosdb_endpoint=cosmos_endpoint,
                credential=credential,
                database_name=app_settings.chat_history.database,
                container_name=app_settings.chat_history.conversations_container,
                enable_message_feedback=app_settings.chat_history.enable_feedback,
            )
        except Exception as e:
            logging.exception("Exception in CosmosDB initialization", e)
            cosmos_conversation_client = None
            raise e
    else:
        logging.debug("CosmosDB not configured")

    return cosmos_conversation_client

async def call_mcp_tool(tool_name: str, tool_args: dict) -> str:
    """Call a MCP tool via the MCP manager
    
    This function handles both local and remote MCP servers transparently
    through the unified MCPServerManager.
    """
    global mcp_manager
    
    try:
        return await mcp_manager.call_tool(tool_name, tool_args)
    except Exception as e:
        logging.error(f"Error calling MCP tool {tool_name}: {e}")
        return f"Error: {str(e)}"

def is_reasoning_model(model_name: str) -> bool:
    """Return True if the model is a reasoning AI model (o1, o3, o4, gpt-5, etc)."""
    if not model_name:
        return False
    model_name = model_name.lower()
    reasoning_prefixes = [
        "o1", "o3", "o4", "gpt-5"
    ]
    for prefix in reasoning_prefixes:
        if model_name.startswith(prefix):
            return True
    return False

def validate_tool_calls(tool_calls):
    """Validate tool_calls structure to prevent malformed data from being sent to OpenAI API"""
    if not tool_calls or not isinstance(tool_calls, list):
        return False
    
    for tool_call in tool_calls:
        if not isinstance(tool_call, dict):
            return False
        
        # Check required fields
        if "function" not in tool_call:
            return False
        
        function = tool_call["function"]
        if not isinstance(function, dict):
            return False
        
        # Function name is required and must not be null/empty
        if "name" not in function or function["name"] is None or function["name"] == "":
            return False
        
        # Arguments should be a string (JSON), default to empty object if missing
        if "arguments" not in function:
            function["arguments"] = "{}"
        elif function["arguments"] is None:
            function["arguments"] = "{}"
    
    return True

def supports_new_tools_api(model: str) -> bool:
    """
    Detect if the model supports the new tools API (tool_calls with role:"tool").
    
    Supported models include:
    - GPT-5+ series: gpt-5, gpt-5-mini, gpt-5-nano, gpt-6+, etc.
    - GPT-4.1+ series: gpt-4.1, gpt-4.1-mini, gpt-4.1-nano, etc.
    - GPT-4o series: gpt-4o, gpt-4o-mini, etc.
    - O-series: o1, o1-mini, o3, o3-pro, o3-mini, o4+, etc.
    - GPT-OSS series: gpt-oss-120b, gpt-oss-20b, etc.
    
    Returns False for legacy models like gpt-4, gpt-4-turbo, gpt-3.5-turbo, etc.
    """
    if not model:
        return False
    
    model = model.lower().strip()
    
    # O-series models (o1, o3, o4+, etc.)
    if model.startswith("o"):
        try:
            # Extract version number after "o"
            version_part = model[1:]
            major_version_str = ""
            for char in version_part:
                if char.isdigit():
                    major_version_str += char
                else:
                    break
            
            if major_version_str:
                major_version = int(major_version_str)
                # o1, o3, o4+ support new tools API
                return major_version >= 1
        except (ValueError, IndexError):
            pass
        return False
    
    # GPT series models
    if model.startswith("gpt-"):
        version_part = model[4:]  # Remove "gpt-" prefix
        
        # Handle GPT-OSS series
        if version_part.startswith("oss-"):
            return True  # All gpt-oss variants support new tools API
        
        # Handle GPT-4o series
        if version_part.startswith("4o"):
            return True  # All gpt-4o variants support new tools API
        
        # Handle numbered GPT versions (4, 4.1, 5, 6, etc.)
        try:
            # Extract version number
            version_str = ""
            for char in version_part:
                if char.isdigit() or char == ".":
                    version_str += char
                else:
                    break
            
            if not version_str:
                return False
            
            # Parse version (handle both "4" and "4.1" formats)
            if "." in version_str:
                # Handle versions like "4.1"
                major_str, minor_str = version_str.split(".", 1)
                major_version = int(major_str)
                minor_version = float(minor_str) if minor_str else 0
                
                # GPT-4.1+ supports new tools API, but GPT-4.0 and plain GPT-4 don't
                if major_version == 4:
                    return minor_version >= 1
                elif major_version >= 5:
                    return True
            else:
                # Handle versions like "4", "5", "6"
                major_version = int(version_str)
                if major_version >= 5:
                    return True
                elif major_version == 4:
                    # Plain "gpt-4" (without minor version) uses legacy API
                    # But "gpt-4.1+" uses new API (handled above)
                    return False
            
        except (ValueError, IndexError):
            pass
    
    # Default to legacy API for unknown/unparseable models
    return False

def prepare_model_args(request_body, request_headers):
    """Prepare model arguments for OpenAI API call"""
    request_messages = request_body.get("messages", [])
    messages = []
    # Always include system message if configured
    if app_settings.azure_openai.system_message:
        messages = [
            {
                "role": "system",
                "content": app_settings.azure_openai.system_message
            }
        ]

    for message in request_messages:
        if message:
            match message["role"]:
                case "user":
                    messages.append(
                        {
                            "role": message["role"],
                            "content": message["content"]
                        }
                    )
                case "assistant" | "function" | "tool":
                    messages_helper = {}
                    messages_helper["role"] = message["role"]
                    if "name" in message:
                        messages_helper["name"] = message["name"]
                    if "function_call" in message:
                        messages_helper["function_call"] = message["function_call"]
                    if "tool_calls" in message:
                        # Validate tool_calls before sending to API
                        if validate_tool_calls(message["tool_calls"]):
                            messages_helper["tool_calls"] = message["tool_calls"]
                        else:
                            logging.warning(f"Skipping malformed tool_calls in message: {message.get('id', 'unknown')}")
                            logging.warning(f"Malformed tool_calls: {json.dumps(message['tool_calls'], indent=2)}")
                            # Don't include malformed tool_calls
                    if "tool_call_id" in message:
                        messages_helper["tool_call_id"] = message["tool_call_id"]
                    if message.get("content") is not None:
                        messages_helper["content"] = message["content"]
                    else:
                        messages_helper["content"] = None
                    if "context" in message:
                        context_obj = json.loads(message["context"])
                        messages_helper["context"] = context_obj
                    
                    messages.append(messages_helper)

    user_json = None
    if (app_settings.base_settings.ms_defender_enabled):
        authenticated_user_details = get_authenticated_user_details(request_headers)
        conversation_id = request_body.get("conversation_id", None)
        application_name = app_settings.ui.title
        user_json = get_msdefender_user_json(authenticated_user_details, request_headers, conversation_id, application_name)

    model_args = {
        "messages": messages,
        "temperature": app_settings.azure_openai.temperature,
        "top_p": app_settings.azure_openai.top_p,
        "stop": app_settings.azure_openai.stop_sequence,
        "stream": app_settings.azure_openai.stream,
        "model": app_settings.azure_openai.model,
        "user": user_json
    }
    
    # Check if this is a reasoning model
    use_reasoning_model = is_reasoning_model(app_settings.azure_openai.model)
    
    if use_reasoning_model:
        model_args["max_completion_tokens"] = app_settings.azure_openai.max_tokens
        # GPT-5 and newer reasoning models should support most parameters
        logging.info(f"Using reasoning model: {app_settings.azure_openai.model}")
    else:
        model_args["max_tokens"] = app_settings.azure_openai.max_tokens

    if len(messages) > 0:
        if messages[-1]["role"] == "user":
            tools = mcp_manager.get_tools()
            if len(tools) > 0:
                model_args["tools"] = tools
                
                # Log the API path being used for debugging
                use_new_api = supports_new_tools_api(app_settings.azure_openai.model)
                logging.debug(f"Model {app_settings.azure_openai.model} will use {'NEW tools API (role:tool)' if use_new_api else 'LEGACY function API (role:function)'}")

            # For reasoning models, use manual RAG instead of OYD
            if use_reasoning_model and app_settings.datasource:
                # Manual RAG will be handled in send_chat_request_with_rag
                # Don't add extra_body for reasoning models
                pass
            elif app_settings.datasource:
                # Use OYD for non-reasoning models
                model_args["extra_body"] = {
                    "data_sources": [
                        app_settings.datasource.construct_payload_configuration(
                            request=request
                        )
                    ]
                }

    model_args_clean = copy.deepcopy(model_args)
    if model_args_clean.get("extra_body"):
        secret_params = [
            "key",
            "connection_string",
            "embedding_key",
            "encoded_api_key",
            "api_key",
        ]
        for secret_param in secret_params:
            if model_args_clean["extra_body"]["data_sources"][0]["parameters"].get(
                secret_param
            ):
                model_args_clean["extra_body"]["data_sources"][0]["parameters"][
                    secret_param
                ] = "*****"
        authentication = model_args_clean["extra_body"]["data_sources"][0][
            "parameters"
        ].get("authentication", {})
        for field in authentication:
            if field in secret_params:
                model_args_clean["extra_body"]["data_sources"][0]["parameters"][
                    "authentication"
                ][field] = "*****"
        embeddingDependency = model_args_clean["extra_body"]["data_sources"][0][
            "parameters"
        ].get("embedding_dependency", {})
        if "authentication" in embeddingDependency:
            for field in embeddingDependency["authentication"]:
                if field in secret_params:
                    model_args_clean["extra_body"]["data_sources"][0]["parameters"][
                        "embedding_dependency"
                    ]["authentication"][field] = "*****"

    logging.debug(f"REQUEST BODY: {json.dumps(model_args_clean, indent=4)}")

    return model_args

async def promptflow_request(request):
    try:
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {app_settings.promptflow.api_key}",
        }
        # Adding timeout for scenarios where response takes longer to come back
        logging.debug(f"Setting timeout to {app_settings.promptflow.response_timeout}")
        async with httpx.AsyncClient(
            timeout=float(app_settings.promptflow.response_timeout)
        ) as client:
            pf_formatted_obj = convert_to_pf_format(
                request,
                app_settings.promptflow.request_field_name,
                app_settings.promptflow.response_field_name
            )
            # NOTE: This only support question and chat_history parameters
            # If you need to add more parameters, you need to modify the request body
            response = await client.post(
                app_settings.promptflow.endpoint,
                json={
                    app_settings.promptflow.request_field_name: pf_formatted_obj[-1]["inputs"][app_settings.promptflow.request_field_name],
                    "chat_history": pf_formatted_obj[:-1],
                },
                headers=headers,
            )
        resp = response.json()
        resp["id"] = request["messages"][-1]["id"]
        return resp
    except Exception as e:
        logging.error(f"An error occurred while making promptflow_request: {e}")

async def process_function_call(response, model_name=None):
    """Process function calls from OpenAI response using the appropriate API path"""
    response_message = response.choices[0].message
    messages = []

    # Handle case where no tool calls are requested
    if not response_message.tool_calls:
        logging.debug("No tool calls requested, returning None")
        return None

    # Determine which API path to use based on model version
    use_new_tools_api = supports_new_tools_api(model_name or app_settings.azure_openai.model)
    
    logging.info(f"Using {'NEW tools API' if use_new_tools_api else 'LEGACY function API'} for model: {model_name or app_settings.azure_openai.model}")
    logging.info(f"Processing {len(response_message.tool_calls)} tool call(s) in parallel")

    # Filter available tool calls
    available_tool_calls = []
    for tool_call in response_message.tool_calls:
        # Validate tool call structure
        if (hasattr(tool_call, 'function') and 
            hasattr(tool_call.function, 'name') and 
            tool_call.function.name is not None):
            
            if not mcp_manager.is_tool_available(tool_call.function.name):
                logging.warning(f"Tool '{tool_call.function.name}' not available, skipping")
                continue
            available_tool_calls.append(tool_call)
        else:
            logging.warning(f"Malformed tool call from OpenAI API, skipping: {tool_call}")
            continue
    
    if not available_tool_calls:
        logging.warning("No available tool calls to process")
        return None
    
    # Execute all available tool calls in parallel
    async def execute_tool_call(tool_call):
        try:
            function_response = await call_mcp_tool(
                tool_call.function.name, 
                json.loads(tool_call.function.arguments)
            )
            return tool_call, function_response, None
        except Exception as e:
            logging.error(f"Error processing tool call '{tool_call.function.name}': {e}")
            return tool_call, None, e
    
    # Run all tool calls in parallel
    tool_call_tasks = [execute_tool_call(tool_call) for tool_call in available_tool_calls]
    tool_call_results = await asyncio.gather(*tool_call_tasks, return_exceptions=True)
    
    # Process results and build response messages
    if use_new_tools_api:
        # NEW path (>= gpt-5): Use role:"tool" with tool_call_id
        
        # Add single assistant message with ALL tool_calls
        assistant_tool_calls = []
        for tool_call, function_response, error in tool_call_results:
            if not isinstance(tool_call, Exception):  # Handle any gather exceptions
                assistant_tool_calls.append({
                    "id": tool_call.id,
                    "type": "function",
                    "function": {
                        "name": tool_call.function.name,
                        "arguments": tool_call.function.arguments,
                    }
                })
        
        if assistant_tool_calls:
            messages.append({
                "role": "assistant",
                "content": response_message.content,
                "tool_calls": assistant_tool_calls
            })
            
            # Add individual tool messages for each successful call
            for tool_call, function_response, error in tool_call_results:
                if not isinstance(tool_call, Exception) and function_response is not None:
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tool_call.id,
                        "content": function_response,
                    })
                elif not isinstance(tool_call, Exception) and error is not None:
                    # Add error response for failed tool calls
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tool_call.id,
                        "content": f"Error: {str(error)}",
                    })
    else:
        # LEGACY path (< gpt-5): Use role:"function" - process first successful call only
        for tool_call, function_response, error in tool_call_results:
            if not isinstance(tool_call, Exception) and function_response is not None:
                messages.append({
                    "role": "assistant",
                    "function_call": {
                        "name": tool_call.function.name,
                        "arguments": tool_call.function.arguments,
                    },
                    "content": None,
                })
                
                messages.append({
                    "role": "function",
                    "name": tool_call.function.name,
                    "content": function_response,
                })
                break  # Legacy API only supports single function call
    
    return messages if messages else None

async def generate_title(messages):
    """Generate a title for the conversation based on the messages"""
    try:
        # Simple title generation based on first user message
        if messages and len(messages) > 0:
            for message in messages:
                if message.get("role") == "user" and message.get("content"):
                    content = message["content"]
                    # Ensure content is not just whitespace
                    if content and content.strip():
                        # Truncate title
                        max_len = 28
                        title = content[:max_len] + "..." if len(content) > max_len else content
                        return title
        
        fallback_title = f"Chat {datetime.datetime.now().strftime('%Y-%m-%d %H:%M')}"
        logging.debug(f"No valid user message found, using fallback title: '{fallback_title}'")
        return fallback_title
    except Exception as e:
        fallback_title = f"Chat {datetime.datetime.now().strftime('%Y-%m-%d %H:%M')}"
        logging.error(f"Error generating title: {e}, using fallback: '{fallback_title}'")
        return fallback_title

async def send_chat_request(request_body, request_headers):
    # Filter messages based on model capabilities and context
    model_name = app_settings.azure_openai.model
    use_new_tools_api = supports_new_tools_api(model_name)
    
    filtered_messages = []
    messages = request_body.get("messages", [])
    
    # For NEW tools API: Keep all messages but validate tool message sequence
    # For LEGACY function API: Filter out tool role messages and replace with function role
    
    if use_new_tools_api:
        # NEW tools API: Keep all messages but validate sequence and structure
        for i, message in enumerate(messages):
            if message.get("role") == "tool":
                # For tool messages, ensure they have tool_call_id and follow an assistant message with tool_calls
                if "tool_call_id" not in message:
                    logging.warning(f"Tool message at index {i} missing tool_call_id, skipping")
                    continue
                    
                # Check if the previous assistant message has tool_calls
                prev_assistant_msg = None
                for j in range(i-1, -1, -1):
                    if messages[j].get("role") == "assistant":
                        prev_assistant_msg = messages[j]
                        break
                
                if prev_assistant_msg and "tool_calls" not in prev_assistant_msg:
                    logging.warning(f"Tool message at index {i} doesn't follow an assistant message with tool_calls, skipping")
                    continue
            
            elif message.get("role") == "assistant" and "tool_calls" in message:
                # Validate tool_calls structure to prevent malformed data
                tool_calls = message.get("tool_calls")
                if tool_calls and isinstance(tool_calls, list):
                    valid_tool_calls = []
                    for tool_call in tool_calls:
                        if (isinstance(tool_call, dict) and 
                            "function" in tool_call and 
                            isinstance(tool_call["function"], dict) and
                            tool_call["function"].get("name") is not None):
                            valid_tool_calls.append(tool_call)
                        else:
                            logging.warning(f"Removing malformed tool call from message {i}: {tool_call}")
                    
                    # Update message with only valid tool calls
                    if valid_tool_calls:
                        message = message.copy()  # Don't modify original
                        message["tool_calls"] = valid_tool_calls
                    else:
                        # Remove tool_calls if none are valid
                        message = message.copy()
                        message.pop("tool_calls", None)
                        logging.warning(f"Removed all malformed tool_calls from assistant message {i}")
                    
            filtered_messages.append(message)
    else:
        # LEGACY function API: Convert tool messages to function messages, filter tool_calls
        for message in messages:
            if message.get("role") == "tool":
                # Skip tool role messages for legacy models
                continue
            elif message.get("role") == "assistant" and "tool_calls" in message:
                # Convert assistant message with tool_calls to function_call format
                filtered_msg = {
                    "role": "assistant",
                    "content": message.get("content")
                }
                # Add function_call if tool_calls exist and are valid
                tool_calls = message.get("tool_calls")
                if tool_calls and len(tool_calls) > 0:
                    first_tool_call = tool_calls[0]
                    # Validate tool call structure to prevent malformed data
                    if (isinstance(first_tool_call, dict) and 
                        "function" in first_tool_call and 
                        isinstance(first_tool_call["function"], dict) and
                        first_tool_call["function"].get("name") is not None):
                        
                        filtered_msg["function_call"] = {
                            "name": first_tool_call["function"]["name"],
                            "arguments": first_tool_call["function"].get("arguments", "{}")
                        }
                        filtered_msg["content"] = None
                    else:
                        logging.warning(f"Skipping malformed tool call in legacy mode: {first_tool_call}")
                        # Keep the message but without tool calling
                        
                filtered_messages.append(filtered_msg)
            else:
                # Keep other messages as-is
                filtered_messages.append(message)
            
    request_body['messages'] = filtered_messages
    
    try:
        # Initialize OpenAI client and MCP tools BEFORE preparing model args
        azure_openai_client = await init_openai_client()
        
        # Check if we need to use manual RAG for reasoning models
        use_reasoning_model = is_reasoning_model(app_settings.azure_openai.model)
        logging.info(f"Using model: {app_settings.azure_openai.model}, is_reasoning_model: {use_reasoning_model}")
        logging.info(f"Datasource available: {bool(app_settings.datasource)}")
        
        if use_reasoning_model and app_settings.datasource:
            logging.info("Using manual RAG for reasoning model")
            return await send_chat_request_with_rag(request_body, request_headers, azure_openai_client)
        elif use_reasoning_model:
            logging.info("Reasoning model detected but no datasource - using regular processing")
        else:
            logging.info("Not a reasoning model - using regular processing")
        
        # Now prepare model args with tools available
        model_args = prepare_model_args(request_body, request_headers)
        
        raw_response = await azure_openai_client.chat.completions.with_raw_response.create(**model_args)
        response = raw_response.parse()
        apim_request_id = raw_response.headers.get("apim-request-id") 
    except Exception as e:
        logging.exception("Exception in send_chat_request")
        raise e

    return response, apim_request_id


async def send_chat_request_with_rag(request_body, request_headers, azure_openai_client):
    """Handle chat requests for reasoning models with manual RAG."""
    try:
        # Get the last user message for RAG query
        messages = request_body.get("messages", [])
        user_query = None
        for message in reversed(messages):
            if message.get("role") == "user":
                user_query = message.get("content", "")
                break
        
        if not user_query:
            logging.warning("No user query found for RAG - using fallback")
            # Fallback to normal processing without RAG
            model_args = prepare_model_args(request_body, request_headers)
            raw_response = await azure_openai_client.chat.completions.with_raw_response.create(**model_args)
            apim_request_id = raw_response.headers.get("apim-request-id")
            response = raw_response.parse()  # This works for both streaming and non-streaming
            return response, apim_request_id
        
        # Retrieve relevant context using RAG service
        rag_service = await get_rag_service()
        if not rag_service:
            logging.error("RAG service not available, falling back to normal processing")
            model_args = prepare_model_args(request_body, request_headers)
            raw_response = await azure_openai_client.chat.completions.with_raw_response.create(**model_args)
            apim_request_id = raw_response.headers.get("apim-request-id")
            response = raw_response.parse()  # This works for both streaming and non-streaming
            apim_request_id = raw_response.headers.get("apim-request-id")
            return response, apim_request_id
        
        context, citations = await rag_service.retrieve_context(user_query)
        
        # Prepare model args without OYD
        model_args = prepare_model_args(request_body, request_headers)
        
        # For reasoning models with RAG, ensure adequate token limits
        # Only increase if the current limit is too small for RAG context
        if is_reasoning_model(app_settings.azure_openai.model):
            current_max_tokens = model_args.get('max_completion_tokens', model_args.get('max_tokens', 0))
            min_required_tokens = 8192  # Minimum needed for RAG + reasoning
            
            if current_max_tokens < min_required_tokens:
                increased_tokens = max(min_required_tokens, 8192)
                logging.debug(f"Increasing max_completion_tokens for reasoning model from {current_max_tokens} to {increased_tokens}")
                
                if "max_completion_tokens" in model_args:
                    model_args["max_completion_tokens"] = increased_tokens
                else:
                    model_args["max_tokens"] = increased_tokens
        
        if context and citations:
            # Inject context into the conversation
            # Modify the last user message to include context
            modified_messages = []
            original_query = None
            
            for message in model_args["messages"]:
                if message.get("role") == "user":
                    # Store original query for potential context reduction
                    original_query = message["content"]
                    # This should be the last user message due to the loop structure
                    enhanced_content = rag_service.format_context_for_prompt(context, message["content"])
                    modified_messages.append({
                        "role": "user",
                        "content": enhanced_content
                    })
                else:
                    modified_messages.append(message)
            
            model_args["messages"] = modified_messages
            
            # Estimate total prompt length for reasoning model compatibility
            total_content_length = sum(len(str(msg.get('content', ''))) for msg in model_args['messages'])
            
            # Check if the prompt might be too long for reasoning models
            if total_content_length > 50000:  # Rough estimate for potential issues
                logging.warning(f"Large prompt detected ({total_content_length} chars). This might cause issues with reasoning models. Ensure that the max tokens are configured correspondingly high.")
        
        # Make the API call
        
        raw_response = await azure_openai_client.chat.completions.with_raw_response.create(**model_args)
        apim_request_id = raw_response.headers.get("apim-request-id")
        
        # For streaming responses, don't parse the raw response - it would destroy the stream
        if app_settings.azure_openai.stream:
            logging.debug("Azure OpenAI API call initiated for streaming response")
            response = raw_response.parse()  # This returns the stream object
        else:
            response = raw_response.parse()  # For non-streaming, this is safe
            logging.debug(f"Azure OpenAI API call completed. Response has {len(response.choices) if response.choices else 0} choices")
        
        # Add citations to the response for streaming support
        if citations:
            if app_settings.azure_openai.stream:
                # For streaming responses, attach citations to the response object
                response._citations = citations
            else:
                # For non-streaming responses, check if we can access the message content
                if hasattr(response, 'choices') and len(response.choices) > 0 and hasattr(response.choices[0].message, 'content'):
                    # Create a context object similar to OYD format
                    context_obj = {
                        "citations": citations,
                        "intent": user_query
                    }
                    
                    # Inject context into the response
                    # We'll handle this in the format_non_streaming_response function
                    response._citations = citations
        
        return response, apim_request_id
        
    except Exception as e:
        logging.exception("Exception in send_chat_request_with_rag")
        # Fallback to normal processing
        model_args = prepare_model_args(request_body, request_headers)
        raw_response = await azure_openai_client.chat.completions.with_raw_response.create(**model_args)
        response = raw_response.parse()
        apim_request_id = raw_response.headers.get("apim-request-id")
        return response, apim_request_id

async def complete_chat_request(request_body, request_headers):
    if app_settings.base_settings.use_promptflow:
        response = await promptflow_request(request_body)
        history_metadata = request_body.get("history_metadata", {})
        return format_pf_non_streaming_response(
            response,
            history_metadata,
            app_settings.promptflow.response_field_name,
            app_settings.promptflow.citations_field_name
        )
    else:
        response, apim_request_id = await send_chat_request(request_body, request_headers)
        history_metadata = request_body.get("history_metadata", {})
        
        # Check if tools are available and if the model made any tool calls
        tools = mcp_manager.get_tools()
        original_citations = getattr(response, '_citations', None)  # Preserve citations from original response
        
        if len(tools) > 0:
            function_response = await process_function_call(response, app_settings.azure_openai.model)

            if function_response:
                # Tool calls were made, extend conversation and get final response
                request_body["messages"].extend(function_response)
                response, apim_request_id = await send_chat_request(request_body, request_headers)
                history_metadata = request_body.get("history_metadata", {})
                
                # Preserve citations from the original response if they exist
                if original_citations and not hasattr(response, '_citations'):
                    response._citations = original_citations
            # If no function_response, the model chose not to use tools, proceed with original response
        
        # Format the final response
        non_streaming_response = format_non_streaming_response(response, history_metadata, apim_request_id)

    return non_streaming_response

class AzureOpenaiFunctionCallStreamState():
    def __init__(self, model_name=None):
        self.tool_calls = []                # All tool calls detected in the stream
        self.tool_name = ""                 # Tool name being streamed
        self.tool_arguments_stream = ""     # Tool arguments being streamed
        self.current_tool_call = None       # JSON with the tool name and arguments currently being streamed
        self.function_messages = []         # All function messages to be appended to the chat history
        self.streaming_state = "INITIAL"    # Streaming state (INITIAL, STREAMING, COMPLETED)
        self.use_new_tools_api = supports_new_tools_api(model_name or app_settings.azure_openai.model)  # API path selection

async def process_function_call_stream(completionChunk, function_call_stream_state, request_body, request_headers, history_metadata, apim_request_id):
    if hasattr(completionChunk, "choices") and len(completionChunk.choices) > 0:
        response_message = completionChunk.choices[0].delta
        
        # Function calling stream processing
        if response_message.tool_calls and function_call_stream_state.streaming_state in ["INITIAL", "STREAMING"]:
            function_call_stream_state.streaming_state = "STREAMING"
            for tool_call_chunk in response_message.tool_calls:
                # Validate tool call chunk structure
                if not hasattr(tool_call_chunk, 'function') or tool_call_chunk.function is None:
                    logging.warning(f"Skipping malformed tool call chunk - missing function: {tool_call_chunk}")
                    continue
                
                # Log tool call chunk for debugging
                logging.debug(f"Processing tool call chunk: id={getattr(tool_call_chunk, 'id', None)}, "
                            f"name={getattr(tool_call_chunk.function, 'name', None)}, "
                            f"args='{getattr(tool_call_chunk.function, 'arguments', '')[:50]}{'...' if len(getattr(tool_call_chunk.function, 'arguments', '')) > 50 else ''}'")
                
                # New tool call
                if tool_call_chunk.id:
                    # Complete previous tool call if exists
                    if function_call_stream_state.current_tool_call:
                        chunk_args = getattr(tool_call_chunk.function, 'arguments', '') or ''
                        function_call_stream_state.tool_arguments_stream += chunk_args
                        function_call_stream_state.current_tool_call["tool_arguments"] = function_call_stream_state.tool_arguments_stream
                        function_call_stream_state.tool_calls.append(function_call_stream_state.current_tool_call)
                        # Reset for new tool call
                        function_call_stream_state.tool_arguments_stream = ""
                        function_call_stream_state.tool_name = ""

                    # Start new tool call - function name might be in this chunk or subsequent ones
                    function_name = getattr(tool_call_chunk.function, 'name', None)
                    
                    function_call_stream_state.current_tool_call = {
                        "tool_id": tool_call_chunk.id,
                        "tool_name": function_name  # This might be None initially
                    }
                    
                    # If this chunk has function name, store it
                    if function_name:
                        function_call_stream_state.tool_name = function_name
                    
                    # Add any arguments from this chunk
                    chunk_args = getattr(tool_call_chunk.function, 'arguments', '') or ''
                    function_call_stream_state.tool_arguments_stream += chunk_args
                else:
                    # Continuation of existing tool call
                    if function_call_stream_state.current_tool_call:
                        # Check if this chunk provides the function name that was missing
                        function_name = getattr(tool_call_chunk.function, 'name', None)
                        if function_name and not function_call_stream_state.current_tool_call.get("tool_name"):
                            function_call_stream_state.current_tool_call["tool_name"] = function_name
                            function_call_stream_state.tool_name = function_name
                        
                        # Accumulate arguments
                        chunk_args = getattr(tool_call_chunk.function, 'arguments', '') or ''
                        function_call_stream_state.tool_arguments_stream += chunk_args
                
        # Function call - Streaming completed
        elif response_message.tool_calls is None and function_call_stream_state.streaming_state == "STREAMING":
            # Complete the final tool call if it exists
            if function_call_stream_state.current_tool_call:
                function_call_stream_state.current_tool_call["tool_arguments"] = function_call_stream_state.tool_arguments_stream
                
                # Only add if we have a valid tool name (it might have been set in a later chunk)
                tool_name = function_call_stream_state.current_tool_call.get("tool_name")
                if tool_name and tool_name != "":
                    function_call_stream_state.tool_calls.append(function_call_stream_state.current_tool_call)
                else:
                    logging.warning(f"Skipping tool call without valid name in stream completion: {function_call_stream_state.current_tool_call}")
            
            # Process all tool calls in parallel instead of just the first one
            logging.info(f"Processing {len(function_call_stream_state.tool_calls)} tool call(s) in parallel from stream")
            
            # Filter available tool calls
            available_tool_calls = []
            for tool_call in function_call_stream_state.tool_calls:
                # Validate tool call structure
                tool_name = tool_call.get("tool_name")
                if not tool_name or tool_name == "":
                    logging.warning(f"Tool call in stream has invalid name, skipping: {tool_call}")
                    continue
                    
                if not mcp_manager.is_tool_available(tool_name):
                    logging.warning(f"Tool '{tool_name}' not available in stream, skipping")
                    continue
                available_tool_calls.append(tool_call)
            
            if not available_tool_calls:
                logging.warning("No available tool calls to process in stream")
                function_call_stream_state.streaming_state = "COMPLETED"
                return function_call_stream_state.streaming_state
            
            # Execute all available tool calls in parallel
            async def execute_stream_tool_call(tool_call):
                try:
                    tool_response = await call_mcp_tool(
                        tool_call["tool_name"], 
                        json.loads(tool_call["tool_arguments"])
                    )
                    return tool_call, tool_response, None
                except Exception as e:
                    logging.error(f"Error processing stream tool call '{tool_call['tool_name']}': {e}")
                    return tool_call, None, e
            
            # Run all tool calls in parallel
            tool_call_tasks = [execute_stream_tool_call(tool_call) for tool_call in available_tool_calls]
            tool_call_results = await asyncio.gather(*tool_call_tasks, return_exceptions=True)
            
            # Process results and build response messages
            if function_call_stream_state.use_new_tools_api:
                # NEW path (>= gpt-5): Use role:"tool" with tool_call_id
                
                # Add single assistant message with ALL tool_calls
                assistant_tool_calls = []
                for tool_call, tool_response, error in tool_call_results:
                    if not isinstance(tool_call, Exception):  # Handle any gather exceptions
                        assistant_tool_calls.append({
                            "id": tool_call["tool_id"],
                            "type": "function", 
                            "function": {
                                "name": tool_call["tool_name"],
                                "arguments": tool_call["tool_arguments"]
                            }
                        })
                
                if assistant_tool_calls:
                    function_call_stream_state.function_messages.append({
                        "role": "assistant",
                        "content": None,
                        "tool_calls": assistant_tool_calls
                    })
                    
                    # Add individual tool messages for each call (successful or failed)
                    for tool_call, tool_response, error in tool_call_results:
                        if not isinstance(tool_call, Exception):
                            content = tool_response if tool_response is not None else f"Error: {str(error)}"
                            function_call_stream_state.function_messages.append({
                                "role": "tool",
                                "tool_call_id": tool_call["tool_id"],
                                "content": content,
                            })
            else:
                # LEGACY path (< gpt-5): Use role:"function" - process first successful call only
                for tool_call, tool_response, error in tool_call_results:
                    if not isinstance(tool_call, Exception) and tool_response is not None:
                        function_call_stream_state.function_messages.append({
                            "role": "assistant",
                            "function_call": {
                                "name": tool_call["tool_name"],
                                "arguments": tool_call["tool_arguments"]
                            },
                            "content": None
                        })
                        
                        function_call_stream_state.function_messages.append({
                            "role": "function",
                            "name": tool_call["tool_name"],
                            "content": tool_response,
                        })
                        break  # Legacy API only supports single function call
            
            function_call_stream_state.streaming_state = "COMPLETED"
            return function_call_stream_state.streaming_state
        
        else:
            return function_call_stream_state.streaming_state

async def stream_chat_request(request_body, request_headers):
    response, apim_request_id = await send_chat_request(request_body, request_headers)
    history_metadata = request_body.get("history_metadata", {})
    
    async def generate(apim_request_id, history_metadata):
        tools = mcp_manager.get_tools()
        original_citations = getattr(response, '_citations', None)  # Preserve citations from original response
        
        if len(tools) > 0:
            # Maintain state during function call streaming
            function_call_stream_state = AzureOpenaiFunctionCallStreamState(app_settings.azure_openai.model)
            
            async for completionChunk in response:
                stream_state = await process_function_call_stream(completionChunk, function_call_stream_state, request_body, request_headers, history_metadata, apim_request_id)
                
                # No function call, asistant response
                if stream_state == "INITIAL":
                    yield format_stream_response(completionChunk, history_metadata, apim_request_id)

                # Function call stream completed, functions were executed.
                # Append function calls and results to history and send to OpenAI, to stream the final answer.
                if stream_state == "COMPLETED" or stream_state == None:
                    request_body["messages"].extend(function_call_stream_state.function_messages)
                    function_response, apim_request_id = await send_chat_request(request_body, request_headers)
                    
                    # Handle citation injection for tool call scenarios
                    citations_injected = False
                    async for functionCompletionChunk in function_response:
                        # Inject citations only on the first chunk (once per stream) when tool calls are involved
                        # Use original citations if the new response doesn't have citations
                        citations_to_inject = getattr(function_response, '_citations', original_citations)
                        if not citations_injected and citations_to_inject:
                            context_obj = {
                                "citations": citations_to_inject,
                                "intent": "Retrieved context for reasoning model with tool calls"
                            }
                            
                            # Create citation response in the same format as normal streaming responses
                            citation_response = {
                                "id": functionCompletionChunk.id,
                                "model": functionCompletionChunk.model,
                                "created": functionCompletionChunk.created,
                                "object": functionCompletionChunk.object,
                                "choices": [{"messages": [{"role": "tool", "content": json.dumps(context_obj)}]}],
                                "history_metadata": history_metadata,
                                "apim-request-id": apim_request_id,
                            }
                            yield citation_response
                            citations_injected = True
                        
                        yield format_stream_response(functionCompletionChunk, history_metadata, apim_request_id)
                
        else:
            # Handle manual RAG citations injection for reasoning models
            citations_injected = False
            assistant_content_received = False
            total_content_chunks = 0
            
            try:
                # Check if response is actually a stream
                if not hasattr(response, '__aiter__'):
                    raise TypeError("Expected streaming response but got non-streaming response")
                
                chunk_count = 0
                async for completionChunk in response:
                    chunk_count += 1
                    
                    # Inject citations only on the first chunk (once per stream)
                    if not citations_injected and hasattr(response, '_citations') and response._citations:
                        context_obj = {
                            "citations": response._citations,
                            "intent": "Retrieved context for reasoning model"
                        }
                        
                        # Create citation response in the same format as normal streaming responses
                        citation_response = {
                            "id": completionChunk.id,
                            "model": completionChunk.model,
                            "created": completionChunk.created,
                            "object": completionChunk.object,
                            "choices": [{"messages": [{"role": "tool", "content": json.dumps(context_obj)}]}],
                            "history_metadata": history_metadata,
                            "apim-request-id": apim_request_id,
                        }
                        yield citation_response
                        citations_injected = True
                    
                    # Debug: Log chunk structure
                    if completionChunk.choices and len(completionChunk.choices) > 0:
                        choice = completionChunk.choices[0]
                        if hasattr(choice, 'delta') and choice.delta:
                            delta = choice.delta
                    
                    if completionChunk.choices and len(completionChunk.choices) > 0:
                        delta = completionChunk.choices[0].delta
                        choice = completionChunk.choices[0]
                        
                        if delta:
                            if hasattr(delta, 'content') and delta.content is not None:
                                assistant_content_received = True
                                total_content_chunks += 1
                    
                    # Process and yield each streaming chunk
                    formatted_response = format_stream_response(completionChunk, history_metadata, apim_request_id)
                    # Only yield non-empty responses to avoid sending empty objects
                    if formatted_response and formatted_response.get("choices") and formatted_response["choices"][0].get("messages"):
                        yield formatted_response
                    
                    # Check if stream ended unexpectedly (choice is defined above in the completionChunk.choices block)
                    if completionChunk.choices and len(completionChunk.choices) > 0:
                        choice = completionChunk.choices[0]
                        if hasattr(choice, 'finish_reason') and choice.finish_reason:
                            break  # Explicitly break to ensure we don't continue
                    
            except Exception as e:
                logging.error(f"Exception during streaming: {str(e)}", exc_info=True)
                # Don't re-raise here, let the fallback logic handle it
            
            if not assistant_content_received:
                # For reasoning models, try a fallback without RAG enhancement
                if is_reasoning_model(app_settings.azure_openai.model):
                    logging.info("Attempting fallback without RAG enhancement for reasoning model")
                    try:
                        # Prepare original model args without RAG enhancement - force non-streaming
                        fallback_model_args = prepare_model_args(request_body, request_headers)
                        fallback_model_args["stream"] = False  # Force non-streaming for fallback
                        
                        azure_openai_client = await init_openai_client()
                        fallback_raw_response = await azure_openai_client.chat.completions.with_raw_response.create(**fallback_model_args)
                        fallback_response = fallback_raw_response.parse()
                        
                        logging.info("Fallback response successful, converting to streaming format")
                        
                        # Convert the non-streaming response to streaming format
                        if fallback_response.choices and len(fallback_response.choices) > 0:
                            # First inject citations if available
                            if hasattr(response, '_citations') and response._citations:
                                context_obj = {
                                    "citations": response._citations,
                                    "intent": "Retrieved context for reasoning model (fallback)"
                                }
                                
                                citation_response = {
                                    "id": fallback_response.id,
                                    "model": fallback_response.model,
                                    "created": fallback_response.created,
                                    "object": "chat.completion.chunk",
                                    "choices": [{"messages": [{"role": "tool", "content": json.dumps(context_obj)}]}],
                                    "history_metadata": history_metadata,
                                    "apim-request-id": apim_request_id,
                                }
                                yield citation_response
                            
                            # Then yield the assistant message content
                            assistant_content = fallback_response.choices[0].message.content
                            if assistant_content:
                                assistant_response = {
                                    "id": fallback_response.id,
                                    "model": fallback_response.model,
                                    "created": fallback_response.created,
                                    "object": "chat.completion.chunk",
                                    "choices": [{"messages": [{"role": "assistant", "content": assistant_content}]}],
                                    "history_metadata": history_metadata,
                                    "apim-request-id": apim_request_id,
                                }
                                yield assistant_response
                            
                    except Exception as fallback_error:
                        logging.error(f"Fallback also failed: {fallback_error}")

    return generate(apim_request_id=apim_request_id, history_metadata=history_metadata)

async def conversation_internal(request_body, request_headers):
    try:
        if app_settings.azure_openai.stream and not app_settings.base_settings.use_promptflow:
            result = await stream_chat_request(request_body, request_headers)
            response = await make_response(format_as_ndjson(result))
            response.timeout = None
            response.mimetype = "application/json-lines"
            return response
        else:
            result = await complete_chat_request(request_body, request_headers)
            return jsonify(result)

    except Exception as ex:
        logging.exception(ex)
        if hasattr(ex, "status_code"):
            return jsonify({"error": str(ex)}), ex.status_code
        else:
            return jsonify({"error": str(ex)}), 500

@bp.route("/conversation", methods=["POST"])
async def conversation():
    if not request.is_json:
        return jsonify({"error": "request must be json"}), 415
    request_json = await request.get_json()

    return await conversation_internal(request_json, request.headers)

@bp.route("/frontend_settings", methods=["GET"])
def get_frontend_settings():
    try:
        return jsonify(frontend_settings), 200
    except Exception as e:
        logging.exception("Exception in /frontend_settings")
        return jsonify({"error": str(e)}), 500

## Conversation History API ##
@bp.route("/history/generate", methods=["POST"])
async def add_conversation():
    await cosmos_db_ready.wait()
    authenticated_user = get_authenticated_user_details(request_headers=request.headers)
    user_id = authenticated_user["user_principal_id"]

    ## check request for conversation_id
    request_json = await request.get_json()
    conversation_id = request_json.get("conversation_id", None)

    try:
        # make sure cosmos is configured
        if not current_app.cosmos_conversation_client:
            raise Exception("CosmosDB is not configured or not working")

        # check for the conversation_id, if the conversation is not set, we will create a new one
        history_metadata = {}
        if not conversation_id:
            title = await generate_title(request_json["messages"])
            conversation_dict = await current_app.cosmos_conversation_client.create_conversation(
                user_id=user_id, title=title
            )
            conversation_id = conversation_dict["id"]
            history_metadata["title"] = title
            history_metadata["date"] = conversation_dict["createdAt"]

        ## Format the incoming message object in the "chat/completions" messages format
        ## then write it to the conversation history in cosmos
        messages = request_json["messages"]
        if len(messages) > 0 and messages[-1]["role"] == "user":
            createdMessageValue = await current_app.cosmos_conversation_client.create_message(
                uuid=str(uuid.uuid4()),
                conversation_id=conversation_id,
                user_id=user_id,
                input_message=messages[-1],
            )
            if createdMessageValue == "Conversation not found":
                raise Exception(
                    "Conversation not found for the given conversation ID: "
                    + conversation_id
                    + "."
                )
        else:
            raise Exception("No user message found")

        # Submit request to Chat Completions for response
        request_body = request_json  # Reuse the already parsed request instead of parsing again
        history_metadata["conversation_id"] = conversation_id
        request_body["history_metadata"] = history_metadata
        return await conversation_internal(request_body, request.headers)

    except Exception as e:
        logging.exception("Exception in /history/generate")
        return jsonify({"error": str(e)}), 500

@bp.route("/history/update", methods=["POST"])
async def update_conversation():
    await cosmos_db_ready.wait()
    authenticated_user = get_authenticated_user_details(request_headers=request.headers)
    user_id = authenticated_user["user_principal_id"]

    ## check request for conversation_id
    request_json = await request.get_json()
    conversation_id = request_json.get("conversation_id", None)

    try:
        # make sure cosmos is configured
        if not current_app.cosmos_conversation_client:
            raise Exception("CosmosDB is not configured or not working")

        # check for the conversation_id, if the conversation is not set, we will create a new one
        if not conversation_id:
            raise Exception("No conversation_id found")

        ## Format the incoming message object in the "chat/completions" messages format
        ## then write it to the conversation history in cosmos
        messages = request_json["messages"]
        if len(messages) > 0 and messages[-1]["role"] == "assistant":
            if len(messages) > 1 and messages[-2].get("role", None) == "tool": 
                # write the tool message first
                await current_app.cosmos_conversation_client.create_message(
                    uuid=str(uuid.uuid4()),
                    conversation_id=conversation_id,
                    user_id=user_id, 
                    input_message=messages[-2],
                )
            # write the assistant message
            await current_app.cosmos_conversation_client.create_message(
                uuid=messages[-1]["id"],
                conversation_id=conversation_id,
                user_id=user_id,
                input_message=messages[-1],
            )
        else:
            raise Exception("No bot messages found")

        # Submit request to Chat Completions for response
        response = {"success": True}
        return jsonify(response), 200

    except Exception as e:
        logging.exception("Exception in /history/update")
        return jsonify({"error": str(e)}), 500

@bp.route("/history/message_feedback", methods=["POST"])
async def update_message():
    await cosmos_db_ready.wait()
    authenticated_user = get_authenticated_user_details(request_headers=request.headers)
    user_id = authenticated_user["user_principal_id"]

    ## check request for message_id
    request_json = await request.get_json()
    message_id = request_json.get("message_id", None)
    message_feedback = request_json.get("message_feedback", None)
    try:
        if not message_id:
            return jsonify({"error": "message_id is required"}), 400

        if not message_feedback:
            return jsonify({"error": "message_feedback is required"}), 400

        ## update the message in cosmos
        updated_message = await current_app.cosmos_conversation_client.update_message_feedback(
            user_id, message_id, message_feedback
        )
        if updated_message:
            return (
                jsonify(
                    {
                        "message": f"Successfully updated message with feedback {message_feedback}",
                        "message_id": message_id,
                    }
                ),
                200,
            )
        else:
            return (
                jsonify(
                    {
                        "error": f"Unable to update message {message_id}. It either does not exist or the user does not have access to it."
                    }
                ),
                404,
            )

    except Exception as e:
        logging.exception("Exception in /history/message_feedback")
        return jsonify({"error": str(e)}), 500

@bp.route("/history/delete", methods=["DELETE"])
async def delete_conversation():
    await cosmos_db_ready.wait()
    ## get the user id from the request headers
    authenticated_user = get_authenticated_user_details(request_headers=request.headers)
    user_id = authenticated_user["user_principal_id"]

    ## check request for conversation_id
    request_json = await request.get_json()
    conversation_id = request_json.get("conversation_id", None)

    try:
        if not conversation_id:
            return jsonify({"error": "conversation_id is required"}), 400

        ## make sure cosmos is configured
        if not current_app.cosmos_conversation_client:
            raise Exception("CosmosDB is not configured or not working")

        ## delete the conversation messages from cosmos first
        deleted_messages = await current_app.cosmos_conversation_client.delete_messages(
            conversation_id, user_id
        )

        ## Now delete the conversation
        deleted_conversation = await current_app.cosmos_conversation_client.delete_conversation(
            user_id, conversation_id
        )

        return (
            jsonify(
                {
                    "message": "Successfully deleted conversation and messages",
                    "conversation_id": conversation_id,
                }
            ),
            200,
        )
    except Exception as e:
        logging.exception("Exception in /history/delete")
        return jsonify({"error": str(e)}), 500

@bp.route("/history/list", methods=["GET"])
async def list_conversations():
    await cosmos_db_ready.wait()
    offset = request.args.get("offset", 0)
    authenticated_user = get_authenticated_user_details(request_headers=request.headers)
    user_id = authenticated_user["user_principal_id"]

    ## make sure cosmos is configured
    if not current_app.cosmos_conversation_client:
        raise Exception("CosmosDB is not configured or not working")

    ## get the conversations from cosmos
    conversations = await current_app.cosmos_conversation_client.get_conversations(
        user_id, offset=offset, limit=25
    )
    if not isinstance(conversations, list):
        return jsonify({"error": f"No conversations for {user_id} were found"}), 404

    ## return the conversation ids

    return jsonify(conversations), 200

@bp.route("/history/read", methods=["POST"])
async def get_conversation():
    await cosmos_db_ready.wait()
    authenticated_user = get_authenticated_user_details(request_headers=request.headers)
    user_id = authenticated_user["user_principal_id"]

    ## check request for conversation_id
    request_json = await request.get_json()
    conversation_id = request_json.get("conversation_id", None)

    if not conversation_id:
        return jsonify({"error": "conversation_id is required"}), 400

    ## make sure cosmos is configured
    if not current_app.cosmos_conversation_client:
        raise Exception("CosmosDB is not configured or not working")

    ## get the conversation object and the related messages from cosmos
    conversation = await current_app.cosmos_conversation_client.get_conversation(
        user_id, conversation_id
    )
    ## return the conversation id and the messages in the bot frontend format
    if not conversation:
        return (
            jsonify(
                {
                    "error": f"Conversation {conversation_id} was not found. It either does not exist or the logged in user does not have access to it."
                }
            ),
            404,
        )

    # get the messages for the conversation from cosmos
    conversation_messages = await current_app.cosmos_conversation_client.get_messages(
        user_id, conversation_id
    )

    ## format the messages in the bot frontend format
    messages = []
    for msg in conversation_messages:
        message = {
            "id": msg["id"],
            "role": msg["role"],
            "content": msg.get("content") if msg.get("content") is not None else None,  # Preserve null content
            "createdAt": msg["createdAt"],
            "feedback": msg.get("feedback"),
        }
        
        # Include tool-related fields if they exist
        if "tool_calls" in msg:
            message["tool_calls"] = msg["tool_calls"]
        
        if "tool_call_id" in msg:
            message["tool_call_id"] = msg["tool_call_id"]
        
        if "function_call" in msg:
            message["function_call"] = msg["function_call"]
        
        if "name" in msg:
            message["name"] = msg["name"]
        
        messages.append(message)

    return jsonify({"conversation_id": conversation_id, "messages": messages}), 200

@bp.route("/history/rename", methods=["POST"])
async def rename_conversation():
    await cosmos_db_ready.wait()
    authenticated_user = get_authenticated_user_details(request_headers=request.headers)
    user_id = authenticated_user["user_principal_id"]

    ## check request for conversation_id
    request_json = await request.get_json()
    conversation_id = request_json.get("conversation_id", None)

    if not conversation_id:
        return jsonify({"error": "conversation_id is required"}), 400

    ## make sure cosmos is configured
    if not current_app.cosmos_conversation_client:
        raise Exception("CosmosDB is not configured or not working")

    ## get the conversation from cosmos
    conversation = await current_app.cosmos_conversation_client.get_conversation(
        user_id, conversation_id
    )
    if not conversation:
        return (
            jsonify(
                {
                    "error": f"Conversation {conversation_id} was not found. It either does not exist or the logged in user does not have access to it."
                }
            ),
            404,
        )

    ## update the title
    title = request_json.get("title", None)
    if not title:
        return jsonify({"error": "title is required"}), 400
    conversation["title"] = title
    updated_conversation = await current_app.cosmos_conversation_client.upsert_conversation(
        conversation
    )

    return jsonify(updated_conversation), 200

@bp.route("/history/delete_all", methods=["DELETE"])
async def delete_all_conversations():
    await cosmos_db_ready.wait()
    ## get the user id from the request headers
    authenticated_user = get_authenticated_user_details(request_headers=request.headers)
    user_id = authenticated_user["user_principal_id"]

    # get conversations for user
    try:
        ## make sure cosmos is configured
        if not current_app.cosmos_conversation_client:
            raise Exception("CosmosDB is not configured or not working")

        conversations = await current_app.cosmos_conversation_client.get_conversations(
            user_id, offset=0, limit=None
        )
        if not conversations:
            return jsonify({"error": f"No conversations for {user_id} were found"}), 404

        # delete each conversation
        for conversation in conversations:
            ## delete the conversation messages from cosmos first
            deleted_messages = await current_app.cosmos_conversation_client.delete_messages(
                conversation["id"], user_id
            )

            ## Now delete the conversation
            deleted_conversation = await current_app.cosmos_conversation_client.delete_conversation(
                user_id, conversation["id"]
            )
        return (
            jsonify(
                {
                    "message": f"Successfully deleted conversation and messages for user {user_id}"
                }
            ),
            200,
        )

    except Exception as e:
        logging.exception("Exception in /history/delete_all")
        return jsonify({"error": str(e)}), 500

@bp.route("/history/clear", methods=["POST"])
async def clear_messages():
    await cosmos_db_ready.wait()
    ## get the user id from the request headers
    authenticated_user = get_authenticated_user_details(request_headers=request.headers)
    user_id = authenticated_user["user_principal_id"]

    ## check request for conversation_id
    request_json = await request.get_json()
    conversation_id = request_json.get("conversation_id", None)

    try:
        if not conversation_id:
            return jsonify({"error": "conversation_id is required"}), 400

        ## make sure cosmos is configured
        if not current_app.cosmos_conversation_client:
            raise Exception("CosmosDB is not configured or not working")

        ## delete the conversation messages from cosmos
        deleted_messages = await current_app.cosmos_conversation_client.delete_messages(
            conversation_id, user_id
        )

        return (
            jsonify(
                {
                    "message": "Successfully deleted messages in conversation",
                    "conversation_id": conversation_id,
                }
            ),
            200,
        )
    except Exception as e:
        logging.exception("Exception in /history/clear_messages")
        return jsonify({"error": str(e)}), 500

@bp.route("/history/ensure", methods=["GET"])
async def ensure_cosmos():
    await cosmos_db_ready.wait()
    if not app_settings.chat_history:
        return jsonify({"error": "CosmosDB is not configured"}), 404

    try:
        success, err = await current_app.cosmos_conversation_client.ensure()
        if not current_app.cosmos_conversation_client or not success:
            if err:
                return jsonify({"error": err}), 422
            return jsonify({"error": "CosmosDB is not configured or not working"}), 500

        return jsonify({"message": "CosmosDB is configured and working"}), 200
    except Exception as e:
        logging.exception("Exception in /history/ensure")
        cosmos_exception = str(e)
        if "Invalid credentials" in cosmos_exception:
            return jsonify({"error": cosmos_exception}), 401
        elif "Invalid CosmosDB database name" in cosmos_exception:
            return (
                jsonify(
                    {
                        "error": f"{cosmos_exception} {app_settings.chat_history.database} for account {app_settings.chat_history.account}"
                    }
                ),
                422,
            )
        elif "Invalid CosmosDB container name" in cosmos_exception:
            return (
                jsonify(
                    {
                        "error": f"{cosmos_exception}: {app_settings.chat_history.conversations_container}"
                    }
                ),
                422,
            )
        else:
            return jsonify({"error": "CosmosDB is not working"}), 500

async def generate_title(conversation_messages) -> str:
    ## make sure the messages are sorted by _ts descending
    title_prompt = "Summarize the conversation so far into a 4-word or less title. Do not use any quotation marks or punctuation. Do not include any other commentary or description."

    messages = [
        {"role": msg["role"], "content": msg["content"]}
        for msg in conversation_messages
    ]
    messages.append({"role": "user", "content": title_prompt})

    try:
        azure_openai_client = await init_openai_client()
        model_name = app_settings.azure_openai.model
        model_args = {
            "model": model_name,
            "messages": messages,
            "temperature": 1
        }
        if is_reasoning_model(model_name):
            model_args["max_completion_tokens"] = 512
        else:
            model_args["max_tokens"] = 64
        response = await azure_openai_client.chat.completions.create(**model_args)
        title = response.choices[0].message.content
        return title
    except Exception as e:
        logging.exception("Exception while generating title", e)
        return messages[-2]["content"]

# Parse the provided URL
def parse_url(url):
    parsed_url = urlparse(url)
    account_name = parsed_url.netloc.split('.')[0]
    container_name = parsed_url.path.split('/')[1]
    return account_name, container_name

# Create a service SAS token for the container
def create_service_sas_container(container_client: 'ContainerClient', account_key: str):
    # Create a SAS token that's valid for one day, as an example
    start_time = datetime.datetime.now(datetime.timezone.utc)
    expiry_time = start_time + datetime.timedelta(minutes=15)
    sas_token = generate_container_sas(
        account_name=container_client.account_name,
        container_name=container_client.container_name,
        account_key=account_key,
        permission=ContainerSasPermissions(read=True),
        expiry=expiry_time,
        start=start_time
    )
    return sas_token

@bp.route("/citationConfig", methods=["GET"])
def citationConfig():
    try:
        citation_config = {}
        if app_settings.citation_file:
            citation_config = {
                "FileStorageBaseUrl": app_settings.citation_file.storage_base_url,
                "FileLinkBaseUrl": app_settings.citation_file.link_base_url,
                "FileLinkUrlAppendix": app_settings.citation_file.link_url_appendix
            }
        return jsonify(citation_config), 200
    except Exception as e:
        details = jsonify({"error": str(e)})
        logging.exception("Exception in /citationConfig: ", details)
        return details, 500

@bp.route("/storageSas", methods=["GET"])
def storageSas():
    try:
        if not app_settings.citation_file or not app_settings.citation_file.storage_base_url:
            return jsonify({"error": "Citation file storage not configured"}), 400
            
        account_name, container_name = parse_url(app_settings.citation_file.storage_base_url)
        account_key = app_settings.citation_file.storage_account_key
        credential = DefaultAzureCredential()
        container_client = ContainerClient(account_url=f"https://{account_name}.blob.core.windows.net", container_name=container_name, credential=credential)
        sas_token = create_service_sas_container(container_client, account_key)
        return sas_token, 200
    except Exception as e:
        details = jsonify({"error": str(e)})
        logging.exception("Exception in /storageSas: ", details)
        return details, 500

# ---------------------------------------------------------------------------
# Protected Resource Metadata endpoint (RFC 9728)
# MCP clients fetch this to discover the Entra ID authorization server and
# available OAuth scopes.  Must be accessible WITHOUT authentication.
# ---------------------------------------------------------------------------

@bp.route("/.well-known/oauth-protected-resource", methods=["GET"])
async def oauth_protected_resource():
    """Serve OAuth 2.0 Protected Resource Metadata per RFC 9728."""
    if not remote_mcp_server:
        return jsonify({"error": "Remote MCP server not initialized"}), 503

    metadata = remote_mcp_server.get_prm_metadata()
    if not metadata:
        return jsonify({"error": "Remote MCP server auth is not configured"}), 503

    response = await make_response(jsonify(metadata))
    response.headers["Cache-Control"] = "public, max-age=3600"
    response.headers["Access-Control-Allow-Origin"] = "*"
    return response, 200


# ---------------------------------------------------------------------------
# MCP Streamable HTTP transport
#
# POST/GET/DELETE /mcp are handled by FastMCP's Starlette ASGI app, which is
# mounted in the ASGI middleware layer (see _MCPASGIDispatch and
# init_remote_mcp_server).  The Quart Blueprint does NOT handle these routes
# directly so that streaming (SSE) and full MCP protocol negotiation work
# correctly with FastMCP's transport layer.
# ---------------------------------------------------------------------------


app = create_app()
