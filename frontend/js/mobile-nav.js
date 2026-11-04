// Phones and tablets (<= 900px): the staff sidebar becomes a drawer that
// slides in from the left. This adds the compact top bar (menu button, page
// title, avatar) and the dimmed backdrop; CSS in hr-portal/css/style.css does
// the layout. Desktop is untouched: the top bar and backdrop are hidden there.
(function () {
  const MOBILE = window.matchMedia("(max-width: 900px)");

  function init() {
    const shell = document.querySelector(".app-shell");
    const sidebar = shell && shell.querySelector(".sidebar");
    if (!sidebar || document.querySelector(".mobile-topbar")) return;

    sidebar.id = sidebar.id || "appSidebar";
    sidebar.setAttribute("aria-label", "Main menu");
    sidebar.tabIndex = -1;

    const bar = document.createElement("header");
    bar.className = "mobile-topbar";
    bar.innerHTML = `
      <button type="button" class="menu-toggle" aria-label="Open menu"
              aria-controls="${sidebar.id}" aria-expanded="false">
        <span></span><span></span><span></span>
      </button>
      <div class="mobile-title">
        <span class="mobile-page"></span>
        <span class="mobile-portal"></span>
      </div>
      <button type="button" class="avatar mobile-avatar" aria-label="Account menu"></button>`;
    shell.before(bar);

    const scrim = document.createElement("div");
    scrim.className = "sidebar-scrim";
    scrim.setAttribute("aria-hidden", "true");
    shell.before(scrim);

    // Close button inside the drawer, next to the brand.
    const brand = sidebar.querySelector(".sidebar-brand");
    if (brand && !brand.querySelector(".menu-close")) {
      const close = document.createElement("button");
      close.type = "button";
      close.className = "menu-close";
      close.setAttribute("aria-label", "Close menu");
      close.innerHTML = "&times;";
      close.onclick = () => setOpen(false);
      brand.append(close);
    }

    const toggle = bar.querySelector(".menu-toggle");
    const pageEl = bar.querySelector(".mobile-page");
    const portalEl = bar.querySelector(".mobile-portal");
    const avatarEl = bar.querySelector(".mobile-avatar");

    // Title, portal name and avatar are filled in later by the page scripts
    // (role-specific brand, nav built by candidate.js), so mirror them live.
    function sync() {
      const active = sidebar.querySelector(".nav-item.active");
      const nameEl = brand && Array.from(brand.children)
        .find(c => c.tagName === "SPAN" && !c.classList.contains("brand-mark"));
      const portal = nameEl ? nameEl.textContent.trim() : "";
      const page = (active ? active.textContent : "").trim() || portal;
      pageEl.textContent = page;
      portalEl.textContent = page === portal ? "" : portal;
      const av = sidebar.querySelector(".user-chip .avatar");
      avatarEl.textContent = av ? av.textContent : "";
    }
    sync();
    new MutationObserver(sync).observe(sidebar, { childList: true, subtree: true, characterData: true });

    let lastFocus = null;
    function setOpen(open) {
      if (open && !MOBILE.matches) return;
      document.body.classList.toggle("nav-open", open);
      toggle.setAttribute("aria-expanded", String(open));
      toggle.setAttribute("aria-label", open ? "Close menu" : "Open menu");
      if (open) {
        lastFocus = document.activeElement;
        // Focus the drawer itself (no ring on a link) so Tab starts inside it.
        sidebar.focus({ preventScroll: true });
      } else if (lastFocus && sidebar.contains(document.activeElement)) {
        lastFocus.focus({ preventScroll: true });
      }
    }

    toggle.onclick = () => setOpen(!document.body.classList.contains("nav-open"));
    avatarEl.onclick = () => setOpen(true);
    scrim.onclick = () => setOpen(false);
    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape" && document.body.classList.contains("nav-open")) setOpen(false);
    });
    // Picking a page, logging out or connecting Zoho closes the drawer.
    sidebar.addEventListener("click", (e) => {
      if (e.target.closest("a, .logout-btn")) setOpen(false);
    });
    MOBILE.addEventListener("change", () => setOpen(false));

    // Swipe left on the open drawer to close it.
    let startX = null, startY = null;
    sidebar.addEventListener("touchstart", (e) => {
      const t = e.touches[0]; startX = t.clientX; startY = t.clientY;
    }, { passive: true });
    sidebar.addEventListener("touchend", (e) => {
      if (startX === null) return;
      const t = e.changedTouches[0];
      const dx = t.clientX - startX, dy = Math.abs(t.clientY - startY);
      if (dx < -60 && dy < 50) setOpen(false);
      startX = null;
    }, { passive: true });

    // Swipe right from the left screen edge to open it.
    let edgeX = null, edgeY = null;
    document.addEventListener("touchstart", (e) => {
      const t = e.touches[0];
      edgeX = MOBILE.matches && !document.body.classList.contains("nav-open") && t.clientX < 24 ? t.clientX : null;
      edgeY = t.clientY;
    }, { passive: true });
    document.addEventListener("touchend", (e) => {
      if (edgeX === null) return;
      const t = e.changedTouches[0];
      if (t.clientX - edgeX > 60 && Math.abs(t.clientY - edgeY) < 50) setOpen(true);
      edgeX = null;
    }, { passive: true });
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init);
  else init();
})();
