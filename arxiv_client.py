"""arXiv search + Google Scholar keyword extraction + TF-IDF ranking.

Ranking note
------------
The TF-IDF ranking in `rank_papers` is intentionally MIRRORED in the browser
extension (background.js → rankPapers). If you change the formula, scoring
constants, or tokenisation here, update background.js too so the web app and
the extension produce consistent results.
"""
import logging
import math
import re
import time
import urllib.parse
import xml.etree.ElementTree as ET

import requests

log = logging.getLogger('scholarmate.arxiv')

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) '
        'AppleWebKit/537.36 (KHTML, like Gecko) '
        'Chrome/122.0.0.0 Safari/537.36'
    )
}

# arXiv's API guidelines ask for a descriptive User-Agent identifying the app
# (and ideally a contact). A bare/browser-spoofing agent is throttled harder.
ARXIV_HEADERS = {
    'User-Agent': 'ScholarMate/1.0 (https://github.com/; mailto:gushangding@gmail.com)'
}


# ── Scholar ──────────────────────────────────────────────────────────────────

def fetch_scholar_keywords(scholar_url):
    """Return (interests, paper_titles) from a Scholar profile page.

    Best-effort: any failure (timeout, block, captcha, markup change) returns
    empty lists so the caller can fall back to manual keywords.
    """
    try:
        resp = requests.get(scholar_url, headers=HEADERS, timeout=10)
        html = resp.text

        # Research interest tags
        interests = re.findall(r'class="gs_ibl[^"]*"[^>]*>([^<]+)</a>', html)

        # Paper titles (two common class names Scholar uses)
        titles = re.findall(r'class="gsc_a_at"[^>]*>([^<]+)</a>', html)
        if not titles:
            titles = re.findall(r'class="gsc_rsb_a_ext"[^>]*>([^<]+)</a>', html)

        return interests, titles[:10]
    except Exception as e:
        log.warning('Scholar fetch failed: %s', e)
        return [], []


def build_keywords(interests, titles):
    """Combine explicit interests with frequent words from paper titles."""
    keywords = list(interests)
    word_freq = {}
    for title in titles:
        for w in title.split():
            w = w.strip('.,()[]{}:;-_')
            if len(w) > 4 and w.isalpha():
                word_freq[w] = word_freq.get(w, 0) + 1
    top_words = sorted(word_freq, key=lambda k: word_freq[k], reverse=True)[:5]
    seen = set()
    result = []
    for kw in keywords + top_words:
        if kw.lower() not in seen:
            seen.add(kw.lower())
            result.append(kw)
    return result


# ── arXiv ─────────────────────────────────────────────────────────────────────

# arXiv throttles bursts and asks clients to back off on 429/503 and honour
# the Retry-After header. We retry transient errors with exponential backoff.
_RETRY_STATUSES = (429, 503)
_MAX_ATTEMPTS = 4
_BASE_BACKOFF = 3.0  # seconds; arXiv's API guideline is ~3s between requests


def search_arxiv(keywords, max_results=30):
    query = ' OR '.join('all:"{}"'.format(k) for k in keywords)
    url = (
        'https://export.arxiv.org/api/query'
        '?search_query={}&start=0&max_results={}'
        '&sortBy=submittedDate&sortOrder=descending'
    ).format(urllib.parse.quote(query), max_results)

    log.info('arXiv GET %s...', url[:120])
    last_exc = None
    for attempt in range(1, _MAX_ATTEMPTS + 1):
        try:
            resp = requests.get(url, headers=ARXIV_HEADERS, timeout=20)
            if resp.status_code in _RETRY_STATUSES and attempt < _MAX_ATTEMPTS:
                wait = _retry_after_seconds(resp, attempt)
                log.warning('arXiv %s (attempt %d/%d), retrying in %.1fs',
                            resp.status_code, attempt, _MAX_ATTEMPTS, wait)
                time.sleep(wait)
                continue
            resp.raise_for_status()
            papers = _parse_xml(resp.text)
            log.info('arXiv returned %d entries', len(papers))
            return papers
        except requests.exceptions.Timeout as e:
            last_exc = e
            if attempt < _MAX_ATTEMPTS:
                wait = _BASE_BACKOFF * (2 ** (attempt - 1))
                log.warning('arXiv timeout (attempt %d/%d), retrying in %.1fs',
                            attempt, _MAX_ATTEMPTS, wait)
                time.sleep(wait)
                continue
            raise RuntimeError('arXiv took too long to respond. Please try again in a moment.')
        except requests.exceptions.RequestException as e:
            last_exc = e
            if attempt < _MAX_ATTEMPTS:
                wait = _BASE_BACKOFF * (2 ** (attempt - 1))
                log.warning('arXiv error %s (attempt %d/%d), retrying in %.1fs',
                            e.__class__.__name__, attempt, _MAX_ATTEMPTS, wait)
                time.sleep(wait)
                continue
            raise RuntimeError(
                f'Could not reach arXiv ({e.__class__.__name__}). Please try again.')

    # Exhausted retries (e.g. repeated 429/503).
    name = last_exc.__class__.__name__ if last_exc else 'HTTPError'
    raise RuntimeError(f'arXiv is busy right now ({name}). Please try again in a moment.')


