// Global toast helper
(function() {
  const el = document.createElement('div');
  el.id = 'toast';
  document.body.appendChild(el);

  let timer;
  window.showToast = function(msg, ok) {
    el.textContent = msg;
    el.className = 'show ' + (ok ? 'ok' : 'fail');
    clearTimeout(timer);
    timer = setTimeout(() => { el.className = ''; }, 2500);
  };
})();
