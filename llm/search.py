import requests

TAVILY_URL = "https://api.tavily.com/search"


def tavily_search(api_key: str, query: str, max_results: int = 5) -> str:
    if not api_key:
        return ""
    try:
        resp = requests.post(
            TAVILY_URL,
            json={
                "api_key": api_key,
                "query": query,
                "search_depth": "basic",
                "max_results": max_results,
            },
            timeout=15,
        )
        if resp.status_code == 401:
            return ""
        resp.raise_for_status()
        data = resp.json()
        results = data.get("results", [])
        if not results:
            return ""
        lines = []
        for r in results:
            title = r.get("title", "")
            content = r.get("content", "")
            lines.append(f"## {title}\n{content}")
        return "\n\n".join(lines)
    except Exception:
        return ""
