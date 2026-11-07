'use strict';

// 登录页的界面风格切换：与主界面共用 localStorage 键，未登录也能先选风格。
(function () {
  var KEY = 'nas-ui-mode';

  function apply(mode, persist) {
    var root = document.documentElement;
    var next = mode === 'pro' ? 'pro' : 'friendly';
    root.dataset.uiMode = next;
    if (persist) {
      try {
        window.localStorage.setItem(KEY, next);
      } catch (_) {
        // 隐私模式下忽略。
      }
    }
    var buttons = document.querySelectorAll('#login-mode-switch button');
    for (var i = 0; i < buttons.length; i += 1) {
      buttons[i].setAttribute('aria-pressed', String(buttons[i].dataset.mode === next));
    }
    var theme = document.getElementById('theme-color');
    if (theme) {
      // 配色由 mode.js 提前写入 data-theme，这里只同步浏览器主题色。
      var dark = root.dataset.theme === 'dark';
      theme.setAttribute('content', dark ? '#0e1219' : (next === 'pro' ? '#eef2f7' : '#fbf4ee'));
    }
  }

  function boot() {
    apply(document.documentElement.dataset.uiMode === 'pro' ? 'pro' : 'friendly', false);
    var buttons = document.querySelectorAll('#login-mode-switch button');
    for (var i = 0; i < buttons.length; i += 1) {
      (function (btn) {
        btn.addEventListener('click', function () {
          apply(btn.dataset.mode, true);
        });
      })(buttons[i]);
    }
  }

  // 脚本在 <head> 中加载，需要等 DOM 就绪后再绑定按钮。
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', boot);
  } else {
    boot();
  }
})();
