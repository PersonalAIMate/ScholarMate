// Background service worker
// Handles scheduled alarms and cached results

chrome.alarms.onAlarm.addListener(async (alarm) => {
  if (alarm.name === 'paper_recommendation') {
    const config = await getConfig();
    if (!config.email) return;
    const papers = await fetchRecommendations(config);
    if (papers.length > 0) {
      await chrome.storage.local.set({
        cachedPapers: papers,
        cacheTime: Date.now()
      });
    }
  }
});

// Setup or clear alarm when storage changes
chrome.storage.onChanged.addListener((changes) => {
  if (changes.schedule || changes.email) {
    setupAlarm();
  }
});

async function setupAlarm() {
  await chrome.alarms.clear('paper_recommendation');
  const config = await getConfig();
  if (!config.email || config.schedule === 'manual') return;

  const periodInMinutes = config.schedule === 'daily' ? 1440 : 10080; // daily or weekly
  chrome.alarms.create('paper_recommendation', {
    delayInMinutes: periodInMinutes,
    periodInMinutes: periodInMinutes
  });
}

async function getConfig() {
  return new Promise((resolve) => {
    chrome.storage.sync.get(
      { email: '', scholarUrl: '', keywords: '', topK: 10, schedule: 'manual' },
      resolve
    );
  });
}

// ──────────────────────────────────────────────
// Scholar profile parsing (regex-based, no DOM)
// ──────────────────────────────────────────────
async function fetchScholarKeywords(scholarUrl) {
  try {
    const resp = await fetch(scholarUrl, {
      headers: { 'User-Agent': 'Mozilla/5.0' }
    });
    if (!resp.ok) return null;
    const html = await resp.text();

    // Extract research interest tags  e.g. <a class="gs_ibl" ...>Machine Learning</a>
    const interests = [];
    const interestRe = /class="gs_ibl[^"]*"[^>]*>([^<]+)<\/a>/g;
    let m;
    while ((m = interestRe.exec(html)) !== null) {
      const t = m[1].trim();
      if (t) interests.push(t);
    }

    // Extract paper titles for additional keywords
    const titles = [];
    const titleRe = /class="gsc_a_at"[^>]*>([^<]+)<\/a>/g;
    while ((m = titleRe.exec(html)) !== null) {
      const t = m[1].trim();
      if (t) titles.push(t);
    }

    return { interests, titles: titles.slice(0, 10) };
  } catch (e) {
    console.error('Scholar fetch failed:', e);
    return null;
  }
}

// ──────────────────────────────────────────────
// arXiv API
// ──────────────────────────────────────────────
async function searchArxiv(keywords, maxResults = 20) {
  const query = keywords
    .map((k) => `all:"${k}"`)
    .join(' OR ');
  const url =
    `https://export.arxiv.org/api/query?` +
    `search_query=${encodeURIComponent(query)}` +
    `&start=0&max_results=${maxResults}` +
    `&sortBy=submittedDate&sortOrder=descending`;

  const resp = await fetch(url);
  if (!resp.ok) throw new Error('arXiv API error');
  const xml = await resp.text();
  return parseArxivXML(xml);
}

function parseArxivXML(xml) {
  const entries = [];
  const entryRe = /<entry>([\s\S]*?)<\/entry>/g;
  let m;
  while ((m = entryRe.exec(xml)) !== null) {
    const block = m[1];
    const get = (tag) => {
      const r = new RegExp(`<${tag}[^>]*>([\\s\\S]*?)<\\/${tag}>`);
      const res = r.exec(block);
      return res ? res[1].trim().replace(/\s+/g, ' ') : '';
    };
    const authors = [];
    const authRe = /<name>([\s\S]*?)<\/name>/g;
    let am;
    while ((am = authRe.exec(block)) !== null) authors.push(am[1].trim());

    const title = get('title');
    if (title) {
      entries.push({
        title,
        authors,
        summary: get('summary'),
        link: get('id'),
        published: get('published').substring(0, 10)
      });
    }
  }
  return entries;
}

// ──────────────────────────────────────────────
// Ranking: score each article by keyword overlap
// ──────────────────────────────────────────────
function rankPapers(papers, keywords) {
  return papers
    .map((p) => {
      const text = (p.title + ' ' + p.summary).toLowerCase();
      let score = 0;
      for (const kw of keywords) {
        const kwl = kw.toLowerCase();
        const count = (text.match(new RegExp(kwl.replace(/[.*+?^${}()|[\]\\]/g, '\\$&'), 'g')) || []).length;
        score += count;
        if (p.title.toLowerCase().includes(kwl)) score += 5; // title bonus
      }
      return { ...p, score };
    })
    .sort((a, b) => b.score - a.score);
}

// ──────────────────────────────────────────────
// Main recommendation pipeline (used by popup too)
// ──────────────────────────────────────────────
async function fetchRecommendations(config) {
  let keywords = [];

  // 1. Try Google Scholar
  if (config.scholarUrl) {
    const data = await fetchScholarKeywords(config.scholarUrl);
    if (data) {
      keywords = [...data.interests];
      // Add frequent single-word nouns from paper titles
      const titleWords = data.titles.join(' ').split(/\s+/)
        .filter((w) => w.length > 4)
        .reduce((acc, w) => { acc[w] = (acc[w] || 0) + 1; return acc; }, {});
      const topWords = Object.entries(titleWords)
        .sort((a, b) => b[1] - a[1])
        .slice(0, 5)
        .map(([w]) => w);
      keywords = [...new Set([...keywords, ...topWords])];
    }
  }

  // 2. Fallback to manual keywords
  if (keywords.length === 0 && config.keywords) {
    keywords = config.keywords.split(',').map((k) => k.trim()).filter(Boolean);
  }

  if (keywords.length === 0) return [];

  // 3. Fetch and rank
  const papers = await searchArxiv(keywords, Math.max(config.topK * 3, 30));
  const ranked = rankPapers(papers, keywords);
  return ranked.slice(0, config.topK);
}

// Expose to popup via message passing
chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
  if (msg.action === 'fetchPapers') {
    fetchRecommendations(msg.config)
      .then((papers) => sendResponse({ ok: true, papers }))
      .catch((e) => sendResponse({ ok: false, error: e.message }));
    return true; // keep channel open for async
  }
  if (msg.action === 'setupAlarm') {
    setupAlarm().then(() => sendResponse({ ok: true }));
    return true;
  }
});
