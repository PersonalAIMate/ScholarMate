// options.js

function $(id) { return document.getElementById(id); }

// Load saved settings into the form
chrome.storage.sync.get(
  { email: '', scholarUrl: '', keywords: '', topK: 10, schedule: 'manual' },
  (config) => {
    $('email').value       = config.email;
    $('scholarUrl').value  = config.scholarUrl;
    $('keywords').value    = config.keywords;
    $('topK').value        = config.topK;
    $('schedule').value    = config.schedule;
  }
);

// Save settings
$('saveBtn').addEventListener('click', () => {
  const email      = $('email').value.trim();
  const scholarUrl = $('scholarUrl').value.trim();
  const keywords   = $('keywords').value.trim();
  const topK       = Math.max(1, Math.min(50, parseInt($('topK').value) || 10));
  const schedule   = $('schedule').value;

  if (!email) {
    alert('Please enter your email address.');
    $('email').focus();
    return;
  }
  if (!scholarUrl && !keywords) {
    alert('Please enter either a Google Scholar URL or some keywords.');
    return;
  }

  chrome.storage.sync.set({ email, scholarUrl, keywords, topK, schedule }, () => {
    // Tell background to update alarm
    chrome.runtime.sendMessage({ action: 'setupAlarm' });

    // Flash save confirmation
    $('saveMsg').classList.remove('hidden');
    setTimeout(() => $('saveMsg').classList.add('hidden'), 2500);
  });
});
