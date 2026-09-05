import threading
import urllib.parse
from playwright.sync_api import sync_playwright

_browser_lock = threading.Lock()


def _parse_results(page, max_results: int) -> list:
    results = []
    items = page.query_selector_all("li.b_algo")
    for item in items:
        a_el = item.query_selector("h2 a")
        if not a_el:
            continue
        snippet_el = item.query_selector(".b_caption p")
        title = a_el.text_content().strip()
        href = a_el.get_attribute("href") or ""
        snippet = snippet_el.inner_text().strip() if snippet_el else ""
        results.append({"title": title, "url": href, "snippet": snippet})
        if len(results) >= max_results:
            break
    return results


def _parse_generic_results(page, max_results: int) -> list:
    title = page.title()
    body = page.query_selector("body")
    text = body.inner_text().strip() if body else ""
    lines = [l.strip() for l in text.split("\n") if l.strip()]
    snippet = "\n".join(lines[:50])
    return [{"title": title, "url": page.url, "snippet": snippet}]


def _format_results(results: list) -> str:
    if not results:
        return ""
    lines = []
    for r in results:
        lines.append(f"## {r['title']}")
        if r["url"]:
            lines.append(f"来源: {r['url']}")
        if r["snippet"]:
            lines.append(r["snippet"])
        lines.append("")
    return "\n".join(lines).strip()


def browser_search(query: str, max_results: int = 5,
                   search_url: str = None) -> str:
    try:
        default = "https://www.bing.com/search?q={query}&count={count}"
        url = search_url or default
        q = urllib.parse.quote(query)
        with _browser_lock:
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True)
                page_url = url.replace("{query}", q).replace("{count}", str(max_results))
                page = browser.new_page()
                page.goto(page_url, timeout=20000)
                if url == default:
                    results = _parse_results(page, max_results)
                else:
                    results = _parse_generic_results(page, max_results)
                page.close()
                browser.close()
                return _format_results(results)
    except Exception as e:
        return ""
