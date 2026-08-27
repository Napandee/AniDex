const urlInput = document.getElementById('anidex-url');
const tokenInput = document.getElementById('anidex-token');
const saveBtn = document.getElementById('save');
const statusEl = document.getElementById('status');

function showStatus(text, ok) {
  statusEl.textContent = text;
  statusEl.className = ok ? 'ok' : 'err';
}

async function load() {
  const { anidexUrl, anidexToken } = await chrome.storage.local.get(['anidexUrl', 'anidexToken']);
  if (anidexUrl) urlInput.value = anidexUrl;
  if (anidexToken) tokenInput.value = anidexToken;
}

saveBtn.addEventListener('click', async () => {
  const url = urlInput.value.trim().replace(/\/+$/, '');
  const token = tokenInput.value.trim();

  if (!url || !token) {
    showStatus('Both fields are required.', false);
    return;
  }

  let origin;
  try {
    origin = new URL(url).origin;
  } catch {
    showStatus('That doesn’t look like a valid URL.', false);
    return;
  }

  // Every AniDex instance is self-hosted at a different URL — there's no
  // fixed host to declare upfront in the manifest the way a SaaS target
  // (Trakt.tv, etc.) could. optional_host_permissions in manifest.json
  // declares the broad *possibility*; this requests only the one specific
  // origin the user actually entered, at the moment they need it, which is
  // all Chrome will actually grant regardless of how broad the optional
  // bucket declared.
  const granted = await chrome.permissions.request({ origins: [`${origin}/*`] });
  if (!granted) {
    showStatus('Permission to reach that URL was not granted — cannot save without it.', false);
    return;
  }

  await chrome.storage.local.set({ anidexUrl: url, anidexToken: token });
  showStatus('Saved. Syncing now…', true);

  chrome.runtime.sendMessage({ type: 'sync-now' }, (result) => {
    if (result?.ok) {
      showStatus('Saved and synced successfully.', true);
    } else {
      const reason = result?.reason || 'unknown error';
      showStatus(`Saved, but the first sync failed (${reason}). Check the AniDex URL/token and try the toolbar button.`, false);
    }
  });
});

load();
