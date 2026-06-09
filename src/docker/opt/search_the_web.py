#!/usr/bin/env python3
"""
Usage examples:
  python searxng_search.py "query"
  python searxng_search.py "query" --json -o results.json
  python searxng_search.py "query" --csv --infobox
  python searxng_search.py "query" --pageno 2 --safesearch 0
"""

import argparse
import csv
import json
import sys
from pathlib import Path
from typing import Any

from bs4 import BeautifulSoup
from curl_cffi import requests

BASE_URL = "https://search.ctq.ro/searxng/search"
RESULT_SELECTOR = "article.result"
URL_SELECTOR = "a.url_header"
TITLE_SELECTOR = "h3 a"
CONTENT_SELECTOR = "p.content"
ENGINES_SELECTOR = "div.engines > span"


def parse_results(html: str) -> list[dict[str, Any]]:
    """Parse the HTML and return a list of result dicts."""
    soup = BeautifulSoup(html, "html.parser")
    results: list[dict[str, Any]] = []
    for article in soup.select(RESULT_SELECTOR):
        url_tag = article.select_one(URL_SELECTOR)
        title_tag = article.select_one(TITLE_SELECTOR)
        content_tag = article.select_one(CONTENT_SELECTOR)
        engine_tags = article.select(ENGINES_SELECTOR)

        url = url_tag["href"].strip() if url_tag and url_tag.has_attr("href") else None
        title = title_tag.get_text(strip=True) if title_tag else None
        if content_tag and "empty_element" not in (content_tag.get("class") or []):
            content = content_tag.get_text(" ", strip=True)
        else:
            content = ""
        engines = [tag.get_text(strip=True) for tag in engine_tags if tag.get_text(strip=True)]

        results.append({
            "url": url,
            "title": title,
            "content": content,
            "engines": engines,
        })
    return results


def extract_infos(html: str) -> list[dict[str, Any]]:
    """Extract the sidebar infobox (e.g. Wikipedia-style info box)."""
    soup = BeautifulSoup(html, "html.parser")
    boxes: list[dict[str, Any]] = []
    for box in soup.select("aside.infobox"):
        title_tag = box.select_one("h2.title")
        desc_tag = box.find("p")
        attributes: dict[str, str] = {}
        for dl in box.select("dl"):
            dt = dl.find("dt")
            dd = dl.find("dd")
            if dt and dd:
                key = dt.get_text(" ", strip=True).rstrip(" :")
                attributes[key] = dd.get_text(" ", strip=True)
        boxes.append({
            "title": title_tag.get_text(" ", strip=True) if title_tag else "",
            "description": desc_tag.get_text(" ", strip=True) if desc_tag else "",
            "attributes": attributes,
        })
    return boxes


def search(
    query: str,
    language: str = "en",
    safesearch: str = "0",
    category: str = "general",
    pageno: int = 1,
    time_range: str = "",
) -> str:
    """Perform a search request and return the HTML response."""
    data = {
        "q": query,
        f"category_{category}": "1",
        "language": language,
        "time_range": time_range,
        "safesearch": safesearch,
        "theme": "simple",
    }
    if pageno > 1:
        data["pageno"] = str(pageno)

    cookie_header = (
        f"categories={category}; "
        f"language=auto; "
        f"locale=en; "
        f"autocomplete=; "
        f"favicon_resolver=; "
        f"method=POST; "
        f"safesearch={safesearch}; "
        f"theme=simple; "
        f"results_on_new_tab=0; "
        f"doi_resolver=oadoi.org; "
        f"simple_style=auto; "
        f"center_alignment=0; "
        f"query_in_title=0; "
        f"search_on_category_select=1; "
        f"hotkeys=default; "
        f"url_formatting=pretty; "
        f"disabled_engines=; "
        f'enabled_engines="duckduckgo__general\\054qwant__general\\054yahoo__general\\054mojeek__general"; '
        f"enabled_plugins=; "
        f"tokens="
    )

    headers = {
        "Cookie": cookie_header,
        "Content-Type": "application/x-www-form-urlencoded",
    }

    response = requests.post(
        BASE_URL,
        data=data,
        impersonate="chrome",
        headers=headers,
    )
    response.raise_for_status()
    return response.text


