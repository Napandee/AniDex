// AniDex — Prime Video Cookie Sync (issue #390)
//
// Only job of this extension: keep AniDex's stored Prime Video cookie current.
// Nothing here fetches Prime Video's watch history, matches titles against
// AniList, or creates library entries — that all stays exactly as
// scripts/sync_primevideo.py already does on the AniDex server side. This
// extension only automates what used to be a manual copy-paste into Settings.
//
// Why this exists at all: Amazon's Prime Video web session has (at least) two
// tiers — ordinary browsing/playback survives a long time, but the
// "account settings" tier the watch-history API sits behind expires much
// faster, independent of anything AniDex's request shape does (confirmed via
// universal-trakt-scrobbler's own shipping source, see the AniDex repo's
// issue #390 for the full writeup). A cookie captured once and pasted into
// Settings goes stale as soon as that tier rotates; reading the browser's
// live cookie jar on each sync is always current for as long as the browser
// itself is still logged in — same principle every other Prime Video
// scrobbler tool relies on, none of them found a way around needing this.

const ALARM_NAME = 'primevideo-cookie-sync';
const ALARM_PERIOD_MINUTES = 60;

async function getConfig() {
  const { anidexUrl, anidexToken } = await chrome.storage.local.get(['anidexUrl', 'anidexToken']);
  return { anidexUrl, anidexToken };
}

function setBadge(text, color) {
  chrome.action.setBadgeText({ text });
  if (color) chrome.action.setBadgeBackgroundColor({ color });
}

async function syncCookie() {
  const { anidexUrl, anidexToken } = await getConfig();
  if (!anidexUrl || !anidexToken) {
    setBadge('!', '#c9a227');
    console.warn('[AniDex PV cookie sync] Not configured — open the extension options page.');
    return { ok: false, reason: 'not_configured' };
  }

  const cookies = await chrome.cookies.getAll({ domain: 'primevideo.com' });
  if (!cookies.length) {
    setBadge('?', '#c9a227');
    console.warn('[AniDex PV cookie sync] No primevideo.com cookies found — are you logged into Prime Video in this browser?');
    return { ok: false, reason: 'no_cookies' };
  }

  // Same "name=value; name=value" shape sync_primevideo.py already expects
  // (it replays this as a literal Cookie header) — see AniDex's
  // notes/2026-08-14-netflix-prime-sync-research.md for the confirmed shape.
  const cookieHeader = cookies.map((c) => `${c.name}=${c.value}`).join('; ');

  let resp;
  try {
    resp = await fetch(`${anidexUrl.replace(/\/+$/, '')}/api/pat/primevideo-cookie`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        Authorization: `Bearer ${anidexToken}`,
      },
      body: JSON.stringify({ cookie_header: cookieHeader }),
    });
  } catch (e) {
    setBadge('✗', '#c0392b');
    console.error('[AniDex PV cookie sync] Could not reach AniDex instance:', e);
    return { ok: false, reason: 'unreachable', error: String(e) };
  }

  if (!resp.ok) {
    setBadge('✗', '#c0392b');
    const body = await resp.text().catch(() => '');
    console.error(`[AniDex PV cookie sync] AniDex rejected the cookie (HTTP ${resp.status}):`, body);
    return { ok: false, reason: 'rejected', status: resp.status, body };
  }

  setBadge('✓', '#2e7d32');
  // Clear the success badge after a bit so it doesn't look stale/permanent —
  // failures stay visible until the next successful sync, successes don't
  // need to.
  setTimeout(() => chrome.action.setBadgeText({ text: '' }), 15000);
  console.log('[AniDex PV cookie sync] Synced successfully.');
  return { ok: true };
}

chrome.runtime.onInstalled.addListener(() => {
  chrome.alarms.create(ALARM_NAME, { periodInMinutes: ALARM_PERIOD_MINUTES });
});

chrome.alarms.onAlarm.addListener((alarm) => {
  if (alarm.name === ALARM_NAME) syncCookie();
});

chrome.action.onClicked.addListener(() => {
  syncCookie();
});

// Lets options.js trigger an immediate sync right after saving config, and
// get a real success/failure result back to show inline rather than relying
// on the badge alone.
chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
  if (message?.type === 'sync-now') {
    syncCookie().then(sendResponse);
    return true; // keep the message channel open for the async response
  }
});
