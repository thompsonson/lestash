// The item panel (#268 themes): an item opened from a list slides up over it as a
// sheet on a phone, and opens as a panel on the right on a wider screen. The list
// stays where it was. Back, Escape, the backdrop or a swipe down on the sheet's top
// closes it. legacy.js's showOnly() decides when a page opens in the sheet.

let closeSheet = () => history.back();

export function onSheetClose(fn) { closeSheet = fn; }

export function sheetOpen() {
  return document.body.classList.contains('sheet-open');
}

// The panel starts below the header and the places bar, wherever they end
function placeTop() {
  const main = document.querySelector('main');
  if (main) document.body.style.setProperty('--chrome-top', `${Math.round(main.getBoundingClientRect().top)}px`);
}

export function setSheet(open) {
  const was = sheetOpen();
  document.body.classList.toggle('sheet-open', open);
  const view = document.getElementById('item-view');
  if (view) {
    view.setAttribute('role', open ? 'dialog' : 'region');
    view.setAttribute('aria-label', 'Item');
    if (open && !was) {
      view.classList.remove('sheet-enter');
      void view.offsetWidth;  // restart the slide
      view.classList.add('sheet-enter');
    }
  }
  if (open) placeTop();
}

function wire() {
  const view = document.getElementById('item-view');
  document.getElementById('sheet-backdrop')?.addEventListener('click', () => closeSheet());
  window.addEventListener('resize', () => { if (sheetOpen()) placeTop(); });
  document.addEventListener('keydown', e => {
    if (e.key !== 'Escape' || !sheetOpen()) return;
    if (document.querySelector('.modal-overlay.open')) return;  // Escape is the dialog's
    // Escape while typing (an edit, a tag) belongs to the field, not the sheet
    if (e.target.closest && e.target.closest('input, textarea, select, [contenteditable="true"]')) return;
    e.preventDefault();
    closeSheet();
  });
  // Swipe down from the sheet's top, when it is scrolled to the top, closes it
  let startY = null;
  view.addEventListener('touchstart', e => {
    startY = sheetOpen() && view.scrollTop <= 0 && e.touches.length === 1 ? e.touches[0].clientY : null;
  }, { passive: true });
  view.addEventListener('touchmove', e => {
    if (startY === null) return;
    const dy = e.touches[0].clientY - startY;
    view.style.transform = dy > 0 ? `translateY(${dy}px)` : '';
  }, { passive: true });
  view.addEventListener('touchend', e => {
    if (startY === null) return;
    const dy = (e.changedTouches[0]?.clientY ?? startY) - startY;
    startY = null;
    view.style.transform = '';
    if (dy > 90) closeSheet();
  });
}

if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', wire);
else wire();
