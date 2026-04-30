"""search_web — SearXNG integration for fast information retrieval.

Always prefer this over launching a browser for research/fact-finding.
SearXNG runs as a Docker container: docker run -d --name searxng -p 8080:8080 searxng/searxng
"""

import logging

import aiohttp

from core.tools import tool

logger = logging.getLogger(__name__)

SEARXNG_URL = "http://localhost:8080/search"


@tool(
    name="search_web",
    description=(
        "Search the web using SearXNG. Returns titles, URLs, and snippets. "
        "Always use this first for information retrieval — 10× faster than browser. "
        "Only escalate to browser if you need to interact with a page."
    ),
    schema={
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Search query"},
            "num_results": {"type": "integer", "description": "Number of results (default 5, max 10)"},
        },
        "required": ["query"],
    },
)
async def search_web(query: str, num_results: int = 5, **kwargs) -> str:
    num_results = min(num_results, 10)
    params = {
        "q": query,
        "format": "json",
        "pageno": 1,
    }

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                SEARXNG_URL, params=params, timeout=aiohttp.ClientTimeout(total=10)
            ) as resp:
                if resp.status != 200:
                    return f"SearXNG returned HTTP {resp.status}. Is the Docker container running?"
                data = await resp.json()
    except aiohttp.ClientConnectorError:
        return (
            "Cannot connect to SearXNG at localhost:8080. "
            "Start it with: docker run -d --name searxng -p 8080:8080 searxng/searxng"
        )
    except Exception as exc:
        return f"Search error: {exc}"

    results = data.get("results", [])[:num_results]
    if not results:
        return f"No results found for: {query}"

    lines = [f"Search results for: {query}\n"]
    for i, r in enumerate(results, 1):
        title = r.get("title", "No title")
        url = r.get("url", "")
        snippet = r.get("content", "")[:200]
        lines.append(f"[{i}] {title}\n    {url}\n    {snippet}\n")

    return "\n".join(lines)
