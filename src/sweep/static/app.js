// Sweep page. All text from the server is put on the page with textContent,
// never as HTML, because it can contain anything found on disk.
"use strict";

const TOKEN = document.querySelector('meta[name="sweep-token"]').content;
const $ = (id) => document.getElementById(id);
let STATE = { recycle_supported: false };
const picked = { scan: new Set(), search: new Set(), history: new Set() };
const dangerOk = new Set();      // tracked repo files whose warning was accepted
let scanData = null, searchData = null, historyData = null;
const extraFolders = [];

// --- small helpers ---------------------------------------------------------

function el(tag, props = {}, ...kids) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(props)) {
    if (k === "class") node.className = v;
    else if (k === "text") node.textContent = v;
    else if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
    else if (v === true) node.setAttribute(k, "");
    else if (v !== false && v != null) node.setAttribute(k, v);
  }
  for (const kid of kids.flat()) {
    if (kid == null) continue;
    node.append(kid instanceof Node ? kid : document.createTextNode(String(kid)));
  }
  return node;
}

function size(bytes) {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1048576) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / 1048576).toFixed(1)} MB`;
}

function plural(n, word) { return `${n.toLocaleString()} ${word}${n === 1 ? "" : "s"}`; }

function say(text, bad = false) {
  const box = $("message");
  box.textContent = text;
  box.className = "message" + (bad ? " bad" : "");
  if (!bad) setTimeout(() => { if (box.textContent === text) box.classList.add("hidden"); }, 6000);
}

async function api(name, body = {}) {
  const response = await fetch(`/api/${name}`, {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-Sweep-Token": TOKEN },
    body: JSON.stringify(body),
  });
  const data = await response.json().catch(() => ({ error: "bad reply" }));
  if (!response.ok) throw new Error(data.error || `error ${response.status}`);
  return data;
}

async function guarded(fn) {
  try { await fn(); } catch (error) { say(error.message, true); }
}

function unlock(id, on = true) { $(id).classList.toggle("locked", !on); }

// replaceChildren writes "null" for a missing optional part, so drop those.
function put(id, ...kids) { $(id).replaceChildren(...kids.flat().filter((k) => k != null)); }

// --- jobs: ask for progress only while one is running ----------------------

function waitForJob(onDone) {
  // Keep the progress row just under the header, whatever height it wraps to.
  document.documentElement.style.setProperty("--header-height", `${document.querySelector(".top").offsetHeight}px`);
  $("job").classList.remove("hidden");
  const tick = async () => {
    let state;
    try { state = await api("state"); } catch (error) { say(error.message, true); return; }
    STATE = state;
    const job = state.job;
    $("job-text").textContent = `${job.kind}: ${job.message} (${job.seconds} s)`;
    if (job.state === "running") { setTimeout(tick, 600); return; }
    $("job").classList.add("hidden");
    if (job.state === "failed") { say(`${job.kind} failed: ${job.error}`, true); return; }
    if (job.state === "stopped") say(`${job.kind} was stopped. Results are incomplete.`, true);
    guarded(onDone);
  };
  tick();
}

$("job-stop").onclick = () => guarded(() => api("stop"));

// --- the repo ---------------------------------------------------------------

$("repo-set").onclick = () => guarded(async () => {
  const info = await api("repo", { path: $("repo-path").value });
  $("repo-path").value = info.repo;
  const remotes = info.remotes.map((r) => `${r.name} ${r.url}`).join(", ") || "no remotes";
  $("repo-info").textContent = `Repo: ${info.repo}  |  ${plural(info.commits, "commit")}  |  ${remotes}`;
  for (const key of Object.keys(picked)) if (key !== "history") picked[key].clear();
  dangerOk.clear();
  scanData = searchData = null;
  for (const id of ["scan-summary", "scan-values", "scan-files", "hits", "search-summary",
                    "estimate-box", "words-list", "names-list"]) $(id).replaceChildren();
  $("scan-files-box").classList.add("hidden");
  $("scan-filters").classList.add("hidden");
  $("words-box").classList.add("hidden");
  unlock("s1"); unlock("s2"); unlock("s3", false); unlock("s4", false);
  renderDeleteBars();
});

// folder picker

let browsePath = "";
async function openBrowser(path, onPick) {
  const data = await api("browse", { path });
  browsePath = data.path;
  $("browser").classList.remove("hidden");
  $("browser-path").textContent = data.path || "Drives";
  $("browser-up").disabled = !data.path;
  $("browser-up").onclick = () => guarded(() => openBrowser(data.parent, onPick));
  $("browser-pick").disabled = !data.path;
  $("browser-pick").onclick = () => { $("browser").classList.add("hidden"); onPick(browsePath); };
  put("browser-list", ...data.folders.map((f) => el("li", {
    class: f.openable ? "" : "off",
    onclick: f.openable ? () => guarded(() => openBrowser(f.path, onPick)) : null,
  }, f.name, f.is_repo ? el("span", { class: "tag", text: "git repo" }) : null,
     f.why ? el("span", { class: "muted small", text: `  (${f.why}, not opened)` }) : null)));
}
$("browser-close").onclick = () => $("browser").classList.add("hidden");
$("repo-browse").onclick = () => guarded(() => openBrowser($("repo-path").value,
  (path) => { $("repo-path").value = path; }));

// --- section 1 --------------------------------------------------------------

$("scan-run").onclick = () => guarded(async () => {
  await api("scan");
  waitForJob(async () => {
    scanData = await api("scan-result");
    picked.scan.clear();
    renderScan();
  });
});

for (const box of document.querySelectorAll("#scan-filters input")) box.onchange = () => renderScan();

function renderScan() {
  if (!scanData) return;
  const counts = { high: 0, medium: 0, low: 0 };
  const groups = new Map();
  const shown = new Set([...document.querySelectorAll("#scan-filters input:checked")]
    .map((b) => b.dataset.area));
  for (const f of scanData.findings) {
    const area = f.area === "history" && f.in_head ? null : f.area;
    if (!area) continue;          // the same line is already shown in the working tree
    counts[f.severity] += 1;
    if (!shown.has(area)) continue;
    const key = `${f.detector}|${f.value.toLowerCase()}`;
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(f);
  }
  const tree = scanData.searched.working_tree || {};
  const hist = scanData.searched.history;
  put("scan-summary", el("div", { class: "summary" },
    ...["high", "medium", "low"].map((s) => el("div", {}, el("b", { class: `sev-${s}`, text: counts[s] }), `${s} findings`)),
    el("div", {}, el("b", { text: (tree.tracked || 0) + (tree.untracked || 0) + (tree.ignored || 0) }), "files read"),
    hist ? el("div", {}, el("b", { text: hist.commits }), "commits searched") : null),
    el("p", { class: "muted small", text: "Not checked: " + scanData.not_checked.join(" ") }));
  $("scan-filters").classList.remove("hidden");

  const order = { high: 0, medium: 1, low: 2 };
  const list = [...groups.values()].sort((a, b) =>
    order[a[0].severity] - order[b[0].severity] || b.length - a.length);
  put("scan-values", ...list.slice(0, 300).map((items) => {
    const first = items[0];
    return el("details", { class: "value" },
      el("summary", {},
        el("span", { class: `sev-${first.severity}`, text: first.severity }),
        el("b", { class: "mono", text: first.value }),
        el("span", { class: "muted", text: `${first.detector}, ${plural(items.length, "place")}` }),
        first.note ? el("span", { class: "muted small", text: first.note }) : null),
      el("div", { class: "where" }, ...items.slice(0, 40).map((f) => el("div", {},
        el("span", { class: "mono small", text: f.area === "history"
          ? `${f.path}:${f.line}  (history only, commit ${(f.commits[0] || "").slice(0, 10)})`
          : f.line ? `${f.path}:${f.line}  (${f.area}${f.status ? ", " + f.status : ""})` : `${f.path}  (${f.area})` }),
        f.area === "history" ? fileLinks("blob", f.blob, f.line, [f.value], false)
          : f.file_id ? fileLinks("scan", f.file_id, f.line, [f.value]) : null,
        f.snippet ? el("div", { class: "snippet" }, ...markWords(f.snippet, [f.value])) : null)),
        items.length > 40 ? el("div", { class: "muted", text: `... and ${items.length - 40} more` }) : null,
        items.some((f) => f.area === "history" && !f.in_head)
          ? el("div", { class: "steps", text: "History-only findings stay in the repo until its history is rewritten (for example with git-filter-repo). Sweep does not rewrite history. Anyone who already pulled the repo keeps them." })
          : null));
  }), list.length > 300 ? el("p", { class: "muted", text: `${list.length - 300} more values are not shown. Save the report to see all of them.` }) : null);

  const files = scanData.files;
  $("scan-files-box").classList.toggle("hidden", !files.length);
  put("scan-files", ...files.map((f) => el("tr", { class: f.status === "tracked" ? "tracked" : "" },
    el("td", {}, el("input", { type: "checkbox", checked: picked.scan.has(f.id),
      onchange: (e) => pickScanFile(e.target, f) })),
    el("td", { class: "path" }, f.relative, fileLinks("scan", f.id, 0, [])),
    el("td", {}, f.status === "tracked" ? el("b", { class: "bad", text: "tracked (DANGER)" }) : f.status),
    el("td", { text: size(f.size) }))));
  renderDeleteBars();
}

function pickScanFile(box, file) {
  if (!box.checked) { picked.scan.delete(file.id); renderDeleteBars(); return; }
  if (file.status !== "tracked") { picked.scan.add(file.id); renderDeleteBars(); return; }
  box.checked = false;
  confirmDialog("DANGER: git tracks this file", [
    el("div", { class: "danger-box" },
      el("p", {}, el("b", { text: "Deleting it only removes the copy in the folder. " }),
        "Git still has it."),
      el("ul", {},
        el("li", { text: "The deletion must still be committed in the repo." }),
        el("li", { text: "Every earlier version stays in the repo's history until history is rewritten." }),
        el("li", { text: "Anyone who already pulled the repo keeps it." })),
      el("p", { class: "mono small", text: file.relative })),
  ], "Allow deleting this tracked file", () => {
    dangerOk.add(file.id); picked.scan.add(file.id); box.checked = true; renderDeleteBars();
  });
}

// --- section 2 --------------------------------------------------------------

$("words-propose").onclick = () => guarded(async () => {
  await api("words/propose");
  waitForJob(async () => renderWords(await api("words")));
});

function checkItem(value, note, checked = true) {
  return el("li", {}, el("input", { type: "checkbox", checked, value }),
    el("span", { class: "mono", text: value }),
    note ? el("span", { class: "why muted small", text: note }) : null);
}

function renderWords(data) {
  $("words-box").classList.remove("hidden");
  put("words-list", ...data.words.map((w) =>
    checkItem(w.term, `in ${plural(w.files, "file")}; ${w.why}`)));
  put("names-list", ...data.file_names.map((n) => checkItem(n, "")));
  $("words-notes").textContent = data.notes.join(" ");
  $("words-status").textContent = "Not reviewed yet.";
}

function addTo(listId, inputId, minLength) {
  const value = $(inputId).value.trim();
  if (value.length < minLength) { say(`At least ${minLength} characters.`, true); return; }
  $(listId).prepend(checkItem(value, "added by you"));
  $(inputId).value = "";
}
$("word-add-btn").onclick = () => addTo("words-list", "word-add", 4);
$("name-add-btn").onclick = () => addTo("names-list", "name-add", 1);

const checkedValues = (listId) => [...document.querySelectorAll(`#${listId} input:checked`)].map((b) => b.value);

