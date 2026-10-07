// On phones, wide tables (repeating form rows, candidate/staff/audit lists) are
// restacked by CSS into one card per row. Each cell then needs its column
// name beside it, so copy the header text onto every cell as data-label.
// Rows are added at runtime (Add Row, prefill, drafts), so keep watching.
(function () {
  function headers(table) {
    const ths = table.tHead ? table.tHead.rows[0]?.cells : null;
    return ths ? Array.from(ths, th => th.textContent.replace(/\*/g, "").trim()) : [];
  }

  function label(table) {
    const names = headers(table);
    if (!names.length) return;
    Array.from(table.tBodies).forEach(tbody => {
      Array.from(tbody.rows).forEach(tr => {
        Array.from(tr.cells).forEach((td, i) => {
          const name = names[i] || "";
          if (td.dataset.label !== name) td.dataset.label = name;
          // A blank header is the delete/actions column.
          td.classList.toggle("cell-actions", !name);
        });
      });
    });
  }

  function watch(table) {
    if (table._stackWatched) return;
    table._stackWatched = true;
    label(table);
    new MutationObserver(() => label(table))
      .observe(table, { childList: true, subtree: true });
  }

  const SELECTOR = "table.rep, table.stack, table.cards";

  function init() {
    document.querySelectorAll(SELECTOR).forEach(watch);
    // Staff pages render their tables later via innerHTML, so pick those up too.
    new MutationObserver(() => document.querySelectorAll(SELECTOR).forEach(watch))
      .observe(document.body, { childList: true, subtree: true });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
