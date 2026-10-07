// Single source of truth for the backend URL, shared by both portals and the
// login page. If you start uvicorn on a different port, change it HERE only.
// Must match the origin the backend allows in FRONTEND_ORIGINS (.env).
// Local dev (Live Server / http.server) talks to local uvicorn; anything else
// (the deployed site) talks to Render.
const API_BASE = ["localhost", "127.0.0.1"].includes(location.hostname)
  ? "http://127.0.0.1:8000"
  : "https://hr-onboarding-bazm.onrender.com";

// Enter on a dropdown doesn't submit its form natively (it does from a text
// box). Make it submit inside pop-up forms (Add employee, Invite candidate…).
// Long candidate forms are left alone so a stray Enter can't send them early.
document.addEventListener("keydown", (e) => {
  if (e.key !== "Enter" || e.defaultPrevented || e.isComposing) return;
  const el = e.target;
  if (el.tagName !== "SELECT" || el.multiple || !el.form || !el.closest(".modal")) return;
  e.preventDefault();
  el.form.requestSubmit();
});

// A hard sideways trackpad swipe makes Chrome/Edge/Firefox go Back/Forward,
// throwing away whatever the user was typing. The CSS overscroll-behavior rule
// covers Chrome/Edge; this also covers Firefox. Sideways wheel gestures are
// swallowed unless something under the pointer can still scroll that way
// (wide tables, long text boxes), so normal horizontal scrolling still works.
(function () {
  function canScrollX(el, dx) {
    if (el.scrollWidth <= el.clientWidth + 1) return false;
    const isPage = el === document.scrollingElement;
    const ox = getComputedStyle(el).overflowX;
    if (!isPage && ox !== "auto" && ox !== "scroll" && el.tagName !== "INPUT" && el.tagName !== "TEXTAREA") return false;
    if (isPage && ox === "hidden") return false;
    return dx < 0 ? el.scrollLeft > 0 : el.scrollLeft + el.clientWidth < el.scrollWidth - 1;
  }
  document.addEventListener("wheel", (e) => {
    if (e.ctrlKey || Math.abs(e.deltaX) <= Math.abs(e.deltaY)) return;
    for (let el = e.target; el instanceof Element; el = el.parentElement) {
      if (canScrollX(el, e.deltaX)) return;
    }
    e.preventDefault();
  }, { passive: false });
})();

// Same idea for the mouse's side Back/Forward buttons (buttons 3 and 4):
// cancelling them stops Chrome/Edge/Firefox from leaving the page.
["mousedown", "mouseup", "auxclick"].forEach((type) =>
  document.addEventListener(type, (e) => {
    if (e.button === 3 || e.button === 4) e.preventDefault();
  })
);