$("words-review").onclick = () => guarded(async () => {
  const done = await api("words/review", { words: checkedValues("words-list"), file_names: checkedValues("names-list") });
  $("words-status").textContent = `Reviewed: ${plural(done.words, "word")}, ${plural(done.file_names, "file name")}. Changing the list means pressing this again.`;
  unlock("s3");
  unlock("s4", false);
  searchData = null; picked.search.clear();
  put("hits", ); put("search-summary", );
  await loadPlaces();
});

// --- section 3 --------------------------------------------------------------

async function loadPlaces() {
  const data = await api("places");
  put("places-list", ...data.places.map((p) => el("li", {},
    el("input", { type: "checkbox", checked: p.checked, "data-label": p.label, value: p.path }),
    el("span", { text: p.label }),
    el("span", { class: "why mono small muted", text: p.path + (p.recycle_bin ? "  (off by default)" : "") }))));
  put("drives-list", ...data.drives.map((d) => el("li", {},
    el("input", { type: "checkbox", "data-label": "drive", value: d }), el("span", { class: "mono", text: d }))));
  renderFolders();
}

function renderFolders() {
  put("folders-list", ...extraFolders.map((f) => el("li", {},
    el("input", { type: "checkbox", checked: true, "data-label": "folder", value: f }),
    el("span", { class: "mono small", text: f }))));
}
$("folder-add-btn").onclick = () => {
  const value = $("folder-add").value.trim();
  if (value) { extraFolders.push(value); $("folder-add").value = ""; renderFolders(); }
};

