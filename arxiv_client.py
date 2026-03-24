"""arXiv search + Google Scholar keyword extraction."""
import re
import urllib.parse
import requests
import xml.etree.ElementTree as ET

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) '
        'AppleWebKit/537.36 (KHTML, like Gecko) '
        'Chrome/122.0.0.0 Safari/537.36'
    )
}


# ── Scholar ──────────────────────────────────────────────────────────────────

def fetch_scholar_keywords(scholar_url):
    """Return (interests, paper_titles) from a Scholar profile page."""
    try:
        resp = requests.get(scholar_url, headers=HEADERS, timeout=15)
        html = resp.text

        # Research interest tags
        interests = re.findall(r'class="gs_ibl[^"]*"[^>]*>([^<]+)</a>', html)

        # Paper titles (two common class names Scholar uses)
        titles = re.findall(r'class="gsc_a_at"[^>]*>([^<]+)</a>', html)
        if not titles:
            titles = re.findall(r'class="gsc_rsb_a_ext"[^>]*>([^<]+)</a>', html)

        return interests, titles[:10]
    except Exception as e:
        print(f'[Scholar] fetch failed: {e}')
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

def search_arxiv(keywords, max_results=30):
    query = ' OR '.join('all:"{}"'.format(k) for k in keywords)
    url = (
        'https://export.arxiv.org/api/query'
        '?search_query={}&start=0&max_results={}'
        '&sortBy=submittedDate&sortOrder=descending'
    ).format(urllib.parse.quote(query), max_results)

    print(f'[arXiv] GET {url[:120]}...')
    resp = requests.get(url, timeout=25)
    resp.raise_for_status()
    papers = _parse_xml(resp.text)
    print(f'[arXiv] got {len(papers)} entries')
    return papers


def _parse_xml(xml_text):
    ns = {'a': 'http://www.w3.org/2005/Atom'}
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as e:
        print(f'[arXiv] XML parse error: {e}')
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


def _score(paper, keywords):
    text = (paper['title'] + ' ' + paper['summary']).lower()
    s = 0
    for kw in keywords:
        kl = kw.lower()
        s += text.count(kl)
        if kl in paper['title'].lower():
            s += 5
    return s


# ── Public API ────────────────────────────────────────────────────────────────

def get_recommendations(scholar_url, keywords_str, top_k):
    """Return top-k ranked papers. Also returns the keywords used."""
    keywords = []
    scholar_ok = False

    if scholar_url:
        interests, titles = fetch_scholar_keywords(scholar_url)
        print(f'[Scholar] interests={interests}, titles_count={len(titles)}')
        keywords = build_keywords(interests, titles)
        scholar_ok = bool(keywords)

    if not keywords and keywords_str:
        keywords = [k.strip() for k in keywords_str.split(',') if k.strip()]

    print(f'[Recommend] keywords={keywords}')
    if not keywords:
        return [], []

    papers = search_arxiv(keywords, max_results=max(top_k * 3, 30))
    ranked = sorted(papers, key=lambda p: _score(p, keywords), reverse=True)
    return ranked[:top_k], keywords
