// Applies the saved theme before the first paint (no flash). "auto" follows prefers-color-scheme.
(function () {
  try {
    var t = JSON.parse(localStorage.getItem('automate.theme') || '"auto"');
    if (t === 'light' || t === 'dark') document.documentElement.setAttribute('data-theme', t);
  } catch (e) { /* storage blocked: follow the system */ }
})();
