"""Multi-source "latest papers across the web" search.

Each source returns papers in the common ScholarMate schema:

    {
        'title':     str,
        'authors':   [str, ...],
        'summary':   str,
        'link':      str,          # canonical URL (DOI preferred)
        'published': 'YYYY-MM-DD',
        'source':    str,          # human-readable venue/platform for the badge
    }

Sources are queried concurrently and best-effort: any one failing (timeout,
rate-limit, markup change) is logged and skipped so the others still return.
Results are merged, de-duplicated by normalised title, and ranked with the
shared TF-IDF scorer in arxiv_client (so the extension's mirror stays valid).

Why aggregators instead of per-publisher integrations
-----------------------------------------------------
Nature, Science, IEEE, ACM, and the big ML/CV/NLP conferences are all indexed
by OpenAlex / Crossref / Semantic Scholar, so we pull from those and surface the
real venue (e.g. "Nature", "IEEE Trans. …", "NeurIPS 2024") as the `source`
badge — falling back to the aggregator name when a record carries no venue.
Google Scholar has no public API and blocks scraping, so it is not a fetch
source here (the Scholar *profile URL* remains a keyword-extraction input).
"""
import concurrent.futures
import datetime
import logging
import os
import re
import urllib.parse

import requests

from arxiv_client import (build_keywords, fetch_scholar_keywords, rank_papers,
                          search_arxiv)

log = logging.getLogger('scholarmate.sources')

# Polite contact for API usage (OpenAlex/Crossref ask for it; raises rate limits).
CONTACT = os.environ.get('CONTACT_EMAIL', 'scholarmate@users.noreply.github.com')
UA = f'ScholarMate/1.0 (mailto:{CONTACT})'
HTTP_TIMEOUT = 15          # per-source HTTP timeout (seconds)
PER_SOURCE_MAX = 25        # max records to request from each source
# "Latest" window. Bounding the query to a recent range both targets fresh work
# and excludes the bogus far-future publication dates some publishers register,
# which a plain date-desc sort would otherwise surface at the top.
RECENCY_DAYS = int(os.environ.get('RECENCY_DAYS', '365'))


def _date_window():
    today = datetime.date.today()
    start = today - datetime.timedelta(days=RECENCY_DAYS)
    return start.isoformat(), today.isoformat()

# Sources enabled by default; override with the SOURCES env var (comma list),
# e.g. SOURCES="arxiv,openalex,crossref". Names must match the registry below.
DEFAULT_SOURCES = ['arxiv', 'openalex', 'crossref', 'semantic_scholar', 'europepmc']


# ── individual sources ──────────────────────────────────────────────────────────
# Each fetcher returns a list of paper dicts on success, or raises on a hard
# failure (network/HTTP). The aggregator counts raises as "source errored".

def fetch_arxiv(keywords, limit):
    papers = search_arxiv(keywords, max_results=limit)   # has its own retry/backoff
    for p in papers:
        p['source'] = 'arXiv'
    return papers


def fetch_openalex(keywords, limit):
    start, end = _date_window()
    params = {
        'search': ' '.join(keywords),
        'filter': f'from_publication_date:{start},to_publication_date:{end}',
        'sort': 'publication_date:desc',
        'per-page': limit,
        'mailto': CONTACT,
    }
    url = 'https://api.openalex.org/works?' + urllib.parse.urlencode(params)
    resp = requests.get(url, headers={'User-Agent': UA}, timeout=HTTP_TIMEOUT)
    resp.raise_for_status()
    out = []
    for w in resp.json().get('results', []):
        title = (w.get('title') or '').strip()
        if not title:
            continue
        authors = [a['author']['display_name']
                   for a in (w.get('authorships') or [])[:10]
                   if a.get('author', {}).get('display_name')]
        loc = w.get('primary_location') or {}
        venue = (loc.get('source') or {}).get('display_name')
        link = w.get('doi') or (loc.get('landing_page_url') or w.get('id') or '')
        out.append({
            'title': title,
            'authors': authors,
            'summary': _openalex_abstract(w.get('abstract_inverted_index')),
            'link': link,
            'published': (w.get('publication_date') or '')[:10],
            'source': venue or 'OpenAlex',
        })
    return out


