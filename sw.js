// Service Worker — 离线缓存策略
//
// ⚠️ 分工是有意为之，别把两者换回来：
//   静态资源（页面/图标/echarts）→ 缓存优先，秒开
//   会变的文件（data.json / releases.json）→ **网络优先**，否则永远慢一个版本
// 历史坑（沿用油价项目实测结论）：data.json 若走「预缓存 + 缓存优先」，
// 返回访客每次都会先看到昨天的数据 —— 页面上表现为「今天的新价格不见了」，
// 必须刷第二次才出来。
// 注意 index.html 里的 fetch('./data.json', {cache:'no-cache'}) **绕不过** Cache Storage ——
// no-cache 只作用在 HTTP 缓存上，caches.match() 照样命中。所以必须在 SW 这层改。
//
// ⚠️ 本项目 data.json 约 1 MB（含 20 年日线 + LME 历史），所以**不放进 CORE_ASSETS 预缓存**，
// 只在用户真正访问时按需缓存，避免安装阶段白白下载 1 MB。
//
// v2：新增 releases.json（同为网络优先）；并注意 —— 改动 index.html 后必须升版本号，
// 因为页面本身走缓存优先，不升版本老访客会一直拿到旧页面。
const CACHE_NAME = 'copper-price-tracker-v3';
const CORE_ASSETS = [
  './',
  './index.html',
  './releases.html',
  './manifest.json',
  './favicon.png',
  './icon-192x192.png',
  './icon-512x512.png',
  'https://cdn.jsdelivr.net/npm/echarts@5.5.1/dist/echarts.min.js',
];

// 会变的文件一律走「网络优先」，绝不放预缓存：
//   data.json     —— 每天 08:30 被 Actions 重写
//   releases.json —— 每次发版更新
// 放在这里而不是 CORE_ASSETS，是为了避免安装阶段就下载 1 MB 的 data.json。
const NETWORK_FIRST = ['data.json', 'releases.json'];

self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(CACHE_NAME).then((cache) => {
      // allSettled：单个资源 404（比如还没生成 favicon）不该让整个安装失败
      return Promise.allSettled(
        CORE_ASSETS.map((url) =>
          cache.add(url).catch((err) => console.warn('[SW] Cache miss:', url, err))
        )
      );
    })
  );
  self.skipWaiting();
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(keys.filter((k) => k !== CACHE_NAME).map((k) => caches.delete(k)))
    )
  );
  self.clients.claim();
});

self.addEventListener('fetch', (event) => {
  if (event.request.method !== 'GET') return;

  const url = new URL(event.request.url);

  // ---- 数据文件：网络优先，断网才回退缓存 ----
  if (NETWORK_FIRST.some((f) => url.pathname.endsWith('/' + f))) {
    event.respondWith(
      fetch(event.request)
        .then((response) => {
          if (response && response.ok) {
            const clone = response.clone();
            caches.open(CACHE_NAME).then((cache) => cache.put(event.request, clone));
          }
          return response;
        })
        .catch(() =>
          caches.match(event.request, { ignoreSearch: true }).then(
            (cached) => cached || Response.error()
          )
        )
    );
    return;
  }

  // ---- 其余静态资源：缓存优先，同时后台更新 ----
  event.respondWith(
    caches.match(event.request).then((cached) => {
      const fetchPromise = fetch(event.request)
        .then((response) => {
          if (response.ok) {
            const clone = response.clone();
            caches.open(CACHE_NAME).then((cache) => cache.put(event.request, clone));
          }
          return response;
        })
        .catch(() => null);

      return cached || fetchPromise;
    })
  );
});
