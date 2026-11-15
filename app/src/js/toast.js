// A short message at the foot of the screen; level 'error' shows it as one
export function showToast(message, level) {
  const el = document.createElement('div');
  el.className = `toast ${level || ''}`;
  el.textContent = message;
  const container = document.getElementById('toasts');
  if (!container) return;
  while (container.children.length >= 5) container.removeChild(container.firstChild);
  container.appendChild(el);
  el.addEventListener('animationend', () => el.remove());
}