def fetch_crossref(keywords, limit):
    start, end = _date_window()
    params = {
        'query': ' '.join(keywords),
        'filter': f'from-pub-date:{start},until-pub-date:{end}',
        'sort': 'published',
        'order': 'desc',
        'rows': limit,
        'select': 'title,author,abstract,URL,container-title,published',
        'mailto': CONTACT,
    }
    url = 'https://api.crossref.org/works?' + urllib.parse.urlencode(params)
    resp = requests.get(url, headers={'User-Agent': UA}, timeout=HTTP_TIMEOUT)
    resp.raise_for_status()
    out = []
    for it in resp.json().get('message', {}).get('items', []):
        title = (it.get('title') or [''])[0].strip()
        if not title:
            continue
        authors = [' '.join(p for p in (a.get('given'), a.get('family')) if p)
                   for a in (it.get('author') or [])[:10]]
        authors = [a for a in authors if a]
        venue = (it.get('container-title') or [None])[0]
        out.append({
            'title': title,
            'authors': authors,
            'summary': _strip_tags(it.get('abstract', '')),
            'link': it.get('URL', ''),
            'published': _crossref_date(it.get('published')),
            'source': venue or 'Crossref',
        })
    return out


def fetch_semantic_scholar(keywords, limit):
    # Bulk search supports date sorting; the relevance endpoint does not.
    start, end = _date_window()
    params = {
        'query': ' '.join(keywords),
        'sort': 'publicationDate:desc',
        'publicationDateOrYear': f'{start}:{end}',
        'fields': 'title,abstract,authors,venue,publicationDate,year,url,externalIds',
    }
    url = ('https://api.semanticscholar.org/graph/v1/paper/search/bulk?'
           + urllib.parse.urlencode(params))
    resp = requests.get(url, headers={'User-Agent': UA}, timeout=HTTP_TIMEOUT)
    resp.raise_for_status()
    out = []
    for p in (resp.json().get('data') or [])[:limit]:
        title = (p.get('title') or '').strip()
        if not title:
            continue
        authors = [a.get('name') for a in (p.get('authors') or [])[:10] if a.get('name')]
        doi = (p.get('externalIds') or {}).get('DOI')
        link = ('https://doi.org/' + doi) if doi else (p.get('url') or '')
        published = p.get('publicationDate') or (
            f"{p['year']}-01-01" if p.get('year') else '')
        out.append({
            'title': title,
            'authors': authors,
            'summary': p.get('abstract') or '',
            'link': link,
            'published': published[:10],
            'source': p.get('venue') or 'Semantic Scholar',
        })
    return out


def fetch_europepmc(keywords, limit):
    start, end = _date_window()
    terms = ' OR '.join('"{}"'.format(k) for k in keywords)
    query = '({}) AND (FIRST_PDATE:[{} TO {}])'.format(terms, start, end)
    params = {
        'query': query,
        'format': 'json',
        'pageSize': limit,
        'sort': 'P_PDATE_D desc',
        'resultType': 'core',
    }
    url = ('https://www.ebi.ac.uk/europepmc/webservices/rest/search?'
           + urllib.parse.urlencode(params))
    resp = requests.get(url, headers={'User-Agent': UA}, timeout=HTTP_TIMEOUT)
    resp.raise_for_status()
    out = []
    for it in resp.json().get('resultList', {}).get('result', []):
        title = (it.get('title') or '').strip()
        if not title:
            continue
        authors = [a.strip() for a in (it.get('authorString') or '').split(',') if a.strip()]
        doi = it.get('doi')
        link = ('https://doi.org/' + doi) if doi else (
            'https://europepmc.org/abstract/{}/{}'.format(
                it.get('source', 'MED'), it.get('id', '')))
        out.append({
            'title': title,
            'authors': authors[:10],
            'summary': it.get('abstractText') or '',
            'link': link,
            'published': (it.get('firstPublicationDate') or '')[:10],
            'source': it.get('journalTitle') or 'Europe PMC',
        })
    return out


SOURCES = {
    'arxiv': fetch_arxiv,
    'openalex': fetch_openalex,
    'crossref': fetch_crossref,
    'semantic_scholar': fetch_semantic_scholar,
    'europepmc': fetch_europepmc,
}


# ── parsing helpers ─────────────────────────────────────────────────────────────

_TAG_RE = re.compile(r'<[^>]+>')