function scopes() {
  return [...document.querySelectorAll("#s3 ul.checks input:checked")]
    .map((b) => ({ label: b.dataset.label, path: b.value }));
}

$("estimate-run").onclick = () => guarded(async () => {
  await api("estimate", { scopes: scopes() });
  waitForJob(async () => {
    const { estimate: e } = await api("estimate-result");
    put("estimate-box", el("div", { class: "summary" },
      el("div", {}, el("b", { text: e.files.toLocaleString() }), "files in these places"),
      el("div", {}, el("b", { text: e.files_to_open.toLocaleString() }), "would be opened"),
      el("div", {}, el("b", { text: `${e.first_run_minutes} min` }), "rough time on a first run")),
      el("p", { class: "muted small", text: "The time is a rough guide: about 30 ms per opened file the first time, measured on one machine. Repeat runs are much faster." }));
  });
});

$("search-run").onclick = () => guarded(async () => {
  await api("search", { scopes: scopes() });
  waitForJob(async () => {
    searchData = await api("search-result");
    picked.search.clear();
    unlock("s4");
    renderSearch();
    $("s4").scrollIntoView({ behavior: "smooth" });
  });
});

// --- section 4 --------------------------------------------------------------

function renderSearch() {
  if (!searchData) return;
  const hits = searchData.hits.filter((h) => !h.gone);
  const count = { strong: 0, medium: 0, weak: 0 };
  for (const h of hits) count[h.strength] += 1;
  const s = searchData.searched;
  put("search-summary", el("div", { class: "summary" },
    ...["strong", "medium", "weak"].map((k) => el("div", {}, el("b", { class: `str-${k}`, text: count[k] }), `${k} hits`)),
    el("div", {}, el("b", { text: searchData.copies.length }), "other copies of the repo"),
    el("div", {}, el("b", { text: s.files_seen.toLocaleString() }), `files seen in ${searchData.seconds} s`)),
    searchData.stopped ? el("p", { class: "bad", text: "The search was stopped part way. These results are incomplete." }) : null,
    searchData.copies.length ? el("p", { class: "muted", text: "Folders sharing this repo's history (not searched file by file): " + searchData.copies.join(", ") }) : null,
    el("p", { class: "muted small", text: "Not searched: " + searchData.cannot_see.join(" ") }));

  const folders = new Map();
  for (const h of hits) {
    if (!folders.has(h.folder)) folders.set(h.folder, []);
    folders.get(h.folder).push(h);
  }
  const order = { strong: 0, medium: 1, weak: 2 };
  const ranked = [...folders.entries()].sort((a, b) =>
    Math.min(...a[1].map((h) => order[h.strength])) - Math.min(...b[1].map((h) => order[h.strength])) ||
    b[1].length - a[1].length);
  put("hits", ...ranked.map(([folder, items]) => el("div", { class: "folder" },
    el("header", {}, el("span", { text: folder }), el("span", { class: "muted", text: plural(items.length, "file") })),
    ...items.map(hitRow))));
  renderDeleteBars();
}