def _retry_after_seconds(resp, attempt):
    """Seconds to wait before retrying, honouring Retry-After when present."""
    header = resp.headers.get('Retry-After')
    if header:
        try:
            return max(0.0, float(header))
        except ValueError:
            pass  # HTTP-date form is uncommon for arXiv; fall through to backoff
    return _BASE_BACKOFF * (2 ** (attempt - 1))


def _parse_xml(xml_text):
    ns = {'a': 'http://www.w3.org/2005/Atom'}
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as e:
        log.warning('arXiv XML parse error: %s', e)
        return []

    papers = []
    for entry in root.findall('a:entry', ns):
        title = (entry.findtext('a:title', namespaces=ns) or '').strip()
        if not title:
            continue

        # Compatible with Python 3.7 (no walrus operator)
        authors = []
        for a in entry.findall('a:author', ns):
            name_el = a.find('a:name', ns)
            if name_el is not None and name_el.text:
                authors.append(name_el.text)

        papers.append({
            'title':     title,
            'authors':   authors,
            'summary':   (entry.findtext('a:summary', namespaces=ns) or '').strip(),
            'link':      (entry.findtext('a:id',      namespaces=ns) or '').strip(),
            'published': (entry.findtext('a:published', namespaces=ns) or '')[:10],
        })
    return papers


# ── TF-IDF ranking ──────────────────────────────────────────────────────────────
# MIRRORED in background.js (rankPapers). Keep both implementations in sync.

_TOKEN_RE = re.compile(r'[a-z0-9]+')


def _tokens(text):
    return _TOKEN_RE.findall(text.lower())


def rank_papers(papers, keywords):
    """Rank papers by summed TF-IDF over the keyword set, with a title bonus.

    For each keyword phrase:
      tf  = (occurrences in title+abstract) / (token count of that doc)
      idf = ln((N + 1) / (1 + df)) + 1     # smoothed; df = docs containing the phrase
    score = Σ tf*idf over keywords, plus 0.5*idf when the phrase appears in the
    title. Rarer keywords (low df across the result set) thus weigh more than
    ones that match nearly everything, which plain count-based scoring missed.
    """
    kws = [k.lower() for k in keywords if k and k.strip()]
    if not papers or not kws:
        return list(papers)

    n_docs = len(papers)
    docs = []  # (title_lower, full_lower, token_count)
    for p in papers:
        title_l = p['title'].lower()
        full_l = (p['title'] + ' ' + p.get('summary', '')).lower()
        docs.append((title_l, full_l, max(1, len(_tokens(full_l)))))

    # document frequency per keyword (substring match across the corpus)
    df = {kw: sum(1 for (_t, full, _n) in docs if kw in full) for kw in kws}

    scored = []
    for paper, (title_l, full_l, ntok) in zip(papers, docs):
        score = 0.0
        for kw in kws:
            idf = math.log((n_docs + 1) / (1 + df[kw])) + 1.0
            tf = full_l.count(kw) / ntok
            score += tf * idf
            if kw in title_l:
                score += 0.5 * idf       # title hit bonus, scaled by rarity
        scored.append((score, paper))

    scored.sort(key=lambda sp: sp[0], reverse=True)
    return [p for _s, p in scored]


# ── Public API ────────────────────────────────────────────────────────────────

def get_recommendations(scholar_url, keywords_str, top_k):
    """Return (top_k ranked papers, keywords used)."""
    keywords = []

    if scholar_url:
        interests, titles = fetch_scholar_keywords(scholar_url)
        log.info('Scholar interests=%s titles=%d', interests, len(titles))
        keywords = build_keywords(interests, titles)

    if not keywords and keywords_str:
        keywords = [k.strip() for k in keywords_str.split(',') if k.strip()]

    log.info('recommend with keywords=%s', keywords)
    if not keywords:
        return [], []

    papers = search_arxiv(keywords, max_results=max(top_k * 3, 30))
    ranked = rank_papers(papers, keywords)
    return ranked[:top_k], keywords
