// API Config — 統一 API Base URL 管理
// 所有 fetch() 必須經此 Config，禁止寫死 localhost

(function() {
  const hostname = window.location.hostname;
  const port = window.location.port;

  // Production: 同一個 domain
  const BASE = window.location.protocol + '//' + hostname + (port ? ':' + port : '');

  window.API_CONFIG = {
    // Johnny AI Platform（自身）
    JOHNNY_API_BASE: BASE,

    // Agent Platform（同 server 另一個 port 或同 domain 代理）
    // Cloudflare Tunnel: 同一個 domain，唔同 port 用 separate tunnel
    // Local: 同 hostname, port 3001
    // EC2: 同 hostname, port 3001
    // 預設自動偵測：如果 hostname 係 localhost/127.0.0.1，用 port 3001
    // 否則嘗試同 domain 嘅 /api/agent/ 代理（Nginx reverse proxy）
    AGENT_API_BASE: (function() {
      // Always use Johnny proxy for same-origin (no CORS issues)
      return BASE + '/api/agent';
    })(),

    // Browser Core（同 server）
    // Local: port 3000
    // Production: 經 Agent Platform 代理
    BROWSER_CORE_BASE: (function() {
      if (hostname === 'localhost' || hostname === '127.0.0.1') {
        return 'http://' + hostname + ':3000';
      }
      return BASE + '/api/browser';
    })(),
  };

  console.log('[API Config]', window.API_CONFIG);
})();