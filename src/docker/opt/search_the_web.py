#!/usr/bin/env python3
"""
Search the web using the Brave Search API.

Usage examples:
  python search_the_web.py "query"
  python search_the_web.py "query" --json -o results.json
  python search_the_web.py "query" --csv
  python search_the_web.py "query" --pageno 2 --safesearch 1

API key must be set in the BRAVE_API_KEY environment variable.
"""

import argparse
import csv
import json
import os
import sys
from pathlib import Path
from typing import Any

import requests

BASE_URL = "https://api.search.brave.com/res/v1/web/search"
COUNT = 20  # Brave max per page
MAX_OFFSET = 9 * COUNT  # Brave limit: offset max 9 (0-indexed page)


def search(
    query: str,
    language: str = "en",
    safesearch: str = "0",
    pageno: int = 1,
    time_range: str = "",
) -> dict[str, Any]:
    """Perform a Brave Web Search request and return the JSON response."""
    api_key = os.environ.get("BRAVE_API_KEY", "")
    if not api_key:
        raise RuntimeError(
            "BRAVE_API_KEY environment variable is not set. "
            "Get a key at https://brave.com/search/api/"
        )

    params: dict[str, str | int] = {
        "q": query,
        "count": COUNT,
    }

    # Language
    if language:
        params["search_lang"] = language

    # SafeSearch mapping: 0=None, 1=Moderate, 2=Strict
    safesearch_map = {"0": "off", "1": "moderate", "2": "strict"}
    if safesearch in safesearch_map:
        params["safesearch"] = safesearch_map[safesearch]

    # Pagination (1-indexed → 0-indexed offset)
    if pageno > 1:
        offset = (pageno - 1) * COUNT
        offset = min(offset, MAX_OFFSET)
        params["offset"] = offset

    # Freshness filtering
    freshness_map = {
        "day": "pd",
        "week": "pw",
        "month": "pm",
        "year": "py",
    }
    if time_range and time_range in freshness_map:
        params["freshness"] = freshness_map[time_range]

    headers = {
        "Accept": "application/json",
        "Accept-Encoding": "gzip",
        "X-Subscription-Token": api_key,
    }

    response = requests.get(BASE_URL, params=params, headers=headers)
    response.raise_for_status()
    return response.json()


def parse_results(data: dict[str, Any]) -> list[dict[str, Any]]:
    """Extract search result items from the Brave API response."""
    web = data.get("web", {})
    raw_results: list[dict[str, Any]] = web.get("results", [])
    results: list[dict[str, Any]] = []
    for r in raw_results:
        results.append({
            "url": r.get("url", ""),
            "title": r.get("title", ""),
            "content": r.get("description", ""),
            "age": r.get("age", ""),
        })
    return results


def more_results_available(data: dict[str, Any]) -> bool:
    """Check if more pages of results are available."""
    return bool(data.get("web", {}).get("more_results_available", False))


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
            writer.writerow(row)
    finally:
        if output:
            out_file.close()


def write_text(results: list[dict[str, Any]], output: str | None) -> None:
    lines: list[str] = []
    for i, r in enumerate(results, 1):
        lines.append(f"{i}. {r['title']}")
        lines.append(f"   URL:     {r['url']}")
        if r.get("age"):
            lines.append(f"   Age:     {r['age']}")
        if r.get("content"):
            lines.append(f"   Snippet: {r['content']}")
        lines.append("")
    text = "\n".join(lines).rstrip() + "\n"
    if output:
        Path(output).write_text(text, encoding="utf-8")
    else:
        print(text, end="")


def main():
    parser = argparse.ArgumentParser(
        description="Search the web using the Brave Search API."
    )
    parser.add_argument("query", help="Search query")
    parser.add_argument(
        "-l", "--language", default="en",
        help="Language code (default: en)",
    )
    parser.add_argument(
        "-s", "--safesearch", default="0",
        choices=["0", "1", "2"],
        help="SafeSearch: 0=None, 1=Moderate, 2=Strict",
    )
    parser.add_argument(
        "-p", "--pageno", type=int, default=1,
        help="Page number (default: 1)",
    )
    parser.add_argument(
        "-t", "--time-range", default="",
        choices=["day", "week", "month", "year"],
        help="Time range filter",
    )
    # --category is accepted for backward compatibility but ignored
    parser.add_argument(
        "-c", "--category", default="general",
        help="(ignored) Search category",
    )
    parser.add_argument("--json", action="store_true", help="Output as JSON")
    parser.add_argument("--csv", action="store_true", help="Output as CSV")
    parser.add_argument("--text", action="store_true",
                        help="Output as plain text (default)")
    parser.add_argument(
        "-o", "--output",
        help="Write output to file instead of stdout",
    )
    args = parser.parse_args()

    if not (args.json or args.csv):
        args.text = True

    try:
        data = search(
            query=args.query,
            language=args.language,
            safesearch=args.safesearch,
            pageno=args.pageno,
            time_range=args.time_range,
        )
    except RuntimeError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    except requests.RequestException as e:
        print(f"Request error: {e}", file=sys.stderr)
        sys.exit(1)

    results = parse_results(data)

    if args.json:
        output_data: dict[str, Any] = {"results": results}
        if more_results_available(data):
            output_data["more_results_available"] = True
            output_data["next_page"] = args.pageno + 1
        write_json(output_data, args.output)
    elif args.csv:
        write_csv(results, args.output)
    else:  # --text (default)
        write_text(results, args.output)


if __name__ == "__main__":
    main()