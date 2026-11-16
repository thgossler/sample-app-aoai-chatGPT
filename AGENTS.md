# Copilot Instructions — Azure OpenAI Chat Sample App

## Project Overview

This is **Microsoft's sample Azure OpenAI ChatGPT web application** — a full-stack chat interface for Azure OpenAI with support for "On Your Data" (OYD) grounding, chat history persistence (Cosmos DB), MCP (Model Context Protocol) tool calling, RAG retrieval for reasoning models, and multiple data source backends. It serves as a production-ready reference architecture for enterprise ChatGPT deployments on Azure.

## Architecture

**Monorepo** with a Python/Quart async backend serving a React/TypeScript SPA.

```
├── app.py                  # Main backend entry point (~1900 lines, all routes)
├── backend/                # Python backend modules
│   ├── settings.py         # Pydantic-based app settings (loaded from .env)
│   ├── utils.py            # Streaming, response formatting, MS Graph helpers
│   ├── mcp_manager.py      # MCP server lifecycle and tool orchestration
│   ├── rag_service.py      # Direct Azure Search RAG for reasoning models
│   ├── auth/               # Azure EasyAuth identity extraction
│   ├── history/            # Cosmos DB conversation persistence
│   ├── security/           # MS Defender for AI integration
│   └── mcp_servers/        # MCP server configs and implementations
├── frontend/               # React 18 + TypeScript + Vite SPA
│   └── src/
│       ├── api/            # fetch()-based API client (NDJSON streaming)
│       ├── components/     # Feature-grouped UI components (Answer, ChatHistory, QuestionInput)
│       ├── pages/          # Chat, Layout, NoPage
│       ├── state/          # React Context + useReducer global state
│       ├── constants/      # Sanitization allowlists, sample data
│       └── theme/          # Fluent UI dark/light theming
├── static/                 # Vite build output (served by Quart)
├── infra/                  # Bicep IaC for Azure deployment
├── tests/                  # pytest + pytest-asyncio
├── scripts/                # Data prep, auth init, document chunking
└── tools/                  # Utility scripts
```

## Quickstart

