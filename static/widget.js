/* MEA Voice call widget.
 * Embed on any page:
 *   <script src="https://YOUR-HOST/static/widget.js" data-client="your-slug"></script>
 * Renders a floating "Call us" button that opens the client's call page in a
 * popup. The page carries its own call key, so the snippet holds no secret. */
(function () {
  var me = document.currentScript;
  if (!me) return;
  var slug = me.getAttribute('data-client');
  if (!slug) { console.warn('[mea-voice] data-client missing on widget script'); return; }
  var label = me.getAttribute('data-label') || 'Call us';
  var origin = new URL(me.src, location.href).origin;
  var url = origin + '/call/' + encodeURIComponent(slug);

  var btn = document.createElement('button');
  btn.type = 'button';
  btn.textContent = label;
  btn.setAttribute('aria-label', label + ' (voice call)');
  btn.style.cssText = [
    'position:fixed', 'right:20px', 'bottom:20px', 'z-index:2147483000',
    'padding:14px 20px', 'border:0', 'border-radius:999px', 'cursor:pointer',
    'font:600 15px/1 system-ui,-apple-system,Segoe UI,Roboto,sans-serif',
    'color:#fff', 'background:' + (me.getAttribute('data-color') || '#0f766e'),
    'box-shadow:0 8px 24px rgba(0,0,0,.18)'
  ].join(';');
  btn.addEventListener('click', function () {
    var w = window.open(url, 'mea-voice-call', 'width=420,height=720,menubar=no,toolbar=no,location=no,status=no');
    if (!w) location.href = url;   // popup blocked: open in the same tab
  });
  document.body.appendChild(btn);
})();
