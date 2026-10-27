/* Run report viewer: fetches a run's report.md from this controller and
   renders the small Markdown subset the harness writes (headings, pipe
   tables, fenced code, lists, paragraphs, `code` and **bold**). Every piece
   of text goes into the page through textContent, never as HTML. */
(() => {
  "use strict";

  const $ = (id) => document.getElementById(id);

  function el(tag, className, ...children) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    for (const child of children) {
      if (child === null || child === undefined) continue;
      node.append(child instanceof Node ? child : document.createTextNode(String(child)));
    }
    return node;
  }

  // Inline `code` and **bold**; everything else is plain text.
  function inline(text) {
    const parts = [];
    const pattern = /`([^`]+)`|\*\*([^*]+)\*\*/g;
    let last = 0;
    let match;
    while ((match = pattern.exec(text)) !== null) {
      if (match.index > last) parts.push(text.slice(last, match.index));
      parts.push(match[1] !== undefined ? el("code", null, match[1]) : el("strong", null, match[2]));
      last = pattern.lastIndex;
    }
    if (last < text.length) parts.push(text.slice(last));
    return parts;
  }

  // Cells of one pipe-table row; "\|" is a literal bar.
  function cells(line) {
    const body = line.trim().replace(/^\|/, "").replace(/\|$/, "");
    const out = [];
    let current = "";
    for (let i = 0; i < body.length; i += 1) {
      if (body[i] === "\\" && body[i + 1] === "|") { current += "|"; i += 1; }
      else if (body[i] === "|") { out.push(current.trim()); current = ""; }
      else current += body[i];
    }
    out.push(current.trim());
    return out;
  }

  const isSeparator = (line) => /^\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)*\|?\s*$/.test(line.trim());

  function render(markdown) {
    const lines = markdown.replace(/\r\n?/g, "\n").split("\n");
    const blocks = [];
    let i = 0;
    while (i < lines.length) {
      const line = lines[i];
      if (/^```/.test(line)) {
        const code = [];
        i += 1;
        while (i < lines.length && !/^```/.test(lines[i])) { code.push(lines[i]); i += 1; }
        i += 1;
        blocks.push(el("pre", "log report-code", code.join("\n")));
        continue;
      }
      const heading = /^(#{1,6})\s+(.*)$/.exec(line);
      if (heading) {
        blocks.push(el(`h${Math.min(heading[1].length + 1, 6)}`, null, ...inline(heading[2])));
        i += 1;
        continue;
      }
      if (/^\s*\|/.test(line) && i + 1 < lines.length && isSeparator(lines[i + 1])) {
        const head = cells(line);
        i += 2;
        const rows = [];
        while (i < lines.length && /^\s*\|/.test(lines[i])) { rows.push(cells(lines[i])); i += 1; }
        const blank = head.every((cell) => !cell);
        const table = el("table", "report-table",
          blank ? null : el("thead", null, el("tr", null,
            ...head.map((cell) => el("th", null, ...inline(cell))))),
          el("tbody", null, ...rows.map((row) => el("tr", null,
            ...row.map((cell) => el("td", null, ...inline(cell)))))));
        blocks.push(el("div", "table-wrap", table));
        continue;
      }
      if (/^\s*[-*]\s+/.test(line)) {
        const items = [];
        while (i < lines.length && /^\s*[-*]\s+/.test(lines[i])) {
          items.push(el("li", null, ...inline(lines[i].replace(/^\s*[-*]\s+/, ""))));
          i += 1;
        }
        blocks.push(el("ul", null, ...items));
        continue;
      }
      if (!line.trim()) { i += 1; continue; }
      const paragraph = [];
      while (i < lines.length && lines[i].trim() && !/^(#{1,6}\s|```|\s*\||\s*[-*]\s)/.test(lines[i])) {
        paragraph.push(lines[i]);
        i += 1;
      }
      blocks.push(el("p", null, ...inline(paragraph.join(" "))));
    }
    return blocks;
  }

  async function start() {
    const runId = new URLSearchParams(window.location.search).get("run") || "";
    const status = $("report-status");
    if (!/^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$/.test(runId)) {
      status.textContent = "No run given: open this page from a run's report link";
      return;
    }
    const url = `/api/runs/${encodeURIComponent(runId)}/report.md`;
    $("report-raw").href = url;
    document.title = `Report ${runId} · Ubertooth test harness`;
    try {
      const response = await fetch(url, { headers: { Accept: "text/markdown, text/plain" } });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      $("report").replaceChildren(...render(await response.text()));
      status.textContent = "";
      status.hidden = true;
    } catch (error) {
      status.textContent = `Could not load the report: ${error.message}`;
    }
  }

  document.addEventListener("DOMContentLoaded", start);
})();