### Prerequisites
- Python 3.11+ with `pip`
- Node.js 18+ with `npm`
- An Azure OpenAI resource with a deployed chat model
- A `.env` file in the project root (see [Configuration](#configuration))

### Local Development
```bash
# Full build + start (builds frontend, installs backend deps, starts server)
./start.sh

# Or build only:
./build.sh                   # Both backend + frontend
./build_backend.sh           # Create .venv, pip install requirements.txt
./build_frontend.sh          # npm install + npm run build → static/

# Dev server (backend only, assumes frontend already built):
source .venv/bin/activate
python3 -m uvicorn app:app --port 8081 --reload

# Frontend dev server (with API proxy to localhost:8081):
cd frontend && npm run dev
```

The app runs at `http://127.0.0.1:50505` (start.sh) or `http://127.0.0.1:8081` (uvicorn directly).

### Running Tests
```bash
pip install -r requirements-dev.txt
python -m pytest tests/
```

## Configuration

All configuration is via environment variables, loaded from a `.env` file through **pydantic-settings** (`BaseSettings`). The `.env` file is gitignored. Key env var groups:

| Prefix | Purpose |
| ------ | ------- |
| `AZURE_OPENAI_*` | Model name, endpoint, API key, temperature, system message, embeddings |
| `AZURE_SEARCH_*` | Search service, index, query type, content/vector field columns |
| `AZURE_COSMOSDB_*` | Chat history (account, database, containers) |
| `UI_*` | App title, logo, chat description, footer HTML |
| `DATASOURCE_TYPE` | Data source selector (`AzureCognitiveSearch`, `Elasticsearch`, `Pinecone`, etc.) |
| `ELASTICSEARCH_*` | Elasticsearch connection settings |
| `PINECONE_*` | Pinecone connection settings |
| `PROMPTFLOW_*` | PromptFlow endpoint and key |
| `SW360_*` | SW360 MCP server credentials |
| `AUTH_ENABLED` | Enable/disable Azure EasyAuth (`true`/`false`) |
| `DEBUG` | Enable debug logging |

System messages support base64 encoding (auto-detected and decoded in `settings.py`).

## Backend Details

### Framework & Server
- **Quart** (async Flask-compatible ASGI) with a single Blueprint (`bp` in `app.py`)
- **Gunicorn** + **Uvicorn workers** in production (`gunicorn.conf.py`: 230s timeout, `(CPU*2)+1` workers)
- **AsyncAzureOpenAI** client from the `openai` library

### API Routes (all defined in `app.py`)

| Route | Method | Purpose |
| ----- | ------ | ------- |
| `/conversation` | POST | Stateless chat completion (streaming/non-streaming) |
| `/frontend_settings` | GET | UI config, auth status, feature flags |
| `/history/generate` | POST | Chat with Cosmos DB persistence |
| `/history/update` | POST | Save assistant + tool messages |
| `/history/list` | GET | List user conversations |
| `/history/read` | POST | Read conversation messages |
| `/history/rename` | POST | Rename conversation |
| `/history/delete` | DELETE | Delete conversation |
| `/history/delete_all` | DELETE | Delete all user conversations |
| `/history/clear` | POST | Clear messages in conversation |
| `/history/ensure` | GET | Cosmos DB health check |
| `/history/message_feedback` | POST | Thumbs up/down feedback |
| `/citationConfig` | GET | Citation file link configuration |
| `/storageSas` | GET | SAS token for blob storage citations |

### Authentication
Azure EasyAuth via `X-Ms-Client-Principal-*` headers. Document-level ACL via MS Graph `transitiveMemberOf` for Azure Search permitted groups. Auth extraction logic in `backend/auth/auth_utils.py`.

### MCP Integration (`backend/mcp_manager.py`)
- Server configs in `backend/mcp_servers/mcp_servers.json`
- Types: `local_stdio` (Python FastMCP over stdio), `local_http`, `remote_http`
- Tools are registered in OpenAI function-calling format and injected into model args
- Parallel tool execution via `asyncio.gather()`
- Model-aware API selection: `role:"tool"` for gpt-4.1+/gpt-5+/o-series, `role:"function"` for older models

### RAG Service (`backend/rag_service.py`)
- For reasoning models (o1, o3, o4, gpt-5) that can't use OYD extensions
- `AzureSearchRAGRetriever` queries Azure Search directly (vector/semantic/hybrid)
- Context is injected into the system prompt with citation metadata

### Data Sources
Configured via `DATASOURCE_TYPE`: Azure Cognitive Search, Elasticsearch, Pinecone, Cosmos DB Mongo vCore, Azure ML Index, Azure SQL Server, MongoDB.

## Frontend Details

### Stack
- **React 18** + **TypeScript** + **Vite** (builds to `../static/`)
- **Fluent UI v8** (`@fluentui/react`) for UI components
- **react-markdown** + **remark-gfm** + **rehype-raw** for message rendering
- **react-syntax-highlighter** (Nord theme) for code blocks
- **DOMPurify** for XSS sanitization
- **react-router-dom** v6 with `HashRouter`

### State Management
React Context + `useReducer` via `AppStateContext` in `state/AppProvider.tsx`. Actions are discriminated unions. No Redux.

### API Layer
Plain `fetch()` in `src/api/api.ts`. Streaming uses `application/json-lines` (NDJSON). All calls use relative URLs, proxied in dev via Vite config.

### Routing
`HashRouter` with routes: `/` → `Layout` > `Chat`, `*` → `NoPage`.

## Code Conventions

### Python
- **snake_case** for functions/variables, **PascalCase** for classes
- `async/await` throughout (Quart is async-native)
- **Pydantic v2** models with validators, field aliases, and model validators for settings
- Type hints on function signatures
- `logging` module for all log output
- Settings loaded from `.env` via `pydantic-settings` `BaseSettings` with `SettingsConfigDict`

### TypeScript / React
- **PascalCase** for components and component files, **camelCase** for functions/variables
- Functional components only (no class components), hooks-based
- Props via `interface Props { ... }` per component
- **CSS Modules** (`*.module.css`) for component-scoped styling
- Barrel exports (`index.ts`) for component directories
- `const` for component definitions, arrow functions for event handlers
- Fluent UI `Stack`, `TextField`, `Dialog`, `CommandBarButton`, `IconButton` for UI primitives

### File Organization
- Components grouped by feature in subdirectories with co-located CSS modules
- API layer centralized in `frontend/src/api/`
- State management in `frontend/src/state/`
- Backend modules under `backend/` with `auth/`, `security/`, `history/` as sub-packages

## Deployment

- **Azure Developer CLI** (`azd`): Primary deployment path via `azure.yaml`
- **Target**: Azure App Service (Linux, Python 3.11, B1 SKU)
- **Infrastructure**: Bicep templates in `infra/` provision App Service Plan, Azure OpenAI, Azure Search, Cosmos DB, Form Recognizer
- **Docker**: `WebApp.Dockerfile` for containerized deployment
- **Production server**: `python3 -m gunicorn app:app` with Uvicorn workers
- **CI/CD**: GitHub Actions workflows for Docker build/publish, Python tests, Node.js lint, static file checks

## Testing

- **pytest** + **pytest-asyncio** for async tests
- `tests/unit_tests/` — Settings loading, utils, MCP integration
- `tests/integration_tests/` — Parameterized tests for data source configurations
- Per-test `.env` files in `tests/unit_tests/dotenv_data/`
- Jinja2 templates in `tests/integration_tests/dotenv_templates/` for env generation
- `unittest.mock` with `patch`, `AsyncMock`, `MagicMock`

## RAG Data Flow & Citation Handling

This section explains the end-to-end flow from user question to rendered answer with clickable document references.

### End-to-End Flow Overview

```
┌──────────┐    POST /conversation     ┌──────────────┐   Azure Search    ┌────────────────┐
│ Frontend │  ───────────────────────>  │   Backend    │  ──────────────>  │ Azure AI Search│
│ (React)  │  { messages: [...] }       │  (app.py)    │  semantic/vector  │   (RAG Index)  │
│          │                            │              │  <──────────────  │                │
│          │                            │              │  search results   │                │
│          │                            │              │  (chunks + meta)  └────────────────┘
│          │                            │              │
│          │                            │  ┌───────────┴──────────┐
│          │                            │  │ Two paths:           │
│          │                            │  │ 1. OYD (non-reason.) │
│          │                            │  │ 2. Manual RAG        │
│          │                            │  │    (reasoning models) │
│          │                            │  └───────────┬──────────┘
│          │                            │              │
│          │                            │              │   chat.completions
│          │                            │              │  ──────────────>  ┌──────────────┐
│          │                            │              │                   │ Azure OpenAI │
│          │    NDJSON stream           │              │  <──────────────  │   (LLM)      │
│          │  <───────────────────────  │              │  answer w/ [docN] └──────────────┘
│          │  { role:"tool",            │              │
│          │    content: citations }    └──────────────┘
│          │  { role:"assistant",
│          │    content: "...answer..." }
└──────────┘
```

### Step 1: Frontend Sends User Message

The frontend sends the full conversation history to the backend via `POST /conversation` (stateless) or `POST /history/generate` (with Cosmos DB persistence). The request body is:

```typescript
// frontend/src/api/api.ts → conversationApi()
{
  messages: [
    { role: "user", content: "What is the architecture of system X?" },
    // ... prior conversation messages
  ]
}
```

The API calls are plain `fetch()` in `frontend/src/api/api.ts`. The `conversationApi()` function is used for stateless mode; `historyGenerate()` is used when chat history persistence is enabled.

### Step 2: Backend Builds Azure OpenAI Request

In `app.py`, the `conversation_internal()` function routes to either `stream_chat_request()` (streaming) or `complete_chat_request()` (non-streaming).

The `prepare_model_args()` function builds the OpenAI API call arguments:

- Prepends the **system message** (`AZURE_OPENAI_SYSTEM_MESSAGE`)
- Adds all conversation messages
- Injects MCP tools if available
- **For non-reasoning models with a data source**: Attaches the data source config via `extra_body.data_sources` — this is the **OYD (On Your Data)** path, where Azure OpenAI handles the search internally
- **For reasoning models** (o1, o3, o4, gpt-5): Skips OYD and routes to `send_chat_request_with_rag()` for **manual RAG**

### Step 3a: OYD Path (Non-Reasoning Models)

For non-reasoning models with `DATASOURCE_TYPE` configured, the data source configuration is built by `app_settings.datasource.construct_payload_configuration()` and sent as `extra_body.data_sources` to the Azure OpenAI API. Azure OpenAI performs the search internally and returns citations in the response via `message.context`.

The response stream includes `delta.context` containing citation objects, which `format_stream_response()` in `backend/utils.py` wraps as:

```json
{ "role": "tool", "content": "{\"citations\": [...], \"intent\": \"...\"}" }
```

### Step 3b: Manual RAG Path (Reasoning Models)

For reasoning models, `send_chat_request_with_rag()` in `app.py`:

1. Extracts the latest user query from the message history
2. Calls `rag_service.retrieve_context(query)` which queries Azure AI Search directly via `AzureSearchRAGRetriever`
3. The retriever supports **simple text**, **vector** (via Azure OpenAI embeddings), **semantic**, and **hybrid** search modes
4. Retrieved documents become `RAGDocument` objects with fields: `content`, `title`, `url`, `filename`, `score`, `chunk_id`
5. Context is formatted and injected into the user message via `rag_service.format_context_for_prompt()`:

```
The following information was retrieved from internal documents...
---
[1] <content of chunk 1>
[2] <content of chunk 2>
---
User Query: <original question>
```

6. Citations are attached to the response object as `response._citations` and injected into the stream as a `role: "tool"` message before the assistant content

### Step 4: Backend Streams Response to Frontend

Responses are streamed as **NDJSON** (`application/json-lines`). Each line is a JSON object with this structure:

```json
{
  "id": "chatcmpl-...",
  "model": "gpt-4",
  "choices": [{
    "messages": [
      {
        "role": "tool",
        "content": "{\"citations\": [{...}, {...}], \"intent\": \"...\"}"
      }
    ]
  }],
  "history_metadata": { "conversation_id": "...", "title": "...", "date": "..." }
}
```

Followed by assistant content chunks:

```json
{
  "choices": [{
    "messages": [
      { "role": "assistant", "content": "Based on the documentation [doc1]..." }
    ]
  }]
}
```

### Step 5: Frontend Parses Streaming Response

In `Chat.tsx`, the `makeApiRequestWithoutCosmosDB()` / `makeApiRequestWithCosmosDB()` functions read the NDJSON stream via `response.body.getReader()`:

1. Each NDJSON line is parsed as JSON
2. Messages are processed by `processResultMessage()`:
   - **`role: "tool"`** messages are stored as `toolMessage` — these contain the citation data
   - **`role: "assistant"`** messages are accumulated as `assistantMessage` — the answer text with `[docN]` references
   - If the assistant message has a `context` field (from OYD), it's wrapped as a synthetic tool message

### Step 6: Citation Parsing (`AnswerParser.tsx`)

The `parseAnswer()` function in `frontend/src/components/Answer/AnswerParser.tsx`:

1. Finds all `[docN]` references in the answer text via regex (`/\[(doc\d\d?\d?)]/g`)
2. Maps each `[docN]` to the corresponding citation from the citations array (1-based index)
3. Replaces `[docN]` with superscript markers ` ^N^ ` for display
4. Deduplicates citations by `id` and assigns `reindex_id` for sequential numbering
5. Calls `enumerateCitations()` to assign `part_index` — tracks which part/chunk of the same file this citation represents

### Step 7: Citation Object Structure

A citation object (from OYD or manual RAG) has this shape:

```typescript
// frontend/src/api/models.ts
type Citation = {
  part_index?: number     // Which chunk of the same file (1-based)
  content: string         // The chunk text content
  id: string              // Citation index
  title: string | null    // Document title
  filepath: string | null // Original file path in the search index
  url: string | null      // Direct URL to the source document
  metadata: string | null // Additional metadata
  chunk_id: string | null // Chunk identifier within the document
  reindex_id: string | null // Display-order index (set by AnswerParser)
}
```

**Custom metadata in chunk content**: For pre-chunked RAG data, the chunk `content` field can contain embedded metadata lines that the frontend parses. The `Answer.tsx` component's `updateCitation()` function extracts these via regex:

```
source_url: https://dev.azure.com/org/project/_wiki/wikis/...
source_title: Architecture Overview - Data Platform
source_file: /architecture/data-platform.md
chunk_index: 2
chunk_total: 5
```

When these fields are present, they override the citation's `url`, `title`, `filepath`, and `part_index` properties. This allows pre-processed data to carry link information directly in the chunk content, enabling the frontend to construct correct URLs regardless of how documents were indexed.

### Step 8: Opening Referenced Documents

When a user clicks a citation in the `Answer` component, `onShowCitation()` in `Chat.tsx` handles document opening. The behavior depends on the document type and the **citation file configuration** (fetched from `/citationConfig`):

#### Citation Config (env vars → `/citationConfig` endpoint)

```typescript
type CitationConfig = {
  FileStorageBaseUrl: string | null  // AZURE_SEARCH_CITATION_FILE_STORAGE_BASE_URL
  FileLinkBaseUrl: string | null     // AZURE_SEARCH_CITATION_FILE_LINK_BASE_URL
  FileLinkUrlAppendix: string | null // AZURE_SEARCH_CITATION_FILE_LINK_URL_APPENDIX
}
```

- **`FileStorageBaseUrl`**: The Azure Blob Storage base URL where chunked source files are stored (e.g., `https://storageaccount.blob.core.windows.net/container`)
- **`FileLinkBaseUrl`**: The base URL for the user-facing link target (e.g., an Azure DevOps Wiki URL or document portal)
- **`FileLinkUrlAppendix`**: Optional suffix appended to constructed links (e.g., query parameters)

#### Document Type Routing Logic

The `onShowCitation()` function uses the following decision tree:

1. **Azure DevOps Wiki / Markdown files** (`_wiki` in URL, or `.md` extension):
   - Extracts the relative file path by stripping the blob storage base URL
   - If `FileLinkBaseUrl` contains `_wiki`: strips `.md` extension (DevOps Wiki convention)
   - Constructs: `FileLinkBaseUrl + encodeURIComponent(relFilePath) + FileLinkUrlAppendix`
   - Opens in new tab via `window.open(url, '_blank')`

2. **PDF files** (`.pdf` extension):
   - Opens directly from blob storage with a SAS token: `citation.url + '?' + storageSas`
   - SAS token is fetched from `/storageSas` endpoint on page load

3. **Direct URL citations** (no `FileStorageBaseUrl` configured, or URL doesn't match storage):
   - Opens `citation.url` directly in a new tab
   - The `onViewSource()` function specifically avoids opening blob storage URLs directly (checks `!citation.url.includes('blob.core')`)

4. **Citation panel** (when `usePanel=true`):
   - Opens a side panel showing the citation content text instead of navigating away

#### Link Target Override

The `link-target` query parameter in the app URL overrides the default `_blank` target:
```
https://app.example.com/#/?link-target=_self
```

### Data Preparation & Chunk Format

Documents are prepared for the Azure AI Search index using scripts in `scripts/` (e.g., `data_preparation.py`, `chunk_documents.py`). Each chunk stored in the search index typically contains:

| Field | Purpose |
| ----- | ------- |
| `id` | Unique chunk identifier |
| `content` | The text chunk (may include embedded `source_url`, `source_title`, `source_file`, `chunk_index`, `chunk_total` metadata) |
| `title` | Document title |
| `filepath` | Original file path or name |
| `url` | Direct URL to the source document |
| Content field(s) | Configured via `AZURE_SEARCH_CONTENT_COLUMNS` |
| Vector field(s) | Configured via `AZURE_SEARCH_VECTOR_COLUMNS` (for vector/hybrid search) |
| Title field | Configured via `AZURE_SEARCH_TITLE_COLUMN` |
| URL field | Configured via `AZURE_SEARCH_URL_COLUMN` |
| Filename field | Configured via `AZURE_SEARCH_FILENAME_COLUMN` |

The search index field mapping is fully configurable via environment variables, allowing different index schemas to work without code changes.

## Key Considerations for Code Changes

1. **`app.py` is monolithic** — all routes live in one file. When adding routes, add them to the `bp` Blueprint.
2. **Frontend must be rebuilt** after changes: `cd frontend && npm run build` (output → `static/`).
3. **Settings are Pydantic models** — add new env vars by extending the appropriate `_*Settings` class in `backend/settings.py`.
4. **Streaming responses** use NDJSON format — maintain this pattern for any new streaming endpoints.
5. **MCP tools** are dynamically registered — add new servers via `backend/mcp_servers/mcp_servers.json`.
6. **Reasoning models** (o1/o3/o4/gpt-5) use the RAG service path instead of OYD — test both paths when modifying chat completion logic.
7. **XSS protection** — all rendered HTML/markdown passes through DOMPurify. Maintain sanitization for any new user-facing content.
8. **Auth headers** — never trust client-provided identity; always use `get_authenticated_user_details()` from EasyAuth headers.
