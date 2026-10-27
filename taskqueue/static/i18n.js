(function() {
  var _catalog = {};
  var _en = {};
  var _locale = 'en';

  function fetchCatalog(locale) {
    if (!/^[a-z]{2,3}(-[A-Za-z0-9]+)?$/.test(locale)) return Promise.resolve({});
    return fetch('/static/locales/' + locale + '.json')
      .then(function(r) { return r.ok ? r.json() : {}; })
      .catch(function() { return {}; });
  }

  // English is always loaded first so a missing locale (or a missing key) falls back to English, never to the raw key.
  function load(locale) {
    return fetchCatalog('en').then(function(en) {
      _en = en;
      return locale === 'en' ? en : fetchCatalog(locale);
    }).then(function(data) {
      var found = data && Object.keys(data).length > 0;
      _catalog = found ? data : _en;
      _locale = found ? locale : 'en';
      applyDOM();
    });
  }

  function t(key, vars) {
    var s = _catalog[key] || _en[key] || key;
    if (vars) Object.keys(vars).forEach(function(k) {
      var v = String(vars[k]);
      s = s.replace(new RegExp('\\{' + k + '\\}', 'g'), function() { return v; });
    });
    return s;
  }

  function applyDOM() {
    if (typeof window.tqUpdatePageTitle === 'function') {
      window.tqUpdatePageTitle();
    } else if (document.title) {
      document.title = t('app.title');
    }
    document.documentElement.lang = _locale || 'en';
    document.documentElement.dir = (_locale === 'ar') ? 'rtl' : 'ltr';
    document.querySelectorAll('[data-i18n]').forEach(function(el) {
      var key = el.getAttribute('data-i18n');
      if (el.getAttribute('data-i18n-attr')) {
        el.setAttribute(el.getAttribute('data-i18n-attr'), t(key));
      } else {
        el.textContent = t(key);
      }
    });
    document.querySelectorAll('[data-i18n-title]').forEach(function(el) {
      el.title = t(el.getAttribute('data-i18n-title'));
    });
    document.querySelectorAll('[data-i18n-placeholder]').forEach(function(el) {
      el.placeholder = t(el.getAttribute('data-i18n-placeholder'));
    });
  }

  var stored = 'en';
  try { stored = localStorage.getItem('tq_lang') || navigator.language.split('-')[0] || 'en'; } catch(e) {}

  window.i18n = { t: t, load: load, applyDOM: applyDOM, get locale() { return _locale; } };

  load(stored);
})();
