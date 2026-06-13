/* Static data layer — rebinds apiFetch so the dashboard runs from baked
   data files instead of the Flask API. Works from file:// or GitHub Pages.
   Regenerate the data with:  python export_static.py */
(() => {
  const KEYS = window.GP_STORE_KEYS || [];
  const SYNC_MSG = 'Prices refresh automatically once a day — no manual sync needed here.';
  const pending = {};
  const cache = {};

  function loadScript(key) {
    if (window.GP_DATA && window.GP_DATA[key]) return Promise.resolve();
    if (!pending[key]) {
      pending[key] = new Promise((res, rej) => {
        const s = document.createElement('script');
        s.src = 'data/' + key + '.js';
        s.onload = res;
        s.onerror = rej;
        document.head.appendChild(s);
      });
    }
    return pending[key];
  }

  const unpack = t => t.rows.map(r => Object.fromEntries(t.cols.map((c, i) => [c, r[i]])));

  async function store(key) {
    await loadScript(key);
    if (!cache[key]) {
      const d = window.GP_DATA[key];
      const products = unpack(d.products);
      const hist = {};
      unpack(d.history).forEach(h => (hist[h.product_id] = hist[h.product_id] || []).push(h));
      Object.values(hist).forEach(a => a.sort((x, y) => x.scraped_at < y.scraped_at ? -1 : 1));
      cache[key] = {products, hist, runs: d.runs,
                    byId: Object.fromEntries(products.map(p => [p.product_id, p]))};
    }
    return cache[key];
  }

  function settings() {
    try {
      const s = JSON.parse(localStorage.gp_static_store);
      if (KEYS.includes(s.retailer + '_' + s.store_id)) return s;
    } catch (e) {}
    return window.GP_DEFAULT_STORE;
  }
  const curKey = () => { const s = settings(); return s.retailer + '_' + s.store_id; };

  function alerts() {
    try { return JSON.parse(localStorage.gp_static_alerts) || []; } catch (e) { return []; }
  }
  const saveAlerts = a => localStorage.gp_static_alerts = JSON.stringify(a);

  apiFetch = async function (url, opts = {}) {
    const u = new URL(url, 'http://x');
    const p = u.pathname;
    const q = u.searchParams;
    const body = opts.body ? JSON.parse(opts.body) : {};
    try {
      if (p === '/api/stores') return window.GP_STORES;

      if (p === '/api/store' && (opts.method || 'GET') === 'GET') {
        return {...settings(), has_data: true};
      }
      if (p === '/api/store') {  // POST: switch store
        const k = (body.retailer || 'saveon') + '_' + body.store_id;
        if (!KEYS.includes(k)) return {error: 'This store isn’t tracked yet — only stores with price data can be selected'};
        localStorage.gp_static_store = JSON.stringify(
          {store_id: body.store_id, retailer: body.retailer || 'saveon',
           store_name: body.store_name || body.store_id});
        return {status: 'ok', store_id: body.store_id, has_data: true};
      }

      if (p === '/api/scrape') return {error: SYNC_MSG};
      if (p === '/api/scrape/status') return {running: false, message: ''};
      if (p === '/api/runs') return (await store(curKey())).runs;

      if (p === '/api/alerts' && (opts.method || 'GET') === 'GET') {
        const d = await store(curKey());
        return alerts().map(a => {
          const pr = d.byId[a.product_id] || {};
          return {...a, name: pr.name, brand: pr.brand, image_url: pr.image_url,
                  current_price: pr.latest_price};
        });
      }
      if (p === '/api/alerts') {  // POST
        const a = alerts();
        a.unshift({id: Date.now(), product_id: body.product_id,
                   target_price: +body.target_price,
                   created_at: new Date().toISOString(), triggered: 0});
        saveAlerts(a);
        return {status: 'created'};
      }
      let m = p.match(/^\/api\/alerts\/(\d+)$/);
      if (m) { saveAlerts(alerts().filter(a => a.id !== +m[1])); return {status: 'deleted'}; }

      const d = await store(curKey());

      if (p === '/api/stats') {
        const ps = d.products;
        const cats = new Set(ps.filter(x => x.category).map(x => x.category));
        const priced = ps.filter(x => x.latest_price != null);
        const lr = d.runs[0];
        return {total_products: ps.length,
                on_sale: ps.filter(x => x.on_sale).length,
                categories: cats.size,
                avg_price: priced.length ? +(priced.reduce((s, x) => s + x.latest_price, 0) / priced.length).toFixed(2) : null,
                last_run: lr ? {completed_at: lr.completed_at, products_scraped: lr.products_scraped, status: lr.status} : null};
      }

      if (p === '/api/categories') {
        const counts = {};
        d.products.forEach(x => { if (x.category) counts[x.category] = (counts[x.category] || 0) + 1; });
        return Object.entries(counts).map(([category, count]) => ({category, count}))
          .sort((a, b) => b.count - a.count);
      }

      if (p === '/api/products') {
        const search = (q.get('search') || '').toLowerCase();
        const cat = q.get('category') || '';
        const onSale = q.get('on_sale') === 'true';
        const page = +(q.get('page') || 1), per = +(q.get('per_page') || 48);
        let items = d.products.filter(x =>
          (!search || (x.name || '').toLowerCase().includes(search) ||
                      (x.brand || '').toLowerCase().includes(search)) &&
          (!cat || x.category === cat) && (!onSale || x.on_sale));
        const sorters = {
          name: (a, b) => (a.name || '').localeCompare(b.name || ''),
          price_asc: (a, b) => (a.latest_price ?? 1e9) - (b.latest_price ?? 1e9),
          price_desc: (a, b) => (b.latest_price ?? -1) - (a.latest_price ?? -1),
          newest: (a, b) => (b.created_at || '').localeCompare(a.created_at || ''),
        };
        items.sort(sorters[q.get('sort')] || sorters.name);
        const total = items.length;
        items = items.slice((page - 1) * per, page * per)
          .map(x => ({...x, last_seen: x.latest_at}));
        return {products: items, total, page, per_page: per};
      }

      if (p === '/api/sales') {
        const page = +(q.get('page') || 1), per = +(q.get('per_page') || 48);
        const all = d.products.filter(x => x.on_sale)
          .map(x => ({...x, price: x.latest_price, last_seen: x.latest_at,
                      discount_pct: x.regular_price
                        ? +((x.regular_price - x.latest_price) / x.regular_price * 100).toFixed(1) : null}))
          .sort((a, b) => (b.discount_pct ?? -1) - (a.discount_pct ?? -1));
        return {sales: all.slice((page - 1) * per, page * per),
                total: all.length, page, per_page: per};
      }

      if (p === '/api/price-drops') {
        return d.products.filter(x => x.prev_price > x.latest_price)
          .map(x => ({...x, current_price: x.latest_price,
                      drop_pct: +((x.prev_price - x.latest_price) / x.prev_price * 100).toFixed(1)}))
          .sort((a, b) => b.drop_pct - a.drop_pct)
          .slice(0, +(q.get('limit') || 10));
      }

      m = p.match(/^\/api\/products\/([^/]+)$/);
      if (m) return d.byId[decodeURIComponent(m[1])] || {error: 'Not found'};

      m = p.match(/^\/api\/products\/([^/]+)\/history$/);
      if (m) return d.hist[decodeURIComponent(m[1])] || [];

      m = p.match(/^\/api\/products\/([^/]+)\/live$/);
      if (m) return {description: '', ingredients: '', nutrition: [], promotions: []};

      return {};
    } catch (e) {
      console.error('static api:', p, e);
      return {};
    }
  };

  // make the Sync button explain itself instead of pretending to scrape
  document.addEventListener('DOMContentLoaded', () => {
    const b = document.getElementById('scrape-btn');
    if (b) { b.onclick = () => alert(SYNC_MSG); }
  });
})();