def _strip_tags(text):
    """Crossref abstracts are JATS XML; strip tags to plain text."""
    if not text:
        return ''
    return re.sub(r'\s+', ' ', _TAG_RE.sub(' ', text)).strip()


def _openalex_abstract(inverted_index):
    """Reconstruct an abstract from OpenAlex's inverted-index representation."""
    if not inverted_index:
        return ''
    positions = []
    for word, idxs in inverted_index.items():
        for i in idxs:
            positions.append((i, word))
    positions.sort()
    return ' '.join(w for _i, w in positions)[:2000]


def _crossref_date(published):
    """Crossref 'published.date-parts' -> 'YYYY-MM-DD' (zero-padded)."""
    if not published:
        return ''
    parts = (published.get('date-parts') or [[]])[0]
    if not parts:
        return ''
    y = parts[0]
    m = parts[1] if len(parts) > 1 else 1
    d = parts[2] if len(parts) > 2 else 1
    return '{:04d}-{:02d}-{:02d}'.format(y, m, d)


# ── aggregation ─────────────────────────────────────────────────────────────────

def enabled_sources():
    raw = os.environ.get('SOURCES')
    names = [s.strip() for s in raw.split(',')] if raw else DEFAULT_SOURCES
    return [(n, SOURCES[n]) for n in names if n in SOURCES]


_NORM_RE = re.compile(r'[^a-z0-9]+')


def _title_key(title):
    return _NORM_RE.sub(' ', (title or '').lower()).strip()


def _dedupe(papers):
    """Collapse the same paper seen on multiple platforms.

    Keeps the richest record (longest abstract) but appends the other platforms'
    names to its source badge, so a paper on both arXiv and Nature reads
    "Nature · arXiv".
    """
    by_key = {}
    order = []
    for p in papers:
        key = _title_key(p['title'])
        if not key:
            continue
        if key not in by_key:
            by_key[key] = p
            order.append(key)
            continue
        kept = by_key[key]
        # merge source labels (preserve order, no dupes)
        srcs = [s for s in (kept.get('source'), p.get('source')) if s]
        merged = []
        for s in ' · '.join(srcs).split(' · '):
            if s and s not in merged:
                merged.append(s)
        winner = p if len(p.get('summary', '')) > len(kept.get('summary', '')) else kept
        winner = dict(winner)
        winner['source'] = ' · '.join(merged[:3])
        by_key[key] = winner
    return [by_key[k] for k in order]


def get_recommendations(scholar_url, keywords_str, top_k):
    """Return (top_k ranked papers across all sources, keywords used).

    Raises RuntimeError only if *every* enabled source errored, so the caller
    (app.py) can fall back to the user's cached results.
    """
    keywords = _derive_keywords(scholar_url, keywords_str)
    log.info('recommend with keywords=%s', keywords)
    if not keywords:
        return [], []

    limit = min(max(top_k * 3, 30), 100)
    sources = enabled_sources()
    if not sources:
        return [], keywords

    results, errors = [], []
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(sources)) as ex:
        futures = {ex.submit(fn, keywords, PER_SOURCE_MAX): name
                   for name, fn in sources}
        for fut in concurrent.futures.as_completed(futures):
            name = futures[fut]
            try:
                got = fut.result() or []
                log.info('source %s returned %d', name, len(got))
                results.extend(got)
            except Exception as e:
                errors.append((name, e))
                log.warning('source %s failed: %s: %s', name, e.__class__.__name__, e)

    # If we got nothing AND at least one source errored, surface it so the caller
    # serves the cached fallback instead of an empty "no results".
    if not results and errors:
        name = errors[0][1].__class__.__name__
        raise RuntimeError(
            f'Could not reach paper sources ({name}). Please try again.')

    today = datetime.date.today().isoformat()
    results = [p for p in results if not p['published'] or p['published'] <= today]
    deduped = _dedupe(results)
    ranked = rank_papers(deduped, keywords)
    return ranked[:top_k], keywords


def _derive_keywords(scholar_url, keywords_str):
    keywords = []
    if scholar_url:
        interests, titles = fetch_scholar_keywords(scholar_url)
        log.info('Scholar interests=%s titles=%d', interests, len(titles))
        keywords = build_keywords(interests, titles)
    if not keywords and keywords_str:
        keywords = [k.strip() for k in keywords_str.split(',') if k.strip()]
    return keywords