function hitRow(h) {
  const name = h.path.slice(h.folder.length).replace(/^[\\/]/, "");
  const details = [];
  if (h.exact_copy_of.length) details.push(`exact copy of ${h.exact_copy_of.slice(0, 3).join(", ")}${h.weak_copy ? " (a file many projects share)" : ""}`);
  if (h.name_match) details.push("same file name as one in the repo");
  if (Object.keys(h.terms).length) details.push("search words: " + Object.entries(h.terms).map(([t, n]) => `${t} (${n})`).join(", "));
  return el("div", { class: "hit" },
    h.deletable ? el("input", { type: "checkbox", checked: picked.search.has(h.id), onchange: (e) => {
      e.target.checked ? picked.search.add(h.id) : picked.search.delete(h.id); renderDeleteBars(); } })
      : el("span", { class: "muted", title: "inside another git repo", text: "x" }),
    el("div", {},
      el("div", {}, el("span", { class: `str-${h.strength}`, text: h.strength + " " }), el("span", { class: "name", text: name }),
        el("span", { class: "muted small", text: `  ${size(h.size)}` }),
        fileLinks("search", h.id, h.line, Object.keys(h.terms))),
      ...details.map((d) => el("div", { class: "detail", text: d })),
      h.snippet ? el("div", { class: "snippet" }, `line ${h.line}: `, ...markWords(h.snippet, Object.keys(h.terms))) : null,
      h.steps.length ? el("div", { class: "steps", text: h.steps.join("\n") }) : null));
}

