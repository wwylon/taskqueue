// Applies the saved theme/font before first paint. Classic script in <head> (a CSP forbids inline scripts).
(function () {
  try {
    var pref = localStorage.getItem('tq_theme') || 'dark';
    var f = localStorage.getItem('tq_font') || 'normal';
    if (f !== 'large' && f !== 'xlarge') f = 'normal';
    var resolved = pref;
    if (pref === 'auto') {
      resolved = window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';
    } else if (pref !== 'light') {
      pref = 'dark';
      resolved = 'dark';
    }
    document.documentElement.setAttribute('data-theme', resolved === 'light' ? 'light' : 'dark');
    document.documentElement.setAttribute('data-theme-pref', pref);
    document.documentElement.setAttribute('data-font', f);
  } catch (e) {
    document.documentElement.setAttribute('data-theme', 'dark');
    document.documentElement.setAttribute('data-theme-pref', 'dark');
    document.documentElement.setAttribute('data-font', 'normal');
  }
})();
