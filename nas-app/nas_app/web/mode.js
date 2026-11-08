'use strict';

// 在首屏渲染前应用配色，避免登录页/主界面先闪一下默认主题。
// 界面风格已收敛为「档案室」单一样式；旧的 nas-ui-mode 键保留在 localStorage 做迁移，
// 这里不再读取旧值，也不主动删除，避免残留值影响样式。
(function () {
  document.documentElement.dataset.uiMode = 'pro';

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
