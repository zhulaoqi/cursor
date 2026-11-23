/*
 * 平台切换菜单（Cursor / Devin / …）。
 * 新增平台：在 CAM_PLATFORMS 末尾追加一项（从左到右依次展示），再新增对应页面与 /api/<key>/* 即可。
 * 各页面只需放一个 <nav data-platform-nav data-active="devin"></nav> 并引入本脚本与 platforms.css。
 */
(function () {
  var ICONS = {
    // lucide: mouse-pointer-2
    cursor: '<path d="M4.037 4.688a.495.495 0 0 1 .651-.651l16 6.5a.5.5 0 0 1-.063.947l-6.124 1.58a2 2 0 0 0-1.438 1.435l-1.579 6.126a.5.5 0 0 1-.947.063z"/>',
    // lucide: bot
    devin: '<path d="M12 8V4H8"/><rect width="16" height="12" x="4" y="8" rx="2"/><path d="M2 14h2"/><path d="M20 14h2"/><path d="M15 13v2"/><path d="M9 13v2"/>'
  };

  window.CAM_PLATFORMS = [
    { key: 'cursor', label: 'Cursor', href: '/' },
    { key: 'devin', label: 'Devin', href: '/devin' }
    // { key: 'xxx', label: 'XXX', href: '/xxx' },   ← 以后来一个加一个
  ];

  function render(nav) {
    var active = nav.getAttribute('data-active') || '';
    nav.classList.add('platform-nav');
    nav.setAttribute('aria-label', '平台切换');
    nav.innerHTML = '';
    window.CAM_PLATFORMS.forEach(function (p) {
      var a = document.createElement('a');
      a.className = 'platform-nav-item' + (p.key === active ? ' is-active' : '');
      a.href = p.href;
      if (p.key === active) a.setAttribute('aria-current', 'page');
      var icon = ICONS[p.key] || '<circle cx="12" cy="12" r="9"/>';
      a.innerHTML =
        '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" ' +
        'stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' + icon + '</svg>' +
        '<span></span>';
      a.querySelector('span').textContent = p.label;
      nav.appendChild(a);
    });
  }

  function init() {
    document.querySelectorAll('[data-platform-nav]').forEach(render);
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
  else init();
})();
