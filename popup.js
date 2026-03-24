// popup.js – runs in the popup window context

let currentPapers = [];
let currentConfig = {};

// ── Helpers ──────────────────────────────────────────────
function $(id) { return document.getElementById(id); }

function openOptions() {
  chrome.runtime.openOptionsPage();
}

function showError(msg) {
  $('error').textContent = msg;
  $('error').classList.remove('hidden');
  $('loading').classList.add('hidden');
}

function hideError() {
  $('error').classList.add('hidden');
}

function setLoading(on) {
  $('loading').classList.toggle('hidden', !on);
  $('findBtn').disabled = on;
}

// ── Render papers ─────────────────────────────────────────
function renderPapers(papers) {
  const container = $('results');
  if (papers.length === 0) {
    container.innerHTML = '<div class="empty">No papers found. Try different keywords.</div>';
    $('emailBtn').classList.add('hidden');
    return;
  }

  container.innerHTML = papers.map((p, i) => {
    const authors = p.authors.slice(0, 3).join(', ') + (p.authors.length > 3 ? ' et al.' : '');
    const abstract = p.summary ? p.summary.substring(0, 220) + '…' : '';
    const link = p.link.replace('http://', 'https://');
    return `
      <div class="paper-card">
        <div class="paper-title">
          <a href="${link}" target="_blank">${i + 1}. ${escapeHtml(p.title)}</a>
        </div>
        <div class="paper-meta">${escapeHtml(authors)} · ${p.published || ''}</div>
        ${abstract ? `<div class="paper-abstract">${escapeHtml(abstract)}</div>` : ''}
      </div>`;
  }).join('');

  $('emailBtn').classList.remove('hidden');
}

function escapeHtml(str) {
  return (str || '').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}

// ── Email via mailto ──────────────────────────────────────
function sendEmail() {
  if (!currentConfig.email || currentPapers.length === 0) return;

  const subject = `Paper Recommendations – ${new Date().toLocaleDateString()}`;
  const body = currentPapers.map((p, i) => {
    const authors = p.authors.slice(0, 3).join(', ');
    const link = p.link.replace('http://', 'https://');
    const snippet = (p.summary || '').substring(0, 300);
    return `${i + 1}. ${p.title}\nAuthors: ${authors}\nLink: ${link}\n${snippet}…\n`;
  }).join('\n─────────────────────────────\n\n');

  const mailto = `mailto:${currentConfig.email}?subject=${encodeURIComponent(subject)}&body=${encodeURIComponent(body)}`;
  // mailto URIs can be very long – open in current tab to avoid truncation issues
  window.open(mailto, '_self');
}

// ── Fetch papers via background ───────────────────────────
async function fetchPapers() {
  hideError();
  setLoading(true);
  $('results').innerHTML = '';
  $('emailBtn').classList.add('hidden');

  chrome.runtime.sendMessage(
    { action: 'fetchPapers', config: currentConfig },
    (resp) => {
      setLoading(false);
      if (chrome.runtime.lastError || !resp) {
        showError('Extension error: ' + (chrome.runtime.lastError?.message || 'unknown'));
        return;
      }
      if (!resp.ok) {
        showError('Error: ' + resp.error);
        return;
      }
      currentPapers = resp.papers;
      // cache in local storage
      chrome.storage.local.set({ cachedPapers: currentPapers, cacheTime: Date.now() });
      renderPapers(currentPapers);
    }
  );
}

// ── Init ──────────────────────────────────────────────────
async function init() {
  chrome.storage.sync.get(
    { email: '', scholarUrl: '', keywords: '', topK: 10, schedule: 'manual' },
    (config) => {
      currentConfig = config;

      const hasSource = config.scholarUrl || config.keywords;
      if (!config.email || !hasSource) {
        $('configWarning').classList.remove('hidden');
        $('mainSection').classList.add('hidden');
        return;
      }

      $('configWarning').classList.add('hidden');
      $('mainSection').classList.remove('hidden');

      // Info bar
      $('sourceInfo').textContent = config.scholarUrl ? '🎓 Scholar' : '🔑 Keywords';
      $('topkInfo').textContent = `Top ${config.topK}`;
      $('scheduleInfo').textContent = config.schedule === 'manual' ? '⏱ Manual' :
        config.schedule === 'daily' ? '📅 Daily' : '📅 Weekly';

      // Try to show cached results immediately
      chrome.storage.local.get(['cachedPapers', 'cacheTime'], (local) => {
        if (local.cachedPapers && local.cachedPapers.length > 0) {
          currentPapers = local.cachedPapers;
          renderPapers(currentPapers);
          // Show age hint
          if (local.cacheTime) {
            const mins = Math.round((Date.now() - local.cacheTime) / 60000);
            const ageEl = document.createElement('div');
            ageEl.style.cssText = 'font-size:11px;color:#aaa;text-align:center;padding:4px';
            ageEl.textContent = `Cached ${mins < 2 ? 'just now' : mins + 'm ago'} – click Find to refresh`;
            $('results').prepend(ageEl);
          }
        }
      });
    }
  );
}

// ── Event listeners ───────────────────────────────────────
$('settingsLink').addEventListener('click', openOptions);
$('configLink')?.addEventListener('click', openOptions);
$('findBtn').addEventListener('click', fetchPapers);
$('emailBtn').addEventListener('click', sendEmail);

init();