// --- deleting ------------------------------------------------------------------

function renderDeleteBars() {
  for (const bar of document.querySelectorAll(".delete-bar")) {
    const source = bar.dataset.source;
    const n = picked[source].size;
    bar.replaceChildren(
      el("span", { class: "muted", text: n ? `${plural(n, "file")} chosen` : "Tick files to delete them." }),
      el("button", { type: "button", disabled: !n || !STATE.recycle_supported,
        title: STATE.recycle_supported ? "" : "Recycle Bin only on Windows",
        onclick: () => askDelete(source, "recycle") }, "Move to Recycle Bin"),
      el("button", { type: "button", class: "danger-outline", disabled: !n,
        onclick: () => askDelete(source, "permanent") }, "Delete permanently..."));
  }
}

function chosenPaths(source) {
  const ids = [...picked[source]];
  if (source === "scan") return ids.map((id) => scanData.files.find((f) => f.id === id)?.relative || id);
  if (source === "search") return ids.map((id) => searchData.hits.find((h) => h.id === id)?.path || id);
  return ids;
}

function askDelete(source, mode) {
  const ids = [...picked[source]];
  const list = el("ul", { class: "mono small" }, ...chosenPaths(source).slice(0, 50).map((p) => el("li", { text: p })),
    ids.length > 50 ? el("li", { text: `... and ${ids.length - 50} more` }) : null);
  const body = [list];
  let input = null;
  if (mode === "recycle") {
    body.unshift(el("p", { text: `Move ${plural(ids.length, "file")} to the Recycle Bin? They can be restored from there. They are still on this machine until the Recycle Bin is emptied. If Windows cannot recycle a file, it will ask on screen before deleting it for good.` }));
  } else {
    input = el("input", { type: "number", min: "0", placeholder: "number of files" });
    body.unshift(el("div", { class: "danger-box" }, el("b", { text: "Permanent delete cannot be undone." }),
      el("p", { text: `Type the number of files, ${ids.length}, to confirm.` }), input));
  }
  confirmDialog(mode === "recycle" ? "Move to Recycle Bin" : "Delete permanently", body,
    mode === "recycle" ? "Move to Recycle Bin" : "Delete permanently", () => guarded(async () => {
      const result = await api("delete", { source, ids, mode,
        confirm: input ? input.value : null,
        danger_ok: source === "scan" ? ids.filter((id) => dangerOk.has(id)) : [] });
      const done = result.outcomes.filter((o) => o.done).length;
      const left = result.outcomes.length - done;
      say(`${plural(done, "file")} done${left ? `, ${left} left alone (see "What was done")` : ""}.`, left > 0);
      picked[source].clear();
      await refreshAfterDelete(source);
    }));
}

async function refreshAfterDelete(source) {
  STATE = await api("state");
  renderLog();
  if (source === "scan") { scanData = await api("scan-result"); renderScan(); }
  if (source === "search") { searchData = await api("search-result"); renderSearch(); }
  if (source === "history") await loadHistory();
  renderDeleteBars();
}

function renderLog() {
  $("log-box").classList.toggle("hidden", !STATE.log.length);
  put("log", ...STATE.log.slice().reverse().map((o) => el("li", { class: o.done ? "ok" : "bad" },
    `${o.time}  ${o.action}: ${o.path}${o.message ? "  (" + o.message + ")" : ""}`,
    o.steps && o.steps.length ? el("div", { class: "steps", text: o.steps.join("\n") }) : null)));
}

// --- dialog ------------------------------------------------------------------

