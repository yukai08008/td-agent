---
name: alex-serp
description: Use this skill when the user needs to search Baidu and retrieve SERP-style results. Triggers on requests like "百度搜索", "搜索一下", "查一下百度", "Baidu search", "SERP search", or any task requiring current Chinese web search results. Provides structured search result data (title, description, link) via the SERP API.
---

# Alex SERP API

## Overview

Retrieve Baidu search result data via a dedicated SERP API. The API returns structured results with `title`, `description`, `link`, and `count` fields, enabling programmatic access to current Baidu SERP data.

## When To Use

- Finding web pages related to a keyword on Baidu
- Checking Chinese search result snippets
- Collecting search result titles and links
- Comparing visible SERP results for a query

Do NOT use this skill for general web crawling, page scraping, or full webpage extraction. It only returns search result summaries.

## How To Use

### API Endpoint

```http
POST http://106.75.97.247:24656/serp
```

### Request Format

Send JSON with a `query` field:

```json
{
  "query": "python"
}
```

The `query` field is required, must be non-empty, and no longer than 500 characters.

### Response Format

Successful response (status `200`):

```json
{
  "results": [
    {
      "title": "Welcome to Python.org",
      "description": "The official home of the Python Programming Language.",
      "link": "https://www.python.org/"
    }
  ],
  "count": 1
}
```

### Error Handling

| Status | Meaning | Suggested Action |
|--------|---------|-----------------|
| `200` | Success | Use `results`. |
| `400` | Invalid query | Fix the request. |
| `429` | Browser pool temporarily busy | Retry after a short delay. |
| `503` | SERP attempt failed after retries | Retry with backoff. |

### Retry Strategy

- Use a client timeout of at least **60 seconds**.
- For `429` or `503`, retry up to 2 times with backoff: wait 1s, then 2s.
- Do NOT retry `400`; fix the query instead.

### Usage Workflow

1. Send `POST /serp` with the user query.
2. If status is `200`, summarize the returned `results` — include titles and links.
3. If status is `429` or `503`, retry up to 2 times with backoff.
4. If still failing, explain that the SERP backend could not get a stable result.

### cURL Example

```bash
curl -s -X POST "http://106.75.97.247:24656/serp" \
  -H "Content-Type: application/json" \
  -d '{"query":"python"}'
```

## Result Quality Notes

- Results can vary between calls for the same query due to Baidu's different proxy exits.
- Sometimes fewer than 10 results may be returned (ads, cards, variant layouts).
- For repeated identical queries, cache successful responses briefly (30-120 seconds TTL).
