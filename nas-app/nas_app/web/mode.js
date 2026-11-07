'use strict';

// 在首屏渲染前应用界面风格与配色，避免登录页/主界面先闪一下默认主题。
(function () {
  var mode = 'friendly';
  try {
    mode = window.localStorage.getItem('nas-ui-mode') === 'pro' ? 'pro' : 'friendly';
  } catch (_) {
    // 隐私模式下 localStorage 可能不可用，回退到友好版。
  }
  document.documentElement.dataset.uiMode = mode;

  var theme = 'auto';
  try {
    theme = window.localStorage.getItem('nas-color-theme') || 'auto';
  } catch (_) {
    // 隐私模式下忽略。
  }
  if (theme !== 'light' && theme !== 'dark') theme = 'auto';
  var dark = theme === 'dark'
    || (theme === 'auto' && window.matchMedia
      && window.matchMedia('(prefers-color-scheme: dark)').matches);
  document.documentElement.dataset.theme = dark ? 'dark' : 'light';
})();
