# arXiv retrieval guidance

Verified against arXiv's official documentation on 2026-10-26:
- API terms: https://info.arxiv.org/help/api/tou.html
- API manual: https://info.arxiv.org/help/api/user-manual.html
- Automated access: https://info.arxiv.org/help/robots.html

The legacy API limit is one request per three seconds, with only one connection at a time, shared across all machines controlled by the same user. This includes the legacy search API, OAI-PMH and RSS. It is NOT a separate allowance for each helper, process, project or server. Follow current endpoint-specific guidance for website/HTML/PDF access too; API permission does not authorize crawling the website.

Coordinate retrieval before delegating: use one downloader and let other helpers work on already-retrieved sources. Do not launch multiple curl/Python/WebFetch downloaders against arXiv in parallel. A three-second sleep in each parallel worker does not implement the shared limit. If other lab work might be retrieving simultaneously and you cannot coordinate safely, reuse existing material or report the need for coordinated retrieval rather than increasing traffic. The platform currently supplies these instructions, not a network-wide enforced throttle for arbitrary CLI tools.

For any direct API client you write, serialize requests and leave at least three seconds between them, including retries. Reuse one connection/client and close responses correctly. Cache successful responses and downloaded source material locally, record the source URL/version/retrieval time, and share retrieved files within the assignment so helpers do not fetch duplicates. Check the shared paper library and existing project material first. Batch known IDs where appropriate. Avoid repeatedly refreshing an unchanged search within a day; this is our conservative caching policy, not a claim that every arXiv endpoint has an identical freshness guarantee.

On HTTP 429 or 503, honor Retry-After if supplied and use bounded exponential backoff with an initial delay of at least three seconds (e.g. 3, 6, 12, 24 seconds). This is our retry policy, not a guarantee that those statuses always mean rate limiting. Keep delays within the session deadline; persistent errors should produce a checkpoint and an honest access limitation. Do not switch hosts, proxies or machines to bypass a restriction, and do not treat an unavailable paper as read.

The search API returns metadata, not full PDF content. It needs no API key. Its manual documents up to 30,000 results in slices of at most 2,000; prefer narrow queries and small useful result sets. For genuinely large metadata harvesting use the documented OAI-PMH interface, still respecting its rate limit. High-volume full-text access belongs on arXiv's documented bulk-access route, not a parallel PDF scraper. Ordinary literature review should retrieve only the papers needed for the research question.

Research can run in parallel after sources are available. The constraint is on retrieval traffic, not the number of agents that may analyze papers or code. Preserve exact sources, read depth, uncertainty and inaccessible sections in the final report.