function confirmDialog(title, body, okText, onOk) {
  $("dialog-title").textContent = title;
  put("dialog-body", ...body);
  $("dialog-ok").textContent = okText;
  $("dialog").classList.remove("hidden");
  $("dialog-ok").onclick = () => { $("dialog").classList.add("hidden"); onOk(); };
  $("dialog-cancel").onclick = () => $("dialog").classList.add("hidden");
}

// --- viewing a result ---------------------------------------------------------

// "View" shows the text in the page, read-only. "Show in folder" opens File
// Explorer with the file selected. Neither opens or runs the file itself.
function fileLinks(source, id, line, words, onDisk = true) {
  if (!id) return null;
  return el("span", { class: "links" },
    el("button", { type: "button", onclick: () => guarded(() => viewFile(source, id, line, words)) },
      onDisk ? "View" : "View old version"),
    onDisk ? el("button", { type: "button", onclick: () => guarded(async () => {
      await api("reveal", { source, id }); say("Opened in File Explorer.");
    }) }, "Show in folder") : null);
}

function markWords(text, words) {
  // Longest first, so a word inside a longer matched word is not split off.
  const wanted = words.filter((w) => w && w.length >= 3).sort((a, b) => b.length - a.length);
  if (!wanted.length) return [text];
  const escaped = wanted.map((w) => w.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"));
  const pattern = new RegExp(`(${escaped.join("|")})`, "gi");
  return text.split(pattern).map((part, i) => i % 2 ? el("mark", { text: part }) : part);
}

async function viewFile(source, id, line, words) {
  const data = await api("view", { source, id });
  const lines = data.text.split("\n");
  put("viewer-title", data.title);
  $("viewer-note").textContent = (data.truncated ? "Only the first 512 KB is shown. " : "") +
    "Read-only. Highlighted: the matched text" + (line ? `, and line ${line}.` : ".");
  put("viewer-text", lines.map((text, i) => el("span", { class: i + 1 === line ? "hl" : "", id: i + 1 === line ? "viewer-line" : null },
    el("span", { class: "ln", text: String(i + 1) }), ...markWords(text, words), "\n")));
  $("viewer").classList.remove("hidden");
  const target = $("viewer-line");
  if (target) target.scrollIntoView({ block: "center" });
  else $("viewer-text").scrollTop = 0;
}
$("viewer-close").onclick = () => $("viewer").classList.add("hidden");
document.addEventListener("keydown", (e) => { if (e.key === "Escape") $("viewer").classList.add("hidden"); });

// --- history -----------------------------------------------------------------

async function loadHistory() {
  historyData = await api("history");
  const valid = new Set(historyData.groups.flatMap((g) => g.files.map((f) => f.path)));
  for (const id of [...picked.history]) if (!valid.has(id)) picked.history.delete(id);
  put("history-list", ...historyData.groups.map((g) => {
    const paths = g.files.map((f) => f.path);
    return el("tr", {},
      el("td", {}, el("input", { type: "checkbox", checked: paths.every((p) => picked.history.has(p)),
        onchange: (e) => { for (const p of paths) e.target.checked ? picked.history.add(p) : picked.history.delete(p); renderDeleteBars(); } })),
      el("td", { class: "path", text: g.name }),
      el("td", { class: "small", text: g.files.map((f) => f.name.slice(g.name.length + 1)).join(", ") }),
      el("td", { text: size(g.bytes) }));
  }));
  if (!historyData.groups.length) put("history-list", el("tr", {}, el("td", { colspan: "4", class: "muted", text: `Nothing saved in ${historyData.report_dir}.` })));
  renderDeleteBars();
}

$("history-refresh").onclick = () => guarded(loadHistory);
$("history-select-all").onclick = () => {
  if (!historyData) return;
  for (const g of historyData.groups) for (const f of g.files) picked.history.add(f.path);
  loadHistory();
};
$("save-scan").onclick = () => guarded(async () => { const r = await api("save", { scan: true }); say(`Saved: ${r.written.join(", ")}`); loadHistory(); });
$("save-search").onclick = () => guarded(async () => { const r = await api("save", { search: true }); say(`Saved: ${r.written.join(", ")}`); loadHistory(); });

// --- start ---------------------------------------------------------------------

guarded(async () => {
  STATE = await api("state");
  $("version").textContent = `v${STATE.version}`;
  renderDeleteBars();
  await loadHistory();
});