def write_json(data: Any, output: str | None) -> None:
    text = json.dumps(data, indent=2, ensure_ascii=False)
    if output:
        Path(output).write_text(text, encoding="utf-8")
    else:
        print(text)


def write_csv(results: list[dict[str, Any]], output: str | None) -> None:
    if not results:
        return
    fieldnames = list(results[0].keys())
    out_file = open(output, "w", newline="", encoding="utf-8") if output else sys.stdout
    try:
        writer = csv.DictWriter(out_file, fieldnames=fieldnames)
        writer.writeheader()
        for row in results:
            row = {**row, "engines": ", ".join(row.get("engines") or [])}
            writer.writerow(row)
    finally:
        if output:
            out_file.close()


def write_text(results: list[dict[str, Any]], output: str | None) -> None:
    lines: list[str] = []
    for i, r in enumerate(results, 1):
        lines.append(f"{i}. {r['title']}")
        lines.append(f"   URL:     {r['url']}")
        lines.append(f"   Engines: {', '.join(r['engines']) or '-'}")
        if r["content"]:
            lines.append(f"   Snippet: {r['content']}")
        lines.append("")
    text = "\n".join(lines).rstrip() + "\n"
    if output:
        Path(output).write_text(text, encoding="utf-8")
    else:
        print(text, end="")


def main():
    parser = argparse.ArgumentParser(
        description="Search using SearXNG and display results."
    )
    parser.add_argument("query", help="Search query")
    parser.add_argument("-l", "--language", default="en", help="Language code (default: en)")
    parser.add_argument("-s", "--safesearch", default="0", choices=["0", "1", "2"],
                        help="SafeSearch: 0=None, 1=Moderate, 2=Strict")
    parser.add_argument("-c", "--category", default="general",
                        help="Search category (default: general)")
    parser.add_argument("-p", "--pageno", type=int, default=1,
                        help="Page number (default: 1)")
    parser.add_argument("-t", "--time-range", default="",
                        help="Time range (day, week, month, year)")
    parser.add_argument("--json", action="store_true", help="Output as JSON")
    parser.add_argument("--csv", action="store_true", help="Output as CSV")
    parser.add_argument("--text", action="store_true", help="Output as plain text")
    parser.add_argument("--infobox", action="store_true",
                        help="Include infobox data (only for JSON/text output)")
    parser.add_argument("-o", "--output", help="Write output to file instead of stdout")
    args = parser.parse_args()
    if not (args.json or args.csv):
        args.text = True
    html = search(
        query=args.query,
        language=args.language,
        safesearch=args.safesearch,
        category=args.category,
        pageno=args.pageno,
        time_range=args.time_range,
    )
    results = parse_results(html)
    if args.json:
        data = {"results": results}
        if args.infobox:
            data["infobox"] = extract_infos(html)
        write_json(data, args.output)
    elif args.csv:
        if args.infobox:
            print("Warning: --infobox ignored for CSV output.", file=sys.stderr)
        write_csv(results, args.output)
    elif args.text:
        lines = []
        if args.infobox:
            infos = extract_infos(html)
            for box in infos:
                lines.append(f"[Infobox] {box['title']}")
                lines.append(f"  Description: {box['description']}")
                for key, val in box["attributes"].items():
                    lines.append(f"  {key}: {val}")
                lines.append("")
        for i, r in enumerate(results, 1):
            lines.append(f"{i}. {r['title']}")
            lines.append(f"   URL:     {r['url']}")
            lines.append(f"   Engines: {', '.join(r['engines']) or '-'}")
            if r["content"]:
                lines.append(f"   Snippet: {r['content']}")
            lines.append("")
        text_output = "\n".join(lines).rstrip() + "\n"
        if args.output:
            Path(args.output).write_text(text_output, encoding="utf-8")
        else:
            print(text_output, end="")


if __name__ == "__main__":
    main()
