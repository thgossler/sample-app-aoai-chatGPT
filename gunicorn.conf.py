import multiprocessing

max_requests = 1000
max_requests_jitter = 50
log_file = "-"
bind = "0.0.0.0"

timeout = 230
# https://learn.microsoft.com/en-us/troubleshoot/azure/app-service/web-apps-performance-faqs#why-does-my-request-time-out-after-230-seconds

num_cpus = multiprocessing.cpu_count()
# Use a single worker when the Remote MCP Server is enabled because FastMCP
# stores Streamable-HTTP sessions in-memory; multiple workers would cause
# "Missing session ID" errors when requests are routed to a different process.
# A single Uvicorn (asyncio) worker can still handle high concurrency.
import os as _os
if _os.environ.get("REMOTE_MCP_SERVER_ENABLED", "").lower() == "true":
    workers = 1
else:
    workers = (num_cpus * 2) + 1
worker_class = "uvicorn.workers.UvicornWorker"
