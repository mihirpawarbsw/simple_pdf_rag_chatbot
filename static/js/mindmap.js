/**
 * mindmap.js — Nexora AI  |  Interactive Mind Map (L→R Tree Edition)
 * ====================================================================
 * Left-to-right expanding tree. Ancestors stay visible as you go deeper.
 * Click any node → right panel opens with description, keywords, sources.
 * "Deep Dive" lazy-loads n-level children from backend on demand.
 *
 * Public API:  MindMap.open()   MindMap.close()   MindMap.generate()
 * Backend:     POST /mindmap    POST /mindmap/drill
 */

const MindMap = (() => {

    // ── Palette ────────────────────────────────────────────────────────────
    const PALETTE = [
        "#b18fcf","#7ec8e3","#57cc99","#f4a261",
        "#ffd166","#c77dff","#ef476f","#06d6a0",
        "#118ab2","#ffc43d","#e76f51","#a8dadc",
    ];

    // ── Layout constants ───────────────────────────────────────────────────
    const COL_W      = 200;   // px per depth column
    const ROW_H      = 48;    // vertical slot per node
    const NODE_H     = 36;    // node box height
    const NODE_PAD_X = 16;    // horizontal padding inside node
    const INDENT     = 20;    // connector horizontal stub length
    const ROOT_X     = 40;    // root node left edge

    // ── State ──────────────────────────────────────────────────────────────
    let _injected    = false;
    let _tree        = null;
    let _selectedId  = null;
    let _sessionId   = "mm-session";
    let _files       = [];
    let _expandedIds = new Set();  // which nodes are expanded

    // ─────────────────────────────────────────────────────────────────────
    // DOM injection
    // ─────────────────────────────────────────────────────────────────────
    function _inject() {
        if (_injected) return;
        _injected = true;

        const overlay = document.createElement("div");
        overlay.id    = "mmOverlay";
        overlay.innerHTML = `
        <div id="mmModal">

          <div id="mmHeader">
            <div id="mmTitleGroup">
              <i class="fa-solid fa-brain mmHeaderIcon"></i>
              <div>
                <h2 id="mmTitle">Mind Map</h2>
                <div id="mmSubtitle">Click <strong>Generate</strong> to build from your documents</div>
              </div>
            </div>
            <div id="mmHeaderRight">
              <button id="mmGenBtn" onclick="MindMap.generate()">
                <i class="fa-solid fa-wand-magic-sparkles"></i> Generate
              </button>
              <button id="mmCloseBtn" onclick="MindMap.close()">
                <i class="fa-solid fa-xmark"></i>
              </button>
            </div>
          </div>

          <!-- Canvas area + side panel -->
          <div id="mmCanvasWrap">
            <!-- SVG tree -->
            <div id="mmScrollArea">
              <svg id="mmSvg" xmlns="http://www.w3.org/2000/svg"></svg>
            </div>

            <!-- State overlay -->
            <div id="mmState" class="mm-state">
              <i class="fa-solid fa-brain mm-state-icon"></i>
              <p>Generate a mind map to explore your documents visually.</p>
            </div>

            <!-- Side detail panel -->
            <div id="mmPanel" class="mm-panel-hidden">
              <div id="mmPanelClose" onclick="MindMap._closePanel()">
                <i class="fa-solid fa-xmark"></i>
              </div>
              <div id="mmPanelType"></div>
              <h3 id="mmPanelLabel"></h3>
              <p id="mmPanelSummary"></p>
              <div id="mmPanelKeywords"></div>
              <div id="mmPanelSources"></div>
              <button id="mmDrillBtn" onclick="MindMap._drillSelected()">
                <i class="fa-solid fa-magnifying-glass-plus"></i> Deep Dive
              </button>
              <div id="mmDrillLoader" style="display:none">
                <i class="fa-solid fa-spinner fa-spin"></i> Exploring…
              </div>
            </div>
          </div>

        </div>`;
        (document.getElementById("featureWorkspace") || document.body).appendChild(overlay);
        overlay.addEventListener("click", e => { if (e.target === overlay) MindMap.close(); });
    }

    // ─────────────────────────────────────────────────────────────────────
    // Helpers
    // ─────────────────────────────────────────────────────────────────────
    function _getFiles() {
        const f = [];
        document.querySelectorAll("#documentCheckboxList input:checked")
            .forEach(cb => f.push(cb.value));
        return f;
    }

    function _setState(html, icon = "fa-brain") {
        const s = document.getElementById("mmState");
        if (!s) return;
        s.style.display = "flex";
        s.innerHTML = `<i class="fa-solid ${icon} mm-state-icon"></i><p>${html}</p>`;
    }

    function _clearState() {
        const s = document.getElementById("mmState");
        if (s) s.style.display = "none";
    }

    function _setSubtitle(txt) {
        const el = document.getElementById("mmSubtitle");
        if (el) el.innerHTML = txt;
    }

    // ─────────────────────────────────────────────────────────────────────
    // Color assignment: each root branch gets a palette color;
    // children inherit that branch color (lightened for deeper levels)
    // ─────────────────────────────────────────────────────────────────────
    function _assignColors(node, color = null, branchIdx = 0) {
        if (node.type === "root") {
            node._color = "#b18fcf";
        } else {
            node._color = color || PALETTE[branchIdx % PALETTE.length];
        }
        (node.children || []).forEach((child, i) => {
            const childColor = node.type === "root"
                ? PALETTE[i % PALETTE.length]
                : node._color;
            _assignColors(child, childColor, i);
        });
    }

    // ─────────────────────────────────────────────────────────────────────
    // Layout: compute flat list of visible nodes (DFS, only expanded)
    // Returns array of { node, depth, y, parentY }
    // ─────────────────────────────────────────────────────────────────────
    function _layoutTree() {
        const rows = [];
        let rowIdx = 0;

        function visit(node, depth, parentRowIdx) {
            const myRow = rowIdx++;
            rows.push({ node, depth, row: myRow, parentRow: parentRowIdx });

            if (_expandedIds.has(node.id) && (node.children || []).length > 0) {
                node.children.forEach(child => visit(child, depth + 1, myRow));
            }
        }

        if (_tree) visit(_tree, 0, -1);
        return rows;
    }

    // ─────────────────────────────────────────────────────────────────────
    // SVG rendering
    // ─────────────────────────────────────────────────────────────────────
    function _render() {
        const svg = document.getElementById("mmSvg");
        if (!svg || !_tree) return;

        const rows   = _layoutTree();
        const totalH = Math.max(rows.length * ROW_H + 40, 400);
        const maxDepth = rows.reduce((m, r) => Math.max(m, r.depth), 0);
        const totalW = Math.max(ROOT_X + (maxDepth + 1) * COL_W + 60, 600);

        svg.setAttribute("width",  totalW);
        svg.setAttribute("height", totalH);
        svg.setAttribute("viewBox", `0 0 ${totalW} ${totalH}`);

        // Build SVG elements as string for speed
        const rowY = r => 20 + r * ROW_H + ROW_H / 2;  // center Y of a row

        // Map rowIdx → centerY for connector drawing
        const yMap = {};
        rows.forEach(r => { yMap[r.row] = rowY(r.row); });

        let edgesHtml  = "";
        let nodesHtml  = "";

        rows.forEach(({ node, depth, row, parentRow }) => {
            const x   = ROOT_X + depth * COL_W;
            const y   = rowY(row);
            const col = node._color || "#b18fcf";
            const isSelected  = node.id === _selectedId;
            const isExpanded  = _expandedIds.has(node.id);
            const hasKids     = (node.children || []).length > 0 || node.has_children;
            const isLoaded    = (node.children || []).length > 0;

            // ── Connector from parent ──
            if (parentRow >= 0) {
                const py = yMap[parentRow];
                const px = ROOT_X + (depth - 1) * COL_W;
                // Elbow: horizontal from parent right edge → bend → horizontal to node
                const midX = px + COL_W - INDENT;
                edgesHtml += `
                  <path d="M${px + _nodeWidth(rows.find(r=>r.row===parentRow)?.node)} ${py}
                            H${midX} V${y} H${x}"
                        fill="none" stroke="${col}55" stroke-width="1.5"
                        class="mm-edge" data-id="${node.id}"/>`;
            }

            // ── Node box ──
            const nw      = _nodeWidth(node);
            const fill    = isSelected ? col + "33" : col + "18";
            const stroke  = isSelected ? col        : col + "66";
            const sw      = isSelected ? 2 : 1;
            const textCol = col;

            // Expand/collapse chevron
            let chevron = "";
            if (hasKids) {
                const cx2  = x + nw + 2;
                const cy2  = y;
                const icon = isExpanded ? "▾" : "▸";
                chevron = `<text x="${cx2 + 10}" y="${cy2 + 1}"
                               fill="${col}cc" font-size="13"
                               text-anchor="middle" dominant-baseline="central"
                               class="mm-chevron" style="pointer-events:none">${icon}</text>`;
            }

            // Loading spinner badge
            let badge = "";
            if (hasKids && !isLoaded) {
                badge = `<circle cx="${x + nw - 5}" cy="${y - NODE_H/2 + 5}" r="5"
                               fill="${col}33" stroke="${col}88" stroke-width="1"/>
                         <text x="${x + nw - 5}" y="${y - NODE_H/2 + 5}"
                               fill="${col}" font-size="8" text-anchor="middle"
                               dominant-baseline="central" style="pointer-events:none">+</text>`;
            }

            // Label (truncated)
            const label   = _truncate(node.label || "", nw - NODE_PAD_X * 2);
            const typeTag = node.type !== "root" ? ` <tspan font-size="9" opacity=".5">${node.type}</tspan>` : "";

            nodesHtml += `
              <g class="mm-node ${isSelected ? 'mm-node-selected' : ''}"
                 data-id="${node.id}"
                 style="cursor:pointer"
                 onclick="MindMap._nodeClick('${node.id}')">
                <rect x="${x}" y="${y - NODE_H/2}"
                      width="${nw}" height="${NODE_H}" rx="8"
                      fill="${fill}" stroke="${stroke}" stroke-width="${sw}"/>
                <text x="${x + NODE_PAD_X}" y="${y}"
                      fill="${textCol}" font-size="12" font-weight="600"
                      dominant-baseline="central"
                      font-family="Sora, system-ui, sans-serif"
                      style="pointer-events:none">
                  ${_svgText(label)}${typeTag}
                </text>
                ${badge}
                ${chevron}
              </g>`;
        });

        svg.innerHTML = `
          <defs>
            <style>
              .mm-node:hover rect { filter: brightness(1.15); }
              .mm-edge { transition: stroke 0.2s; }
            </style>
          </defs>
          ${edgesHtml}
          ${nodesHtml}`;
    }

    function _nodeWidth(node) {
        if (!node) return 160;
        const base = node.type === "root" ? 180 : 160;
        return base;
    }

    function _truncate(str, maxPx) {
        // rough 7px per char at 12px font
        const maxChars = Math.floor(maxPx / 7);
        return str.length > maxChars ? str.slice(0, maxChars - 1) + "…" : str;
    }

    function _svgText(str) {
        return str.replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;");
    }

    // ─────────────────────────────────────────────────────────────────────
    // Node click: toggle expand + open panel
    // ─────────────────────────────────────────────────────────────────────
    function _nodeClick(id) {
        // Find node in tree
        const node = _findNode(_tree, id);
        if (!node) return;

        // Toggle expansion
        if (_expandedIds.has(id)) {
            _expandedIds.delete(id);
        } else {
            _expandedIds.add(id);
            // Auto-load children if not yet fetched
            if ((node.children || []).length === 0 && node.has_children) {
                _lazyLoad(node);
            }
        }

        _selectedId = id;
        _render();
        _openPanel(node);
    }

    function _findNode(root, id) {
        if (!root) return null;
        if (root.id === id) return root;
        for (const c of (root.children || [])) {
            const found = _findNode(c, id);
            if (found) return found;
        }
        return null;
    }

    // ─────────────────────────────────────────────────────────────────────
    // Side panel
    // ─────────────────────────────────────────────────────────────────────
    let _selectedNode = null;

    function _openPanel(node) {
        _selectedNode = node;
        const panel   = document.getElementById("mmPanel");
        if (!panel) return;
        panel.classList.remove("mm-panel-hidden");
        panel.classList.add("mm-panel-open");

        const typeLabels = { root: "Document Root", theme: "Theme", concept: "Concept", detail: "Deep Insight" };
        const color      = node._color || "#b18fcf";

        document.getElementById("mmPanelType").innerHTML =
            `<span class="mm-type-badge" style="background:${color}22;color:${color}">${typeLabels[node.type] || node.type}</span>`;
        document.getElementById("mmPanelLabel").textContent   = node.label;
        document.getElementById("mmPanelSummary").textContent = node.summary || "No description available.";

        const kwEl = document.getElementById("mmPanelKeywords");
        kwEl.innerHTML = (node.keywords || []).map(k =>
            `<span class="mm-kw" style="border-color:${color}55;color:${color}">${k}</span>`
        ).join("");

        const srcEl = document.getElementById("mmPanelSources");
        if ((node.sources || []).length) {
            srcEl.innerHTML = `<div class="mm-src-label">Sources</div>` +
                node.sources.map(s =>
                    `<span class="mm-src-tag"><i class="fa-solid fa-file-pdf"></i> ${s}</span>`
                ).join("");
        } else { srcEl.innerHTML = ""; }

        const drillBtn   = document.getElementById("mmDrillBtn");
        const drillLoad  = document.getElementById("mmDrillLoader");
        drillBtn.style.display  = node.has_children ? "flex" : "none";
        drillLoad.style.display = "none";
    }

    function _closePanel() {
        _selectedId   = null;
        _selectedNode = null;
        const panel   = document.getElementById("mmPanel");
        if (!panel) return;
        panel.classList.remove("mm-panel-open");
        panel.classList.add("mm-panel-hidden");
        _render();
    }

    // ─────────────────────────────────────────────────────────────────────
    // Lazy load children for a node (called on first expansion)
    // ─────────────────────────────────────────────────────────────────────
    async function _lazyLoad(node) {
        if (!node.has_children || (node.children || []).length > 0) return;

        const drillBtn  = document.getElementById("mmDrillBtn");
        const loader    = document.getElementById("mmDrillLoader");
        if (drillBtn) drillBtn.style.display = "none";
        if (loader)   loader.style.display   = "flex";

        try {
            const sid = (typeof session_id !== "undefined" && session_id) ? session_id : _sessionId;
            const res = await fetch("/mindmap/drill", {
                method:  "POST",
                headers: { "Content-Type": "application/json" },
                body:    JSON.stringify({
                    session_id:   sid,
                    node_id:      node.id,
                    label:        node.label,
                    parent_label: node.label,
                    files:        _files,
                }),
            });

            if (!res.ok) throw new Error(await res.text());
            const data = await res.json();

            node.children     = data.children || [];
            node.has_children = node.children.length > 0;

            // Assign colors to new children
            _assignColors(node, node._color, 0);

            _render();

            if (drillBtn) drillBtn.style.display = node.has_children ? "flex" : "none";
        } catch (err) {
            console.error("[MindMap lazy]", err);
            if (loader) loader.innerHTML =
                `<span style="color:#ef476f"><i class="fa-solid fa-circle-exclamation"></i> Error loading</span>`;
        } finally {
            if (loader && loader.innerHTML.includes("Exploring"))
                loader.style.display = "none";
        }
    }

    // Public drill button: same as lazy load but triggered manually
    async function _drillSelected() {
        if (!_selectedNode) return;
        await _lazyLoad(_selectedNode);
        // If not yet expanded, expand now
        _expandedIds.add(_selectedNode.id);
        _render();
    }

    // ─────────────────────────────────────────────────────────────────────
    // Public: generate
    // ─────────────────────────────────────────────────────────────────────
    async function generate() {
        _files = _getFiles();
        const sid = (typeof session_id !== "undefined" && session_id) ? session_id : "mm-session";
        _sessionId = sid;

        const btn = document.getElementById("mmGenBtn");
        if (btn) { btn.disabled = true; btn.innerHTML = `<i class="fa-solid fa-spinner fa-spin"></i> Building…`; }
        _setState("Building your mind map… this may take 30–60 seconds.", "fa-spinner fa-spin");
        _tree = null; _selectedId = null; _expandedIds = new Set();
        _closePanel();

        try {
            const res = await fetch("/mindmap", {
                method:  "POST",
                headers: { "Content-Type": "application/json" },
                body:    JSON.stringify({ session_id: sid, files: _files }),
            });

            if (!res.ok) {
                const e = await res.json().catch(() => ({}));
                _setState(`Error: ${e.message || res.statusText}`, "fa-circle-exclamation");
                return;
            }

            _tree = await res.json();
            _assignColors(_tree);

            // Auto-expand root
            _expandedIds.add(_tree.id);

            _clearState();
            _render();

            const themeCount   = (_tree.children || []).length;
            const conceptCount = (_tree.children || []).reduce((s, t) => s + (t.children?.length || 0), 0);
            _setSubtitle(`${themeCount} themes · ${conceptCount} concepts · click any node to explore`);
            document.getElementById("mmTitle").textContent = _tree.label || "Mind Map";

        } catch (err) {
            console.error("[MindMap]", err);
            _setState("Network error. Please try again.", "fa-triangle-exclamation");
        } finally {
            if (btn) { btn.disabled = false; btn.innerHTML = `<i class="fa-solid fa-rotate"></i> Regenerate`; }
        }
    }

    // ─────────────────────────────────────────────────────────────────────
    // Public: open / close
    // ─────────────────────────────────────────────────────────────────────
    function open() {
        _inject();
        document.getElementById("mmOverlay").classList.add("mm-active");
        if (_tree) _render();
    }

    function close() {
        const o = document.getElementById("mmOverlay");
        if (o) o.classList.remove("mm-active");
        if (typeof backToChatbot === "function") backToChatbot();
    }

    window.addEventListener("resize", () => {
        if (document.getElementById("mmOverlay")?.classList.contains("mm-active") && _tree) {
            _render();
        }
    });

    return { open, close, generate, _drillSelected, _closePanel, _nodeClick };

})();