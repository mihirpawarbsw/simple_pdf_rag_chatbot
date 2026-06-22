/**
 * graph.js — Nexora AI Interactive Knowledge Graph
 * Uses D3.js v7 force simulation with zoom, drag, tooltips, and filtering.
 *
 * FIXES:
 *  1. invalidate() — clears cached graph data and auto-refreshes if modal is open.
 *     Call KnowledgeGraph.invalidate() after any upload or delete so the graph
 *     always reflects the current document set.
 *  2. Sends ALL selected documents via POST for multi-doc graph.
 *  3. Edge + node tooltips show which PDF(s) each relation/entity came from.
 *  4. D3 lazy-load preserved (fixes "d3 is not defined").
 */

const KnowledgeGraph = (() => {

  // ── State ──────────────────────────────────────────────────────────────────
  let svg, simulation, container;
  let allNodes = [], allEdges = [];
  let nodeElements, linkElements, labelElements;
  let isOpen = false;

  // ── Colour palette per node type ──────────────────────────────────────────
  const TYPE_COLORS = {
    entity:   { fill: "#a78bfa", stroke: "#7c3aed", glow: "#a78bfa" },
    concept:  { fill: "#67e8f9", stroke: "#0891b2", glow: "#67e8f9" },
    document: { fill: "#fbbf24", stroke: "#d97706", glow: "#fbbf24" },
    default:  { fill: "#94a3b8", stroke: "#475569", glow: "#94a3b8" },
  };

  // ── Inject modal HTML + styles once ───────────────────────────────────────
  function _injectModal() {
    if (document.getElementById("kgModal")) return;

    const style = document.createElement("style");
    style.textContent = `
      #kgModal {
        display: none; position: fixed; inset: 0; z-index: 9999;
        background: rgba(0, 0, 0, 0.72);
        backdrop-filter: blur(16px);
        -webkit-backdrop-filter: blur(16px);
        animation: kgFadeIn .25s ease;
      }
      #kgModal.open { display: flex; flex-direction: column; }
      @keyframes kgFadeIn { from { opacity:0; } to { opacity:1; } }

      #kgPanel {
        margin: auto;
        width: min(96vw, 1100px);
        height: min(90vh, 780px);
        background: var(--bg-glass-deep);
        border: 1px solid var(--border);
        border-radius: 20px;
        box-shadow: 0 30px 80px rgba(0,0,0,.7), 0 0 60px var(--accent-glow-soft);
        display: flex; flex-direction: column; overflow: hidden;
        position: relative;
      }

      #kgHeader {
        display: flex; align-items: center; gap: 12px;
        padding: 16px 22px;
        border-bottom: 1px solid var(--border);
        background: var(--accent-bg);
        flex-shrink: 0;
      }
      #kgHeader h3 {
        margin: 0; font-size: 1.1rem; font-weight: 700;
        background: var(--grad-text);
        -webkit-background-clip: text;
        -webkit-text-fill-color: transparent;
        background-clip: text;
        letter-spacing: .02em;
        flex: 1;
      }
      #kgHeader .kg-icon { color: var(--accent); font-size: 1.2rem; }

      #kgStats {
        display: flex; gap: 10px; font-size: .72rem;
        color: var(--text-secondary);
      }
      #kgStats span {
        background: var(--bg-glass);
        border: 1px solid var(--border);
        border-radius: 20px; padding: 3px 10px;
      }
      #kgStats b { color: var(--accent); }

      #kgLegend {
        display: flex; gap: 14px; align-items: center;
        padding: 8px 22px;
        border-bottom: 1px solid var(--border);
        flex-wrap: wrap; flex-shrink: 0;
        background: var(--bg-glass);
      }
      .kg-legend-item {
        display: flex; align-items: center; gap: 6px;
        font-size: .72rem; color: var(--text-secondary); cursor: pointer;
        padding: 3px 8px; border-radius: 12px; transition: background .2s;
        user-select: none;
      }
      .kg-legend-item:hover { background: var(--accent-bg); }
      .kg-legend-item.muted { opacity: .35; }
      .kg-legend-dot {
        width: 10px; height: 10px; border-radius: 50%; flex-shrink: 0;
      }

      #kgZoomControls {
        position: absolute; bottom: 18px; right: 18px; z-index: 10;
        display: flex; flex-direction: column; gap: 6px;
      }
      .kg-zoom-btn {
        width: 32px; height: 32px;
        background: var(--bg-glass-deep);
        border: 1px solid var(--border);
        border-radius: 8px; color: var(--text-primary);
        font-size: .9rem; cursor: pointer;
        display: flex; align-items: center; justify-content: center;
        transition: background .2s, border-color .2s, color .2s;
        backdrop-filter: blur(8px);
        -webkit-backdrop-filter: blur(8px);
      }
      .kg-zoom-btn:hover { background: var(--accent-bg); border-color: var(--accent); color: var(--accent-light); }
      body.light-mode .kg-zoom-btn:hover { color: var(--accent); }

      #kgSvgWrap {
        flex: 1; overflow: hidden; position: relative;
      }
      #kgSvgWrap svg { width: 100%; height: 100%; }

      #kgLoader {
        position: absolute; inset: 0; display: flex;
        flex-direction: column; gap: 14px;
        align-items: center; justify-content: center;
        color: var(--text-secondary); font-size: .88rem;
        background: var(--bg-glass-deep);
      }
      #kgLoader.hidden { display: none; }

      #kgTooltip {
        position: fixed; z-index: 10000;
        background: var(--bg-glass-deep);
        border: 1px solid var(--border);
        border-radius: 10px; padding: 10px 14px;
        font-size: .78rem; color: var(--text-primary);
        pointer-events: none; opacity: 0;
        transition: opacity .15s;
        max-width: 280px; box-shadow: 0 8px 24px rgba(0,0,0,.5), 0 0 10px var(--accent-glow-soft);
        line-height: 1.5;
        backdrop-filter: blur(12px);
        -webkit-backdrop-filter: blur(12px);
      }
      #kgTooltip.visible { opacity: 1; }
      #kgTooltip .kg-tt-label { font-weight: 600; color: var(--accent); margin-bottom: 4px; }
      #kgTooltip .kg-tt-type  { font-size: .7rem; color: var(--text-muted); text-transform: uppercase; letter-spacing: .06em; }
      #kgTooltip .kg-tt-rel   { font-style: italic; color: var(--accent-light); margin-top: 4px; }
      body.light-mode #kgTooltip .kg-tt-rel { color: var(--accent-deep); }
      #kgTooltip .kg-tt-conn  { color: var(--text-secondary); margin-top: 4px; }
      #kgTooltip .kg-tt-src   {
        margin-top: 7px; padding-top: 7px;
        border-top: 1px solid var(--border);
        font-size: .7rem; color: var(--text-muted);
      }
      #kgTooltip .kg-tt-src b { color: var(--accent); }
      #kgTooltip .kg-tt-src span { display: block; color: var(--text-secondary); margin-top: 2px; }

      #kgEmpty {
        position: absolute; inset: 0; display: none;
        flex-direction: column; gap: 10px;
        align-items: center; justify-content: center;
        color: var(--text-muted); font-size: .88rem;
      }
      #kgEmpty i { font-size: 2.5rem; color: var(--accent-glow-soft); }
      #kgEmpty.show { display: flex; }

      #kgCloseBtn {
        background: transparent;
        border: none; cursor: pointer;
        color: var(--text-muted); font-size: 1.2rem;
        padding: 4px 8px; border-radius: 6px;
        transition: color .2s, background .2s;
        display: flex; align-items: center; justify-content: center;
      }
      #kgCloseBtn:hover { color: var(--accent); background: var(--accent-bg); }

      .kg-node { cursor: pointer; }
      .kg-node circle { transition: r .15s, filter .15s; }
      .kg-node:hover circle { filter: brightness(1.2); }
      body.light-mode .kg-node circle { filter: drop-shadow(0 2px 4px rgba(0,0,0,0.1)); }
      body.light-mode .kg-node:hover circle { filter: drop-shadow(0 4px 8px rgba(0,0,0,0.15)) brightness(1.05); }
      .kg-link {
        stroke-opacity: .45;
        transition: stroke-opacity .15s;
      }
      .kg-link.highlighted { stroke-opacity: 1; stroke-width: 2px; }
      .kg-label {
        font-size: 10px; fill: var(--text-primary);
        pointer-events: none;
        paint-order: stroke;
        stroke: var(--bg-base);
        stroke-width: 3px;
        font-family: 'Sora', sans-serif;
      }
      .kg-edge-label {
        font-size: 8.5px; fill: var(--accent);
        pointer-events: none;
        paint-order: stroke;
        stroke: var(--bg-base);
        stroke-width: 3px;
        font-family: 'Sora', sans-serif;
        opacity: 0; transition: opacity .2s;
      }
      .kg-edge-label.show { opacity: 1; }
    `;
    document.head.appendChild(style);

    const modal = document.createElement("div");
    modal.id = "kgModal";
    modal.innerHTML = `
      <div id="kgPanel">
        <div id="kgHeader">
          <i class="fa-solid fa-diagram-project kg-icon"></i>
          <h3>Knowledge Graph</h3>
          <div id="kgStats">
            <span><b id="kgStatNodes">0</b> nodes</span>
            <span><b id="kgStatEdges">0</b> edges</span>
          </div>
          <button id="kgCloseBtn" onclick="KnowledgeGraph.close()" title="Close">
            <i class="fa-solid fa-xmark"></i>
          </button>
        </div>

        <div id="kgLegend">
          <span style="font-size:.72rem;color:#6b7280;margin-right:4px;">Filter:</span>
          <div class="kg-legend-item" data-type="entity"   onclick="KnowledgeGraph.toggleType('entity')">
            <div class="kg-legend-dot" style="background:#a78bfa;box-shadow:0 0 6px #a78bfa"></div> Entity
          </div>
          <div class="kg-legend-item" data-type="concept"  onclick="KnowledgeGraph.toggleType('concept')">
            <div class="kg-legend-dot" style="background:#67e8f9;box-shadow:0 0 6px #67e8f9"></div> Concept
          </div>
          <div class="kg-legend-item" data-type="document" onclick="KnowledgeGraph.toggleType('document')">
            <div class="kg-legend-dot" style="background:#fbbf24;box-shadow:0 0 6px #fbbf24"></div> Document
          </div>
        </div>

        <div id="kgSvgWrap">
          <div id="kgLoader" class="hidden"><span class="nexora-spinner large"></span><span>Extracting knowledge graph…</span></div>
          <div id="kgEmpty"><i class="fa-solid fa-circle-nodes"></i><span>No graph data for selected documents.</span></div>
          <div id="kgStateMsg" style="position: absolute; inset: 0; display: flex; flex-direction: column; align-items: center; justify-content: center; text-align: center; padding: 40px 20px; gap: 12px; color: var(--text-secondary); background: var(--bg-glass-deep); z-index: 10;">
             <i class="fa-solid fa-diagram-project" style="font-size: 32px; color: var(--accent); margin-bottom: 8px;"></i>
             <h3 style="margin: 0; font-family: var(--font-display); font-size: 1.1rem; color: var(--text-primary);">Knowledge Graph</h3>
             <p style="margin: 0 0 12px; font-size: 0.88rem; max-width: 480px; line-height: 1.5;">
                 Extracts and displays a network of connected concepts, entities, and documents to show how information is linked.
             </p>
             <button onclick="KnowledgeGraph.generateGraph()" style="background: var(--grad-button); color: var(--swal-btn-text); border: none; padding: 10px 24px; border-radius: 12px; font-family: var(--font-display); font-size: 0.88rem; font-weight: 700; cursor: pointer; transition: transform 0.2s, box-shadow 0.2s; box-shadow: 0 4px 12px var(--accent-glow); display: inline-flex; align-items: center; gap: 8px;">
                 <i class="fa-solid fa-diagram-project"></i> Generate Graph
             </button>
          </div>
        </div>

        <div id="kgZoomControls">
          <button class="kg-zoom-btn" onclick="KnowledgeGraph.zoom(1.3)" title="Zoom In"><i class="fa-solid fa-plus"></i></button>
          <button class="kg-zoom-btn" onclick="KnowledgeGraph.zoom(0.7)" title="Zoom Out"><i class="fa-solid fa-minus"></i></button>
          <button class="kg-zoom-btn" onclick="KnowledgeGraph.resetZoom()" title="Fit"><i class="fa-solid fa-maximize"></i></button>
        </div>
      </div>

      <div id="kgTooltip"></div>
    `;
    (document.getElementById("featureWorkspace") || document.body).appendChild(modal);

    modal.addEventListener("click", e => {
      if (e.target === modal) KnowledgeGraph.close();
    });
  }

  // ── Get selected documents from sidebar checkboxes ────────────────────────
  function _getSelectedFiles() {
    const checks = document.querySelectorAll("#documentCheckboxList input[type=checkbox]:checked");
    return Array.from(checks).map(c => c.value).filter(Boolean);
  }

  // ── Current zoom transform ─────────────────────────────────────────────────
  let _zoomBehavior, _currentTransform = { k: 1, x: 0, y: 0 };

  // ── Core fetch + render (shared by open() and invalidate()) ────────────────
  async function _fetchAndRender() {
    const loader  = document.getElementById("kgLoader");
    const empty   = document.getElementById("kgEmpty");
    const stateMsg = document.getElementById("kgStateMsg");
    const svgWrap = document.getElementById("kgSvgWrap");
    if (!loader || !svgWrap) return;

    if (stateMsg) stateMsg.style.display = "none";
    // Reset UI to loading state
    loader.classList.remove("hidden");
    empty.classList.remove("show");
    svgWrap.querySelectorAll("svg").forEach(el => el.remove());
    if (simulation) { simulation.stop(); simulation = null; }

    const files     = _getSelectedFiles();
    const sessionId = (typeof session_id !== "undefined" && session_id) ? session_id : "kg-session";

    try {
      const res  = await fetch("/knowledge_graph", {
        method:  "POST",
        headers: { "Content-Type": "application/json" },
        body:    JSON.stringify({ session_id: sessionId, files }),
      });
      const data = await res.json();
      loader.classList.add("hidden");

      if (!data.nodes || data.nodes.length === 0) {
        allNodes = []; allEdges = [];
        empty.classList.add("show");
        document.getElementById("kgStatNodes").textContent = "0";
        document.getElementById("kgStatEdges").textContent = "0";
        return;
      }

      document.getElementById("kgStatNodes").textContent = data.stats?.node_count  ?? data.nodes.length;
      document.getElementById("kgStatEdges").textContent = data.stats?.edge_count  ?? data.edges.length;

      allNodes = data.nodes;
      allEdges = data.edges;

      _buildGraph(
        JSON.parse(JSON.stringify(allNodes)),
        JSON.parse(JSON.stringify(allEdges)),
        svgWrap
      );

    } catch (err) {
      console.error("[KG] fetch error:", err);
      loader.classList.add("hidden");
      empty.classList.add("show");
    }
  }

  // ── Build D3 graph ─────────────────────────────────────────────────────────
  function _buildGraph(nodes, edges, svgWrap) {
    svgWrap.innerHTML = "";

    const W = svgWrap.clientWidth  || 800;
    const H = svgWrap.clientHeight || 500;

    svg = d3.select(svgWrap).append("svg")
      .attr("viewBox", `0 0 ${W} ${H}`)
      .attr("preserveAspectRatio", "xMidYMid meet");

    const defs = svg.append("defs");
    defs.append("marker")
      .attr("id", "kg-arrow")
      .attr("viewBox", "0 -4 8 8")
      .attr("refX", 14).attr("refY", 0)
      .attr("markerWidth", 6).attr("markerHeight", 6)
      .attr("orient", "auto")
      .append("path")
        .attr("d", "M0,-4L8,0L0,4")
        .attr("fill", "var(--border-hover)");

    Object.entries(TYPE_COLORS).forEach(([type]) => {
      const f = defs.append("filter").attr("id", `glow-${type}`).attr("x", "-50%").attr("y", "-50%").attr("width", "200%").attr("height", "200%");
      f.append("feGaussianBlur").attr("stdDeviation", "3").attr("result", "blur");
      const merge = f.append("feMerge");
      merge.append("feMergeNode").attr("in", "blur");
      merge.append("feMergeNode").attr("in", "SourceGraphic");
    });

    container = svg.append("g").attr("class", "kg-container");

    _zoomBehavior = d3.zoom()
      .scaleExtent([0.15, 4])
      .on("zoom", (event) => {
        _currentTransform = event.transform;
        container.attr("transform", event.transform);
      });
    svg.call(_zoomBehavior);

    simulation = d3.forceSimulation(nodes)
      .force("link",   d3.forceLink(edges).id(d => d.id).distance(d => {
        if (d.source.type === "document" || d.target.type === "document") return 140;
        return 90;
      }).strength(0.5))
      .force("charge", d3.forceManyBody().strength(-320))
      .force("center", d3.forceCenter(W / 2, H / 2))
      .force("collide", d3.forceCollide().radius(d => _nodeRadius(d) + 8));

    linkElements = container.append("g").selectAll(".kg-link")
      .data(edges).enter().append("line")
      .attr("class", "kg-link")
      .attr("stroke", "var(--border)")
      .attr("stroke-width", 1.2)
      .attr("marker-end", "url(#kg-arrow)");

    const edgeLabelElements = container.append("g").selectAll(".kg-edge-label")
      .data(edges).enter().append("text")
      .attr("class", "kg-edge-label")
      .attr("text-anchor", "middle")
      .text(d => d.relation);

    const nodeGroups = container.append("g").selectAll(".kg-node")
      .data(nodes).enter().append("g")
      .attr("class", "kg-node")
      .call(d3.drag()
        .on("start", (event, d) => {
          if (!event.active) simulation.alphaTarget(0.3).restart();
          d.fx = d.x; d.fy = d.y;
        })
        .on("drag",  (event, d) => { d.fx = event.x; d.fy = event.y; })
        .on("end",   (event, d) => {
          if (!event.active) simulation.alphaTarget(0);
          d.fx = null; d.fy = null;
        })
      );

    nodeGroups.append("circle")
      .attr("r",      d => _nodeRadius(d))
      .attr("fill",   d => (TYPE_COLORS[d.type] || TYPE_COLORS.default).fill)
      .attr("stroke", d => (TYPE_COLORS[d.type] || TYPE_COLORS.default).stroke)
      .attr("stroke-width", 1.5)
      .attr("filter", d => `url(#glow-${d.type in TYPE_COLORS ? d.type : "default"})`);

    nodeGroups.append("text")
      .attr("class", "kg-label")
      .attr("dy", d => _nodeRadius(d) + 11)
      .attr("text-anchor", "middle")
      .text(d => _truncate(d.label, 22));

    // ── Tooltip helpers ───────────────────────────────────────────────────────
    const tooltip = document.getElementById("kgTooltip");

    function _srcHtml(sources) {
      if (!sources || !sources.length) return "";
      return `<div class="kg-tt-src">
        <b>&#128196; PDF source${sources.length > 1 ? "s" : ""}:</b>
        <span>${sources.join("<br>")}</span>
      </div>`;
    }

    // Node tooltips
    nodeGroups
      .on("mouseover", (event, d) => {
        const connCount = edges.filter(e =>
          (e.source.id || e.source) === d.id || (e.target.id || e.target) === d.id
        ).length;
        tooltip.innerHTML = `
          <div class="kg-tt-type">${d.type}</div>
          <div class="kg-tt-label">${d.label}</div>
          <div class="kg-tt-conn">${connCount} connection${connCount !== 1 ? "s" : ""}</div>
          ${_srcHtml(d.sources)}
        `;
        tooltip.classList.add("visible");
        linkElements.classed("highlighted", e =>
          (e.source.id || e.source) === d.id || (e.target.id || e.target) === d.id
        );
        edgeLabelElements.classed("show", e =>
          (e.source.id || e.source) === d.id || (e.target.id || e.target) === d.id
        );
      })
      .on("mousemove", (event) => {
        tooltip.style.left = (event.clientX + 14) + "px";
        tooltip.style.top  = (event.clientY - 10) + "px";
      })
      .on("mouseout", () => {
        tooltip.classList.remove("visible");
        linkElements.classed("highlighted", false);
        edgeLabelElements.classed("show", false);
      });

    // Edge tooltips — with PDF source traceability
    linkElements
      .on("mouseover", (event, d) => {
        const srcLabel = d.source.label || d.source;
        const tgtLabel = d.target.label || d.target;
        tooltip.innerHTML = `
          <div class="kg-tt-type">relationship</div>
          <div class="kg-tt-label">${d.relation}</div>
          <div class="kg-tt-rel">${srcLabel} &rarr; ${tgtLabel}</div>
          ${_srcHtml(d.sources)}
        `;
        tooltip.classList.add("visible");
      })
      .on("mousemove", (event) => {
        tooltip.style.left = (event.clientX + 14) + "px";
        tooltip.style.top  = (event.clientY - 10) + "px";
      })
      .on("mouseout", () => tooltip.classList.remove("visible"));

    simulation.on("tick", () => {
      linkElements
        .attr("x1", d => d.source.x).attr("y1", d => d.source.y)
        .attr("x2", d => d.target.x).attr("y2", d => d.target.y);
      edgeLabelElements
        .attr("x", d => (d.source.x + d.target.x) / 2)
        .attr("y", d => (d.source.y + d.target.y) / 2);
      nodeGroups.attr("transform", d => `translate(${d.x},${d.y})`);
    });

    nodeElements = nodeGroups;
    labelElements = edgeLabelElements;
  }

  function _nodeRadius(d) {
    if (d.type === "document") return 16;
    if (d.type === "entity")   return 8 + Math.min(d.size * 1.5, 8);
    return 6 + Math.min(d.size, 6);
  }

  function _truncate(s, n) {
    return s.length > n ? s.slice(0, n - 1) + "…" : s;
  }

  // ── Public API ─────────────────────────────────────────────────────────────

  async function open() {
    _injectModal();
    if (typeof d3 === "undefined") await _loadD3();

    const modal = document.getElementById("kgModal");
    modal.classList.add("open");
    isOpen = true;

    const stateMsg = document.getElementById("kgStateMsg");
    if (stateMsg) stateMsg.style.display = "flex";
    const loader = document.getElementById("kgLoader");
    if (loader) loader.classList.add("hidden");
    const empty = document.getElementById("kgEmpty");
    if (empty) empty.classList.remove("show");
  }

  async function generateGraph() {
    const stateMsg = document.getElementById("kgStateMsg");
    if (stateMsg) stateMsg.style.display = "none";
    await _fetchAndRender();
  }

  function close() {
    const modal = document.getElementById("kgModal");
    if (modal) modal.classList.remove("open");
    if (simulation) simulation.stop();
    const tooltip = document.getElementById("kgTooltip");
    if (tooltip) tooltip.classList.remove("visible");
    isOpen = false;
    if (typeof backToChatbot === "function") backToChatbot();
  }

  /**
   * invalidate() — call this after any upload or delete.
   * Clears cached graph data. If the modal is currently open,
   * immediately re-fetches and re-renders the updated graph.
   */
  async function invalidate() {
    allNodes = [];
    allEdges = [];
    if (!isOpen) return;                         // modal closed — nothing to do
    if (typeof d3 === "undefined") await _loadD3();
    await _fetchAndRender();
  }

  const _hiddenTypes = new Set();
  function toggleType(type) {
    const item = document.querySelector(`.kg-legend-item[data-type="${type}"]`);
    if (_hiddenTypes.has(type)) {
      _hiddenTypes.delete(type);
      if (item) item.classList.remove("muted");
    } else {
      _hiddenTypes.add(type);
      if (item) item.classList.add("muted");
    }
    if (!nodeElements) return;
    nodeElements.style("display", d => _hiddenTypes.has(d.type) ? "none" : null);
    linkElements && linkElements.style("display", d => {
      const sType = d.source.type || "";
      const tType = d.target.type || "";
      return (_hiddenTypes.has(sType) || _hiddenTypes.has(tType)) ? "none" : null;
    });
  }

  function zoom(factor) {
    if (!svg || !_zoomBehavior) return;
    svg.transition().duration(300).call(_zoomBehavior.scaleBy, factor);
  }

  function resetZoom() {
    if (!svg || !_zoomBehavior) return;
    svg.transition().duration(400).call(_zoomBehavior.transform, d3.zoomIdentity);
  }

  async function _loadD3() {
    return new Promise((resolve, reject) => {
      const s   = document.createElement("script");
      s.src     = "https://cdnjs.cloudflare.com/ajax/libs/d3/7.9.0/d3.min.js";
      s.onload  = resolve;
      s.onerror = reject;
      document.head.appendChild(s);
    });
  }

  window.addEventListener("load", () => {
    if (typeof d3 === "undefined") _loadD3().catch(() => {});
    _injectModal();
  });

  return { open, close, invalidate, toggleType, zoom, resetZoom, generateGraph };

})();
