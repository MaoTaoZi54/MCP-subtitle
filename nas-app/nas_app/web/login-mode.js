'use strict';

// 登录页只负责同步浏览器主题色；界面风格已统一为「档案室」，不再提供风格切换。
(function () {
  function sync() {
    var root = document.documentElement;
    var meta = document.getElementById('theme-color');
    if (meta) {
      meta.setAttribute('content', root.dataset.theme === 'dark' ? '#17150f' : '#f4f1ea');
    }
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', sync);
  } else {
    sync();
  }
})();
