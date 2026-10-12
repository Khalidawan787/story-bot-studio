"""Make the status page installable as a phone app (a PWA).

Writes the app manifest, two icons and a small service worker next to the
status page. On Android, Chrome offers "Install app"; on iPhone, Safari's
Share > "Add to Home Screen". The app opens full screen, without the browser
bar, and shows the last loaded status when the phone is offline.
"""
from __future__ import annotations

import json
from pathlib import Path

NAME = "Story Bot Studio"

HEAD_TAGS = """
<link rel="manifest" href="manifest.webmanifest">
<meta name="theme-color" content="#0d1020">
<link rel="icon" href="icon-192.png">
<link rel="apple-touch-icon" href="icon-192.png">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<meta name="apple-mobile-web-app-title" content="Story Bot">
"""

# Shown at the top of the page: an Install button where the browser allows it,
# and the one-line iPhone instruction there, since Safari has no button.
INSTALL_BAR = """
<div id="install-bar" hidden style="max-width:900px;margin:0 auto 14px;padding:12px 14px;border-radius:12px;
  background:#16224a;border:1px solid #2c3d78;display:flex;gap:12px;align-items:center;justify-content:space-between;flex-wrap:wrap">
  <span id="install-text">📲 Install Story Bot as an app on this phone</span>
  <button id="install-btn" style="background:#4f7dff;color:#fff;border:0;border-radius:10px;padding:9px 14px;font-weight:600">Install</button>
</div>
<script>
(function () {
  if ('serviceWorker' in navigator) navigator.serviceWorker.register('sw.js').catch(function () {});
  var bar = document.getElementById('install-bar'), btn = document.getElementById('install-btn'), prompt = null;
  var standalone = window.matchMedia('(display-mode: standalone)').matches || navigator.standalone;
  if (standalone) return;
  window.addEventListener('beforeinstallprompt', function (e) {
    e.preventDefault(); prompt = e; bar.hidden = false;
  });
  btn.addEventListener('click', function () {
    if (!prompt) return;
    prompt.prompt(); prompt.userChoice.finally(function () { bar.hidden = true; prompt = null; });
  });
  if (/iphone|ipad|ipod/i.test(navigator.userAgent)) {
    document.getElementById('install-text').textContent = '📲 iPhone: tap Share, then “Add to Home Screen”';
    btn.hidden = true; bar.hidden = false;
  }
})();
</script>
"""

# Network first so the status is always fresh; the saved copy only when offline.
SERVICE_WORKER = """
const CACHE = 'storybot-v1';
self.addEventListener('install', e => self.skipWaiting());
self.addEventListener('activate', e => e.waitUntil(self.clients.claim()));
self.addEventListener('fetch', e => {
  if (e.request.method !== 'GET' || new URL(e.request.url).origin !== location.origin) return;
  e.respondWith(
    fetch(e.request).then(r => {
      const copy = r.clone();
      caches.open(CACHE).then(c => c.put(e.request, copy));
      return r;
    }).catch(() => caches.match(e.request))
  );
});
"""


def _icon(size: int) -> bytes:
    import cv2
    import numpy as np

    y, x = np.mgrid[0:size, 0:size].astype(np.float32) / size
    t = (x * 0.6 + y * 0.4)[..., None]
    img = ((1 - t) * np.array([140, 70, 20]) + t * np.array([230, 110, 120])).astype(np.uint8)
    s = size / 1024
    cv2.circle(img, (size // 2, size // 2), int(330 * s), (255, 255, 255), max(2, int(18 * s)), cv2.LINE_AA)
    pts = (np.array([[440, 360], [440, 664], [690, 512]]) * s).astype(np.int32)
    cv2.fillPoly(img, [pts], (255, 255, 255), cv2.LINE_AA)
    return cv2.imencode(".png", img)[1].tobytes()


def write(out_dir: Path) -> None:
    (out_dir / "icon-192.png").write_bytes(_icon(192))
    (out_dir / "icon-512.png").write_bytes(_icon(512))
    (out_dir / "sw.js").write_text(SERVICE_WORKER.strip() + "\n", encoding="utf-8")
    (out_dir / "manifest.webmanifest").write_text(json.dumps({
        "name": NAME,
        "short_name": "Story Bot",
        "description": "Daily videos: what was made, uploaded and scheduled.",
        "start_url": "./",
        "scope": "./",
        "display": "standalone",
        "background_color": "#0d1020",
        "theme_color": "#0d1020",
        "icons": [
            {"src": "icon-192.png", "sizes": "192x192", "type": "image/png", "purpose": "any maskable"},
            {"src": "icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "any maskable"},
        ],
    }, indent=2), encoding="utf-8")
