/**
 * mindmap.js — Nexora AI  |  Interactive Mind Map (Radial Bubble Edition)
 * =========================================================================
 * Root bubble in the center, themes orbiting it, concepts orbiting each
 * theme, and detail nodes orbiting each concept — fully expanded on first
 * render (no click-to-expand needed). Click any bubble → right panel opens
 * with description, keywords, sources. "Deep Dive" lazy-loads one more
 * ring of children from the backend for any node that still reports
 * has_children = true (e.g. if a branch came back thin).
 *
 * UI structure mirrors graph.js's proven modal pattern:
 *   - one self-contained <style> block injected once (not dependent on
 *     page CSS existing for #mmOverlay/.mm-panel-hidden/etc.)
 *   - exactly ONE generate trigger, live inside the empty-state overlay
 *   - loader / empty-state are absolutely positioned OVER the canvas,
 *     not permanently reserved layout space (fixes the "stuck black box"
 *     and "stuck Building… button" bugs from the previous version)
 *   - side panel only occupies space once a node is actually selected
 *
 * Public API:  MindMap.open()   MindMap.close()   MindMap.generate()
 * Backend:     POST /mindmap    POST /mindmap/drill
 */

const MindMap = (() => {

    // ── Palette (one color per theme branch; children inherit it) ──────────
    const PALETTE = [
        "#b18fcf", "#7ec8e3", "#57cc99", "#f4a261",
        "#ffd166", "#c77dff", "#ef476f", "#06d6a0",
        "#118ab2", "#ffc43d", "#e76f51", "#a8dadc",
    ];
    const ROOT_COLOR = "#3a3f4b";

    // ── Radial layout constants (px) ────────────────────────────────────────
    const R_THEME   = 230;   // distance: root → theme ring
    const R_CONCEPT = 150;   // distance: theme → concept ring
    const R_DETAIL  = 105;   // distance: concept → detail ring

    const RADIUS_BY_TYPE = { root: 58, theme: 42, concept: 28, detail: 18 };
    const FONT_BY_TYPE   = { root: 14, theme: 12.5, concept: 10.5, detail: 8.5 };

    // Minimum angular gap (radians) enforced between siblings on the same
    // ring so labels don't collide when a theme has many concepts, etc.
    const MIN_GAP_CONCEPT = 0.34;
    const MIN_GAP_DETAIL  = 0.30;

    // ── State ────────────────────────────────────────────────────────────────
    let _injected     = false;
    let _tree          = null;
    let _selectedId    = null;
    let _selectedNode  = null;
    let _sessionId     = "mm-session";
    let _files         = [];
    let _positions     = new Map();   // node.id -> {x, y, angle}
    let _zoomBehaviorAttached = false;
    let _panZoom       = { scale: 1, x: 0, y: 0 };
    let _isOpen        = false;
    let _zoomBehavior  = null;

    // ─────────────────────────────────────────────────────────────────────
    // Styles + DOM injection (self-contained, injected once)
    // ─────────────────────────────────────────────────────────────────────
    function _injectStyles() {
        if (document.getElementById("mmStyles")) return;
        const style = document.createElement("style");
        style.id = "mmStyles";
        style.textContent = `
          #mmOverlay {
            display: none; position: fixed; inset: 0; z-index: 9999;
            background: rgba(0, 0, 0, 0.72);
            backdrop-filter: blur(16px);
            -webkit-backdrop-filter: blur(16px);
          }
          #mmOverlay.mm-active { display: flex; flex-direction: column; }

          #mmModal {
            margin: auto;
            width: min(96vw, 1180px);
            height: min(90vh, 800px);
            background: var(--bg-glass-deep, #14151c);
            border: 1px solid var(--border, #2a2d3a);
            border-radius: 20px;
            box-shadow: 0 30px 80px rgba(0,0,0,.7), 0 0 60px var(--accent-glow-soft, rgba(177,143,207,.18));
            display: flex; flex-direction: column; overflow: hidden;
            position: relative;
          }

          #mmHeader {
            display: flex; align-items: center; gap: 12px;
            padding: 16px 22px;
            border-bottom: 1px solid var(--border, #2a2d3a);
            background: var(--accent-bg, rgba(177,143,207,.06));
            flex-shrink: 0;
          }
          #mmTitleGroup { display: flex; align-items: flex-start; gap: 10px; flex: 1; }
          .kg-icon { color: var(--accent, #b18fcf); font-size: 1.2rem; margin-top: 3px; }
          #mmTitle {
            margin: 0; font-size: 1.1rem; font-weight: 700;
            background: var(--grad-text, linear-gradient(90deg,#b18fcf,#7ec8e3));
            -webkit-background-clip: text; -webkit-text-fill-color: transparent;
            background-clip: text; letter-spacing: .02em;
          }
          #mmSubtitle { font-size: 0.78rem; color: var(--text-secondary, #9aa0b4); margin-top: 2px; }
          #mmHeaderRight { display: flex; align-items: center; gap: 8px; }
          #mmCloseBtn {
            background: transparent; border: none; cursor: pointer;
            color: var(--text-muted, #6b7280); font-size: 1.2rem;
            padding: 6px 10px; border-radius: 8px;
            transition: color .2s, background .2s;
            display: flex; align-items: center; justify-content: center;
          }
          #mmCloseBtn:hover { color: var(--accent, #b18fcf); background: var(--accent-bg, rgba(177,143,207,.08)); }

          #mmCanvasWrap { flex: 1; position: relative; overflow: hidden; display: flex; }

          #mmScrollArea {
            flex: 1; position: relative; overflow: auto;
            display: flex; align-items: center; justify-content: center;
            cursor: grab;
          }
          #mmScrollArea:active { cursor: grabbing; }
          #mmSvg { display: block; }

          #mmState {
            position: absolute; inset: 0; z-index: 5;
            display: none;
            background: var(--bg-glass-deep, #14151c);
          }
          #mmState.mm-state-visible { display: flex; }

          #mmZoomControls {
            position: absolute; bottom: 18px; left: 18px; z-index: 10;
            display: flex; flex-direction: column; gap: 6px;
          }
          .mm-zoom-btn {
            width: 32px; height: 32px;
            background: var(--bg-glass-deep, #14151c);
            border: 1px solid var(--border, #2a2d3a);
            border-radius: 8px; color: var(--text-primary, #e8e9ee);
            font-size: .9rem; cursor: pointer;
            display: flex; align-items: center; justify-content: center;
            transition: background .2s, border-color .2s, color .2s;
          }
          .mm-zoom-btn:hover { background: var(--accent-bg, rgba(177,143,207,.08)); border-color: var(--accent, #b18fcf); }

          #mmPanel {
            width: 300px !important; flex-shrink: 0 !important; height: 100% !important;
            border-left: 1px solid var(--border, #2a2d3a) !important;
            background: var(--bg-glass, rgba(20,21,28,.7)) !important;
            padding: 0 !important;
            position: relative;
            transform: none !important;
            transition: width 0.3s cubic-bezier(0.4, 0, 0.2, 1), border-left-color 0.3s !important;
            display: flex; flex-direction: column;
            overflow: visible !important;
          }
          #mmPanel.collapsed {
            width: 0 !important;
            border-left-color: transparent !important;
          }

          #mmPanelContent {
            width: 300px; height: 100%;
            padding: 22px 20px; box-sizing: border-box;
            overflow-y: auto; display: flex; flex-direction: column; gap: 14px;
            transition: opacity 0.25s ease; opacity: 1;
          }
          #mmPanel.collapsed #mmPanelContent {
            opacity: 0; pointer-events: none; overflow: hidden;
          }

          #mmPanelToggle {
            position: absolute; left: -24px; top: 50%; transform: translateY(-50%);
            width: 24px; height: 52px;
            background: var(--bg-glass-deep, #14151c);
            border: 1px solid var(--border, #2a2d3a);
            border-right: none; border-radius: 10px 0 0 10px;
            display: none; align-items: center; justify-content: center;
            cursor: pointer; color: var(--text-secondary, #9aa0b4);
            z-index: 10; box-shadow: -4px 0 12px rgba(0,0,0,0.3);
            transition: background 0.2s, color 0.2s;
          }
          #mmPanel.has-node #mmPanelToggle { display: flex; }
          #mmPanelToggle:hover {
            background: var(--accent-bg, rgba(177,143,207,.08));
            color: var(--accent, #b18fcf);
          }

          #mmPanelClose {
            position: absolute; top: 14px; right: 14px;
            cursor: pointer; color: var(--text-muted, #6b7280);
            font-size: 1rem; padding: 4px 8px; border-radius: 6px;
            transition: color .2s, background .2s; z-index: 5;
          }
          #mmPanelClose:hover { color: var(--accent, #b18fcf); background: var(--accent-bg, rgba(177,143,207,.08)); }

          .mm-type-badge {
            display: inline-block; font-size: .68rem; font-weight: 700;
            text-transform: uppercase; letter-spacing: .05em;
            padding: 3px 10px; border-radius: 20px; margin-bottom: 10px;
          }
          #mmPanelLabel {
            margin: 0 0 10px; font-size: 1.15rem; font-weight: 700;
            color: var(--text-primary, #e8e9ee); line-height: 1.3;
            padding-right: 20px;
          }
          #mmPanelSummary {
            font-size: .86rem; line-height: 1.55;
            color: var(--text-secondary, #9aa0b4); margin: 0 0 16px;
            white-space: pre-wrap;
          }
          #mmPanelKeywords { display: flex; flex-wrap: wrap; gap: 6px; margin-bottom: 16px; }
          .mm-kw {
            font-size: .7rem; padding: 3px 9px; border-radius: 14px;
            border: 1px solid; background: transparent;
          }
          .mm-src-label {
            font-size: .68rem; text-transform: uppercase; letter-spacing: .05em;
            color: var(--text-muted, #6b7280); margin-bottom: 6px;
          }
          .mm-src-tag {
            display: block; font-size: .76rem; color: var(--text-secondary, #9aa0b4);
            padding: 4px 0; display: flex; align-items: center; gap: 6px;
          }
          #mmDrillBtn {
            margin-top: 18px; width: 100%;
            background: var(--grad-button, linear-gradient(90deg,#b18fcf,#7ec8e3));
            color: var(--swal-btn-text, #14151c); border: none;
            padding: 10px 16px; border-radius: 12px;
            font-family: var(--font-display, 'Sora', sans-serif);
            font-size: .85rem; font-weight: 700; cursor: pointer;
            display: none; align-items: center; justify-content: center; gap: 8px;
            transition: transform .15s, box-shadow .15s;
          }
          #mmDrillBtn:hover { transform: translateY(-1px); }
          #mmDrillLoader {
            margin-top: 18px; text-align: center; font-size: .82rem;
            color: var(--text-secondary, #9aa0b4); display: none;
            align-items: center; justify-content: center; gap: 8px;
          }

          .mm-node:hover circle { filter: brightness(1.12); }
          .mm-edge { transition: stroke 0.2s; }

          @media (max-width: 700px) {
            #mmPanel {
              width: 100% !important; height: 45% !important;
              border-left: none !important; border-top: 1px solid var(--border, #2a2d3a) !important;
              transition: height 0.3s cubic-bezier(0.4, 0, 0.2, 1), border-top-color 0.3s !important;
            }
            #mmPanel.collapsed {
              height: 0 !important; border-top-color: transparent !important;
            }
            #mmPanelContent {
              width: 100%; padding: 20px;
            }
            #mmPanelToggle {
              left: 50% !important; top: -24px !important; transform: translateX(-50%) !important;
              width: 52px !important; height: 24px !important;
              border: 1px solid var(--border, #2a2d3a) !important;
              border-bottom: none !important; border-radius: 10px 10px 0 0 !important;
            }
          }
        `;
        document.head.appendChild(style);
    }

    function _inject() {
        if (_injected) return;
        _injected = true;
        _injectStyles();

        const overlay = document.createElement("div");
        overlay.id    = "mmOverlay";
        overlay.innerHTML = `
        <div id="mmModal">

          <div id="mmHeader">
            <div class="mm-header-left">
              <i class="fa-solid fa-brain kg-icon"></i>
              <div>
                <h3 style="margin: 0;" id="mmTitle">Mind Map</h3>
                <span id="mmSubtitle" class="hdr-subtitle">Generates a hierarchical interactive radial map of topics and subtopics.</span>
                <span id="mmDocsList" style="font-size: 0.68rem; color: var(--text-muted); display: block; margin-top: 4px; font-weight: 500;"></span>
              </div>
            </div>
            <div class="mm-header-right">
              <div id="mmStats" style="display: flex; gap: 10px; font-size: .72rem; color: var(--text-secondary);">
                <span><b id="mmStatThemes" style="color: var(--accent);">0</b> themes</span>
                <span><b id="mmStatConcepts" style="color: var(--accent);">0</b> concepts</span>
              </div>
              <button id="mmCloseBtn" onclick="MindMap.close()" title="Close">
                <i class="fa-solid fa-xmark"></i>
              </button>
            </div>
          </div>

          <div id="mmCanvasWrap">
            <div id="mmScrollArea">
              <svg id="mmSvg" xmlns="http://www.w3.org/2000/svg"></svg>
            </div>

            <div id="mmZoomControls">
              <button class="mm-zoom-btn" onclick="MindMap._zoom(1.2)" title="Zoom In"><i class="fa-solid fa-plus"></i></button>
              <button class="mm-zoom-btn" onclick="MindMap._zoom(0.8)" title="Zoom Out"><i class="fa-solid fa-minus"></i></button>
              <button class="mm-zoom-btn" onclick="MindMap._resetZoom()" title="Fit"><i class="fa-solid fa-maximize"></i></button>
            </div>

            <!-- State overlay: empty state, loading state, AND error state all
                 live here, absolutely positioned over the canvas. Exactly one
                 generate/regenerate trigger exists, and it's inside this
                 overlay only — never duplicated in the header. -->
            <div id="mmState" class="mm-state-visible" style="flex-direction: column; align-items: center; justify-content: center; text-align: center; padding: 40px 20px; gap: 12px; color: var(--text-secondary, #9aa0b4);">
              <i class="fa-solid fa-brain" id="mmStateIcon" style="font-size: 32px; color: var(--accent, #b18fcf); margin-bottom: 8px;"></i>
              <h3 style="margin: 0; font-family: var(--font-display, 'Sora', sans-serif); font-size: 1.1rem; color: var(--text-primary, #e8e9ee);">Interactive Mind Map</h3>
              <p id="mmStateText" style="margin: 0 0 12px; font-size: 0.88rem; max-width: 480px; line-height: 1.5;">
                Generates a fully-expanded radial map to help you explore document topics at a glance — click any bubble for details.
              </p>
              <button id="mmGenBtn" onclick="MindMap.generate()" style="background: var(--grad-button, linear-gradient(90deg,#b18fcf,#7ec8e3)); color: var(--swal-btn-text, #14151c); border: none; padding: 10px 24px; border-radius: 12px; font-family: var(--font-display, 'Sora', sans-serif); font-size: 0.88rem; font-weight: 700; cursor: pointer; transition: transform 0.2s, box-shadow 0.2s; box-shadow: 0 4px 12px var(--accent-glow, rgba(177,143,207,.3)); display: inline-flex; align-items: center; gap: 8px;">
                <i class="fa-solid fa-wand-magic-sparkles"></i> Generate Mind Map
              </button>
            </div>

            <!-- Side detail panel — only takes up space once opened -->
            <div id="mmPanel" class="collapsed">
              <div id="mmPanelToggle" onclick="MindMap.togglePanel()">
                <i class="fa-solid fa-chevron-right"></i>
              </div>
              <div id="mmPanelContent">
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
                <div id="mmDrillLoader">
                  <span class="nexora-spinner inline"></span> Exploring…
                </div>
              </div>
            </div>
          </div>

        </div>`;
        (document.getElementById("featureWorkspace") || document.body).appendChild(overlay);
        overlay.addEventListener("click", e => { if (e.target === overlay) MindMap.close(); });

        _attachPanZoom();
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

    function _showState({ icon = "fa-brain", spinning = false, title = "Interactive Mind Map", text = "", showButton = true, buttonLabel = "Generate Mind Map", buttonIcon = "fa-wand-magic-sparkles" } = {}) {
        const state = document.getElementById("mmState");
        const iconEl = document.getElementById("mmStateIcon");
        const textEl = document.getElementById("mmStateText");
        const btn    = document.getElementById("mmGenBtn");
        if (!state) return;

        state.classList.add("mm-state-visible");
        state.querySelector("h3").textContent = title;

        if (spinning) {
            iconEl.outerHTML = `<span class="nexora-spinner large" id="mmStateIcon" style="margin-bottom: 8px;"></span>`;
        } else if (!document.getElementById("mmStateIcon")?.classList?.contains("fa-solid")) {
            // restore icon element if a spinner had replaced it previously
            const span = document.getElementById("mmStateIcon");
            if (span) span.outerHTML = `<i class="fa-solid ${icon}" id="mmStateIcon" style="font-size: 32px; color: var(--accent, #b18fcf); margin-bottom: 8px;"></i>`;
        } else {
            iconEl.className = `fa-solid ${icon}`;
        }

        if (textEl) textEl.innerHTML = text || "Generates a fully-expanded radial map to help you explore document topics at a glance — click any bubble for details.";
        if (btn) {
            btn.style.display = showButton ? "inline-flex" : "none";
            btn.innerHTML = `<i class="fa-solid ${buttonIcon}"></i> ${buttonLabel}`;
            btn.disabled = false;
        }
    }

    function _hideState() {
        const state = document.getElementById("mmState");
        if (state) state.classList.remove("mm-state-visible");
    }

    function _setSubtitle(txt) {
        const el = document.getElementById("mmSubtitle");
        if (el) el.innerHTML = txt;
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

    function _findParent(root, id, parent = null) {
        if (!root) return null;
        if (root.id === id) return parent;
        for (const c of (root.children || [])) {
            const found = _findParent(c, id, root);
            if (found) return found;
        }
        return null;
    }

    // ─────────────────────────────────────────────────────────────────────
    // Color assignment: each theme gets a palette color; concepts/details
    // inherit their theme's color so a whole branch reads as one color.
    // ─────────────────────────────────────────────────────────────────────
    function _assignColors(node) {
        if (node.type === "root") {
            node._color = ROOT_COLOR;
            (node.children || []).forEach((theme, i) => {
                theme._color = PALETTE[i % PALETTE.length];
                _paintBranch(theme, theme._color);
            });
        }
    }

    function _paintBranch(node, color) {
        node._color = color;
        (node.children || []).forEach(child => _paintBranch(child, color));
    }

    // ─────────────────────────────────────────────────────────────────────
    // Radial layout
    // ─────────────────────────────────────────────────────────────────────
    function _layoutRadial() {
        _positions = new Map();
        if (!_tree) return { minX: 0, minY: 0, maxX: 0, maxY: 0 };

        let minX = -RADIUS_BY_TYPE.root, minY = -RADIUS_BY_TYPE.root;
        let maxX = RADIUS_BY_TYPE.root, maxY = RADIUS_BY_TYPE.root;

        function track(x, y, r) {
            minX = Math.min(minX, x - r); maxX = Math.max(maxX, x + r);
            minY = Math.min(minY, y - r); maxY = Math.max(maxY, y + r);
        }

        _positions.set(_tree.id, { x: 0, y: 0, angle: 0 });
        track(0, 0, RADIUS_BY_TYPE.root);

        const themes = _tree.children || [];
        const n = themes.length || 1;

        themes.forEach((theme, i) => {
            const angle = (-Math.PI / 2) + (i * 2 * Math.PI / n);
            const tx = Math.cos(angle) * R_THEME;
            const ty = Math.sin(angle) * R_THEME;
            _positions.set(theme.id, { x: tx, y: ty, angle });
            track(tx, ty, RADIUS_BY_TYPE.theme);

            const concepts = theme.children || [];
            const cN = concepts.length;
            if (cN === 0) return;

            const span = Math.max(MIN_GAP_CONCEPT * (cN - 1), 0);
            const startA = angle - span / 2;

            concepts.forEach((concept, j) => {
                const cAngle = cN === 1 ? angle : startA + j * MIN_GAP_CONCEPT;
                const cx = tx + Math.cos(cAngle) * R_CONCEPT;
                const cy = ty + Math.sin(cAngle) * R_CONCEPT;
                _positions.set(concept.id, { x: cx, y: cy, angle: cAngle });
                track(cx, cy, RADIUS_BY_TYPE.concept);

                const details = concept.children || [];
                const dN = details.length;
                if (dN === 0) return;

                const dSpan = Math.max(MIN_GAP_DETAIL * (dN - 1), 0);
                const dStart = cAngle - dSpan / 2;

                details.forEach((detail, k) => {
                    const dAngle = dN === 1 ? cAngle : dStart + k * MIN_GAP_DETAIL;
                    const dx = cx + Math.cos(dAngle) * R_DETAIL;
                    const dy = cy + Math.sin(dAngle) * R_DETAIL;
                    _positions.set(detail.id, { x: dx, y: dy, angle: dAngle });
                    track(dx, dy, RADIUS_BY_TYPE.detail);
                });
            });
        });

        return { minX, minY, maxX, maxY };
    }

    // ─────────────────────────────────────────────────────────────────────
    // Pan + zoom (using D3 zoom behavior)
    // ─────────────────────────────────────────────────────────────────────
    function _attachPanZoom() {
        if (_zoomBehaviorAttached) return;
        if (typeof d3 === "undefined") {
            setTimeout(_attachPanZoom, 100);
            return;
        }
        _zoomBehaviorAttached = true;

        const svgEl = document.getElementById("mmSvg");
        if (!svgEl) return;

        _zoomBehavior = d3.zoom()
            .scaleExtent([0.15, 3])
            .on("zoom", (event) => {
                const g = svgEl.querySelector("#mmZoomGroup");
                if (g) {
                    g.setAttribute("transform", event.transform.toString());
                }
                _panZoom.scale = event.transform.k;
                _panZoom.x = event.transform.x;
                _panZoom.y = event.transform.y;
            });

        d3.select(svgEl)
            .call(_zoomBehavior)
            .on("dblclick.zoom", null);
    }

    function _zoom(factor) {
        const svgEl = document.getElementById("mmSvg");
        if (!svgEl || !_zoomBehavior) return;
        d3.select(svgEl).transition().duration(300).call(_zoomBehavior.scaleBy, factor);
    }

    function _resetZoom() {
        const svgEl = document.getElementById("mmSvg");
        if (!svgEl || !_zoomBehavior) return;
        d3.select(svgEl).transition().duration(400).call(_zoomBehavior.transform, d3.zoomIdentity);
    }

    // ─────────────────────────────────────────────────────────────────────
    // SVG rendering
    // ─────────────────────────────────────────────────────────────────────
    function _render() {
        const svg = document.getElementById("mmSvg");
        const scrollArea = document.getElementById("mmScrollArea");
        if (!svg || !_tree || !scrollArea) return;

        const PAD = 70;
        const { minX, minY, maxX, maxY } = _layoutRadial();
        const w = Math.max((maxX - minX) + PAD * 2, 600);
        const h = Math.max((maxY - minY) + PAD * 2, 500);
        const offX = -minX + PAD;
        const offY = -minY + PAD;

        const viewportW = scrollArea.clientWidth  || 800;
        const viewportH = scrollArea.clientHeight || 600;

        svg.setAttribute("width",  viewportW);
        svg.setAttribute("height", viewportH);
        svg.setAttribute("viewBox", `0 0 ${viewportW} ${viewportH}`);

        // Center the natural-size drawing inside the viewport, then pan/zoom
        // is applied on top of that via the #mmZoomGroup transform.
        const baseX = (viewportW - w) / 2;
        const baseY = (viewportH - h) / 2;

        let edgesHtml = "";
        let nodesHtml = "";

        function px(id) { const p = _positions.get(id); return p ? p.x + offX : offX; }
        function py(id) { const p = _positions.get(id); return p ? p.y + offY : offY; }

        function drawEdge(parent, child) {
            const x1 = px(parent.id), y1 = py(parent.id);
            const x2 = px(child.id),  y2 = py(child.id);
            const col = child._color || "#999";
            edgesHtml += `<line x1="${x1}" y1="${y1}" x2="${x2}" y2="${y2}"
                                 stroke="${col}66" stroke-width="2"
                                 class="mm-edge" data-id="${child.id}"/>`;
        }

        function drawNode(node) {
            const x = px(node.id), y = py(node.id);
            const r = RADIUS_BY_TYPE[node.type] || 22;
            const fz = FONT_BY_TYPE[node.type] || 10;
            const col = node._color || "#b18fcf";
            const isSelected = node.id === _selectedId;
            const hasMore = !!node.has_children && (node.children || []).length === 0;

            const fill   = node.type === "root" ? col : col + (isSelected ? "cc" : "55");
            const stroke = node.type === "root" ? "#22252e" : (isSelected ? col : col + "aa");
            const sw     = isSelected ? 3 : (node.type === "root" ? 2 : 1.5);
            const txtCol = node.type === "root" ? "#ffffff" : "#1f2430";

            const label = _wrapLabel(node.label || "", r);
            const fixedLines = label.map((line, idx) => {
                const dyFirst = -((label.length - 1) / 2) * (fz + 2);
                return `<tspan x="${x}" dy="${idx === 0 ? dyFirst : (fz + 2)}">${_svgText(line)}</tspan>`;
            }).join("");

            let badge = "";
            if (hasMore) {
                badge = `<circle cx="${x + r * 0.7}" cy="${y - r * 0.7}" r="7"
                               fill="#ffffff" stroke="${col}" stroke-width="1.5"/>
                         <text x="${x + r * 0.7}" y="${y - r * 0.7 + 1}"
                               fill="${col}" font-size="10" font-weight="700"
                               text-anchor="middle" dominant-baseline="central"
                               style="pointer-events:none">+</text>`;
            }

            nodesHtml += `
              <g class="mm-node ${isSelected ? 'mm-node-selected' : ''}"
                 data-id="${node.id}" data-type="${node.type}"
                 style="cursor:pointer"
                 onclick="MindMap._nodeClick('${node.id}')">
                <circle cx="${x}" cy="${y}" r="${r}"
                        fill="${fill}" stroke="${stroke}" stroke-width="${sw}"/>
                <text x="${x}" y="${y}"
                      fill="${txtCol}" font-size="${fz}" font-weight="700"
                      text-anchor="middle" dominant-baseline="central"
                      font-family="Sora, system-ui, sans-serif"
                      style="pointer-events:none">
                  ${fixedLines}
                </text>
                ${badge}
              </g>`;
        }

        const themes = _tree.children || [];
        themes.forEach(theme => {
            drawEdge(_tree, theme);
            (theme.children || []).forEach(concept => {
                drawEdge(theme, concept);
                (concept.children || []).forEach(detail => drawEdge(concept, detail));
            });
        });

        drawNode(_tree);
        themes.forEach(theme => {
            drawNode(theme);
            (theme.children || []).forEach(concept => {
                drawNode(concept);
                (concept.children || []).forEach(detail => drawNode(detail));
            });
        });

        svg.innerHTML = `
          <g id="mmZoomGroup" transform="translate(${_panZoom.x},${_panZoom.y}) scale(${_panZoom.scale})">
            <g transform="translate(${baseX},${baseY})">
              ${edgesHtml}
              ${nodesHtml}
            </g>
          </g>`;
    }

    function _wrapLabel(str, r) {
        const maxLines = r >= 50 ? 3 : 2;
        const maxCharsPerLine = Math.max(Math.floor(r / 3.6), 6);
        const words = str.split(/\s+/);
        const lines = [];
        let cur = "";
        for (const word of words) {
            const candidate = cur ? cur + " " + word : word;
            if (candidate.length > maxCharsPerLine && cur) {
                lines.push(cur);
                cur = word;
            } else {
                cur = candidate;
            }
            if (lines.length === maxLines - 1 && cur.length > maxCharsPerLine) {
                cur = cur.slice(0, maxCharsPerLine - 1) + "…";
                break;
            }
        }
        if (cur) lines.push(cur);
        if (lines.length > maxLines) {
            const truncated = lines.slice(0, maxLines);
            truncated[maxLines - 1] = truncated[maxLines - 1].replace(/.{1,2}$/, "…");
            return truncated;
        }
        return lines.length ? lines : [str];
    }

    function _svgText(str) {
        return str.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
    }

    // ─────────────────────────────────────────────────────────────────────
    // Node click: select + open panel
    // ─────────────────────────────────────────────────────────────────────
    function _nodeClick(id) {
        const node = _findNode(_tree, id);
        if (!node) return;

        _selectedId = id;
        _render();
        _openPanel(node);
    }

    // ─────────────────────────────────────────────────────────────────────
    // Side panel
    // ─────────────────────────────────────────────────────────────────────
    function _openPanel(node) {
        _selectedNode = node;
        const panel = document.getElementById("mmPanel");
        if (!panel) return;
        panel.classList.add("has-node");
        panel.classList.remove("collapsed");

        const icon = document.querySelector("#mmPanelToggle i");
        if (icon) icon.className = "fa-solid fa-chevron-right";

        const typeLabels = { root: "Document Root", theme: "Theme", concept: "Concept", detail: "Deep Insight" };
        const color = node._color || "#b18fcf";

        document.getElementById("mmPanelType").innerHTML =
            `<span class="mm-type-badge" style="background:${color}22;color:${color}">${typeLabels[node.type] || node.type}</span>`;
        document.getElementById("mmPanelLabel").textContent = node.label;
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
        } else {
            srcEl.innerHTML = "";
        }

        const drillBtn  = document.getElementById("mmDrillBtn");
        const drillLoad = document.getElementById("mmDrillLoader");
        const canDrillMore = !!node.has_children && (node.children || []).length === 0;
        drillBtn.style.display  = canDrillMore ? "flex" : "none";
        drillLoad.style.display = "none";
    }

    function _closePanel() {
        _selectedId   = null;
        _selectedNode = null;
        const panel = document.getElementById("mmPanel");
        if (!panel) return;
        panel.classList.remove("has-node");
        panel.classList.add("collapsed");
        _render();
    }

    function togglePanel() {
        const panel = document.getElementById("mmPanel");
        if (!panel) return;
        const isCollapsed = panel.classList.toggle("collapsed");
        const icon = document.querySelector("#mmPanelToggle i");
        if (icon) {
            icon.className = isCollapsed ? "fa-solid fa-chevron-left" : "fa-solid fa-chevron-right";
        }
    }

    // ─────────────────────────────────────────────────────────────────────
    // Lazy load one more ring of children ("Deep Dive" button in panel)
    // ─────────────────────────────────────────────────────────────────────
    async function _drillSelected() {
        const node = _selectedNode;
        if (!node || !node.has_children || (node.children || []).length > 0) return;

        const drillBtn = document.getElementById("mmDrillBtn");
        const loader   = document.getElementById("mmDrillLoader");
        if (drillBtn) drillBtn.style.display = "none";
        if (loader)   { loader.style.display = "flex"; loader.innerHTML = `<span class="nexora-spinner inline"></span> Exploring…`; }

        const parent = _findParent(_tree, node.id);

        try {
            const sid = (typeof session_id !== "undefined" && session_id) ? session_id : _sessionId;
            const res = await fetch("/mindmap/drill", {
                method:  "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({
                    session_id:   sid,
                    node_id:      node.id,
                    label:        node.label,
                    parent_label: parent ? parent.label : "Document",
                    files:        _files,
                }),
            });

            if (!res.ok) throw new Error(await res.text());
            const data = await res.json();

            node.children     = data.children || [];
            node.has_children = node.children.length === 0;

            _paintBranch(node, node._color);
            _render();
            _openPanel(node);
        } catch (err) {
            console.error("[MindMap drill]", err);
            if (loader) loader.innerHTML =
                `<span style="color:#ef476f"><i class="fa-solid fa-circle-exclamation"></i> Error loading</span>`;
        } finally {
            if (loader && loader.innerHTML.includes("Exploring"))
                loader.style.display = "none";
        }
    }

    // ─────────────────────────────────────────────────────────────────────
    // Public: generate — the SINGLE entry point that builds the map.
    // Lives only inside the state overlay's button; there is no second
    // trigger anywhere else in the UI.
    // ─────────────────────────────────────────────────────────────────────
    async function generate() {
        _files = _getFiles();
        const sid = (typeof session_id !== "undefined" && session_id) ? session_id : "mm-session";
        _sessionId = sid;

        _tree = null; _selectedId = null; _selectedNode = null;
        _panZoom = { scale: 1, x: 0, y: 0 };
        const panel = document.getElementById("mmPanel");
        if (panel) {
            panel.classList.remove("has-node");
            panel.classList.add("collapsed");
        }

        _showState({
            spinning: true,
            title: "Building your mind map",
            text: "This may take 30–60 seconds…",
            showButton: false,
        });

        try {
            const res = await fetch("/mindmap", {
                method:  "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ session_id: sid, files: _files }),
            });

            if (!res.ok) {
                const e = await res.json().catch(() => ({}));
                _showState({
                    icon: "fa-circle-exclamation",
                    title: "Couldn't build the mind map",
                    text: e.message || res.statusText || "Something went wrong. Please try again.",
                    showButton: true,
                    buttonLabel: "Try Again",
                    buttonIcon: "fa-rotate",
                });
                return;
            }

            const data = await res.json();

            if (!data || !data.children || data.children.length === 0) {
                _showState({
                    icon: "fa-circle-info",
                    title: "No content found",
                    text: data?.summary || "No indexed documents found for the selected files.",
                    showButton: true,
                    buttonLabel: "Try Again",
                    buttonIcon: "fa-rotate",
                });
                return;
            }

            _tree = data;
            _assignColors(_tree);

            _hideState();
            _render();

            const svgEl = document.getElementById("mmSvg");
            if (svgEl && _zoomBehavior) {
                d3.select(svgEl).call(_zoomBehavior.transform, d3.zoomIdentity);
            }

            const themeCount   = (_tree.children || []).length;
            const conceptCount = (_tree.children || []).reduce((s, t) => s + (t.children?.length || 0), 0);
            
            const themeStat = document.getElementById("mmStatThemes");
            if (themeStat) themeStat.textContent = themeCount;
            const conceptStat = document.getElementById("mmStatConcepts");
            if (conceptStat) conceptStat.textContent = conceptCount;

            const docsListEl = document.getElementById("mmDocsList");
            if (docsListEl) {
                docsListEl.textContent = _files.length > 0 ? "Sources: " + _files.join(", ") : "No sources selected";
            }

            _setSubtitle(`${themeCount} themes · ${conceptCount} concepts · click any node to explore`);
            document.getElementById("mmTitle").textContent = _tree.label || "Mind Map";

        } catch (err) {
            console.error("[MindMap]", err);
            _showState({
                icon: "fa-triangle-exclamation",
                title: "Network error",
                text: "Please check your connection and try again.",
                showButton: true,
                buttonLabel: "Try Again",
                buttonIcon: "fa-rotate",
            });
        }
    }

    // ─────────────────────────────────────────────────────────────────────
    // Public: open / close
    // ─────────────────────────────────────────────────────────────────────
    async function _loadD3() {
        if (typeof d3 !== "undefined") return;
        return new Promise((resolve, reject) => {
            const s   = document.createElement("script");
            s.src     = "https://cdnjs.cloudflare.com/ajax/libs/d3/7.9.0/d3.min.js";
            s.onload  = () => resolve();
            s.onerror = () => reject(new Error("D3 load fail"));
            document.head.appendChild(s);
        });
    }

    async function open() {
        _inject();
        if (typeof d3 === "undefined") {
            try {
                await _loadD3();
            } catch (e) {
                console.error("[MindMap] D3 load failed:", e);
            }
        }
        document.getElementById("mmOverlay").classList.add("mm-active");
        _isOpen = true;
        if (_tree) {
            _hideState();
            _render();
        } else {
            _showState();
        }
    }

    function close() {
        const o = document.getElementById("mmOverlay");
        if (o) o.classList.remove("mm-active");
        _isOpen = false;
        if (typeof backToChatbot === "function") backToChatbot();
    }

    window.addEventListener("resize", () => {
        if (_isOpen && _tree) _render();
    });

    return { open, close, generate, _drillSelected, _closePanel, _nodeClick, _zoom, _resetZoom, togglePanel };

})();
