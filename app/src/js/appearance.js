// Appearance: theme, density and type, chosen in Settings and kept on this device.
// A plain script loaded in <head>, before the page draws, so it never flashes the
// wrong colours. It sets <html data-theme data-density data-font>; css/themes.css
// and css/app.css do the rest. The settings dialog uses window.LeStashAppearance.
(function () {
  var KEY = 'lestash-appearance';
  var THEMES = [
    ['auto', 'Automatic (Nord, light or dark as the system is)'],
    ['nord', 'Nord'], ['nord-light', 'Nord Light'],
    ['catppuccin', 'Catppuccin Mocha'], ['catppuccin-latte', 'Catppuccin Latte'],
    ['rose-pine', 'Rosé Pine'], ['rose-pine-dawn', 'Rosé Pine Dawn'],
    ['gruvbox', 'Gruvbox'], ['gruvbox-light', 'Gruvbox Light'],
    ['tokyo-night', 'Tokyo Night'],
    ['solarized', 'Solarized'], ['solarized-light', 'Solarized Light'],
    ['classic', 'LeStash classic'],
  ];
  var DENSITIES = [['compact', 'Compact'], ['comfortable', 'Comfortable']];
  var FONTS = [['sans', 'Sans-serif'], ['serif', 'Serif for reading'], ['mono', 'Monospace']];
  var DEFAULTS = { theme: 'nord', density: 'compact', font: 'sans' };
  var known = function (list, v) { return list.some(function (o) { return o[0] === v; }); };
  var light = window.matchMedia ? window.matchMedia('(prefers-color-scheme: light)') : null;

  function load() {
    var s = {};
    try { s = JSON.parse(localStorage.getItem(KEY) || '{}') || {}; } catch (e) { s = {}; }
    return {
      theme: known(THEMES, s.theme) ? s.theme : DEFAULTS.theme,
      density: known(DENSITIES, s.density) ? s.density : DEFAULTS.density,
      font: known(FONTS, s.font) ? s.font : DEFAULTS.font,
    };
  }

  function apply(s) {
    var root = document.documentElement;
    var theme = s.theme === 'auto' ? (light && light.matches ? 'nord-light' : 'nord') : s.theme;
    root.dataset.theme = theme;
    root.dataset.density = s.density;
    root.dataset.font = s.font;
  }

  function save(change) {
    var s = load();
    for (var k in change) if (Object.prototype.hasOwnProperty.call(change, k)) s[k] = change[k];
    try { localStorage.setItem(KEY, JSON.stringify(s)); } catch (e) { /* private mode */ }
    apply(load());
  }

  apply(load());
  // Automatic follows the system as it changes
  if (light) {
    var follow = function () { if (load().theme === 'auto') apply(load()); };
    if (light.addEventListener) light.addEventListener('change', follow);
    else if (light.addListener) light.addListener(follow);
  }

  window.LeStashAppearance = {
    THEMES: THEMES, DENSITIES: DENSITIES, FONTS: FONTS, load: load, save: save,
  };
})();
