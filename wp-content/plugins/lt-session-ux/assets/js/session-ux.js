/**
 * LaTestata.it – UX sessione scaduta
 * - Intercetta errori 401/403 (nonce/sessione scaduta) su fetch/wp.apiFetch
 * - Mostra un modale chiaro e non distruttivo
 * - Offre login e ritorno alla stessa pagina
 * - Prova il ri-salvataggio "bozza" al rientro (best-effort, non invasivo)
 */
(function () {
  'use strict';

  var cfg = (typeof window.LT_SESSION_UX !== 'undefined') ? window.LT_SESSION_UX : {};
  var hasShown = false;

  function el(tag, attrs, children) {
    var e = document.createElement(tag);
    if (attrs) {
      Object.keys(attrs).forEach(function (k) {
        if (k === 'class') e.className = attrs[k];
        else if (k === 'style') Object.assign(e.style, attrs[k]);
        else if (k === 'html') e.innerHTML = attrs[k];
        else e.setAttribute(k, attrs[k]);
      });
    }
    (children || []).forEach(function (c) { e.appendChild(c); });
    return e;
  }

  function showModal() {
    if (hasShown) return;
    hasShown = true;
    try {
      var overlay = el('div', { class: 'ltuxs-overlay', role: 'dialog', 'aria-modal': 'true' });
      var box = el('div', { class: 'ltuxs-box' });
      var title = el('h3', { class: 'ltuxs-title', html: (cfg.copy && cfg.copy.title) || 'Sessione scaduta' });
      var msg = el('p', { class: 'ltuxs-msg', html: (cfg.copy && cfg.copy.message) || 'La tua sessione è scaduta. Accedi di nuovo.' });
      var actions = el('div', { class: 'ltuxs-actions' });
      var primary = el('a', { class: 'ltuxs-btn ltuxs-primary', href: cfg.reloginUrl || '/wp-login.php' }, [document.createTextNode((cfg.copy && cfg.copy.primary) || 'Accedi di nuovo')]);
      var secondary = el('button', { class: 'ltuxs-btn ltuxs-secondary', type: 'button' }, [document.createTextNode((cfg.copy && cfg.copy.secondary) || 'Resta qui')]);

      // Se l'utente sceglie di accedere, ricordiamo di ritentare la bozza
      primary.addEventListener('click', function () {
        try { sessionStorage.setItem('lt-retry-draft', '1'); } catch (e) {}
      });
      secondary.addEventListener('click', function () {
        try { document.body.removeChild(overlay); } catch (e) {}
      });

      actions.appendChild(primary);
      actions.appendChild(secondary);

      box.appendChild(title);
      box.appendChild(msg);
      box.appendChild(actions);
      overlay.appendChild(box);
      document.body.appendChild(overlay);
    } catch (e) {
      // Non bloccare il flusso.
    }
  }

  function isNonceExpiredResponse(resp, bodyText) {
    if (!resp) return false;
    if (resp.status === 401 || resp.status === 403) return true;
    // Body euristico: errori tipici WP REST / messaggi custom.
    var t = (bodyText || '').toLowerCase();
    if (!t && resp.headers && resp.headers.get('content-type') && resp.headers.get('content-type').indexOf('application/json') !== -1) {
      // Non possiamo leggere body due volte. Usiamo euristica solo su testo già fornito.
      return false;
    }
    return t.indexOf('nonce') !== -1 || t.indexOf('sessione wordpress scaduta') !== -1 || t.indexOf('rest_cookie_invalid_nonce') !== -1;
  }

  // Patch fetch globale (best-effort, senza alterare il valore di ritorno).
  if (typeof window.fetch === 'function') {
    var _fetch = window.fetch;
    window.fetch = function () {
      return _fetch.apply(window, arguments).then(function (resp) {
        try {
          var ct = (resp.headers && resp.headers.get('content-type')) || '';
          if (ct.indexOf('application/json') !== -1 && typeof resp.clone === 'function') {
            // Leggiamo una copia per non consumare lo stream.
            resp.clone().text().then(function (txt) {
              if (isNonceExpiredResponse(resp, txt)) {
                showModal();
              }
            }).catch(function(){ /* ignore */});
          } else if (resp.status === 401 || resp.status === 403) {
            showModal();
          }
        } catch (e) {}
        return resp;
      }).catch(function (err) {
        // In caso di rete down, non mostriamo il modale "sessione scaduta".
        throw err;
      });
    };
  }

  // Se è disponibile wp.apiFetch, agganciamo un middleware.
  if (window.wp && wp.apiFetch && typeof wp.apiFetch.use === 'function') {
    wp.apiFetch.use(function (options, next) {
      // Imposta nonce se fornito dal backend.
      if (cfg.restNonce) {
        options = options || {};
        options.headers = options.headers || {};
        if (!options.headers['X-WP-Nonce']) {
          options.headers['X-WP-Nonce'] = cfg.restNonce;
        }
      }
      return next(options).catch(function (err) {
        try {
          var status = err && (err.status || (err.data && err.data.status));
          var code = err && (err.code || (err.data && err.data.code));
          if (status === 401 || status === 403 || code === 'rest_cookie_invalid_nonce') {
            showModal();
          }
        } catch (e) {}
        throw err;
      });
    });
  }

  // Ping periodico per anticipare la scadenza.
  function heartbeat() {
    if (!cfg.restUrl || !cfg.restNonce) return;
    var url = (cfg.restUrl.replace(/\\/$/, '')) + '/lt/v1/ping';
    fetch(url, {
      method: 'GET',
      credentials: 'include',
      headers: { 'X-WP-Nonce': cfg.restNonce }
    }).then(function (r) {
      if (r.status === 401 || r.status === 403) showModal();
    }).catch(function () {
      // rete down: nessun modale "sessione", non sappiamo discriminare.
    });
  }
  if (cfg.pingIntervalSec && Number(cfg.pingIntervalSec) > 0) {
    setInterval(heartbeat, Number(cfg.pingIntervalSec) * 1000);
    // Primo check dopo breve attesa, utile se la pagina resta aperta a lungo.
    setTimeout(heartbeat, 5000);
  }
})(); 

