/**
 * cluster.js — Nexora AI  |  Cluster Universe
 * =============================================
 * Fetches topic clusters from /cluster_universe (backed by Chroma + LLM)
 * and renders them as interactive cards inside a modal overlay.
 *
 * Usage (from index.html button):
 *   onclick="ClusterUniverse.open()"
 *
 * The modal HTML + CSS are injected at runtime so no changes to index.html
 * are required beyond adding the trigger button.
 */

const ClusterUniverse = (() => {

    // ── Palette — mirrors Knowledge Graph / Nexora accent tokens ──────────
    const CLUSTER_COLORS = [
        "#b18fcf", "#7ec8e3", "#f4a261", "#57cc99",
        "#e76f51", "#a8dadc", "#ffd166", "#c77dff",
    ];

    // ── State ─────────────────────────────────────────────────────────────
    let _modalInjected = false;
    let _lastFetchKey  = null;        // cache-busting: "file1.pdf|file2.pdf"

    // ── Inject modal + styles once ────────────────────────────────────────
    function _inject() {
        if (_modalInjected) return;
        _modalInjected = true;

        /* ---- CSS ---- */
        const style = document.createElement("style");
        style.textContent = `
        /* ── Overlay ── */
        #cuOverlay {
            display: none;
            position: fixed;
            inset: 0;
            background: rgba(0, 0, 0, 0.72);
            backdrop-filter: blur(16px);
            -webkit-backdrop-filter: blur(16px);
            z-index: 1200;
            align-items: center;
            justify-content: center;
        }
        #cuOverlay.cu-active { display: flex; }

        /* ── Modal shell ── */
        #cuModal {
            background: var(--bg-glass-deep);
            border: 1px solid var(--border);
            border-radius: 24px;
            width: min(920px, 96vw);
            max-height: 88vh;
            display: flex;
            flex-direction: column;
            overflow: hidden;
            box-shadow: 0 24px 64px rgba(0,0,0,0.6), 0 0 40px var(--accent-glow-soft);
            backdrop-filter: blur(30px);
            -webkit-backdrop-filter: blur(30px);
            animation: cu-slide-up 0.22s cubic-bezier(0.22, 1, 0.36, 1);
            font-family: var(--font-body), sans-serif;
        }

        @keyframes cu-slide-up {
            from { opacity: 0; transform: translateY(18px) scale(0.98); }
            to   { opacity: 1; transform: translateY(0)    scale(1);    }
        }

        /* ── Header ── */
        #cuHeader {
            display: flex;
            align-items: center;
            justify-content: space-between;
            padding: 18px 24px 14px;
            border-bottom: 1px solid var(--border);
            flex-shrink: 0;
        }
        #cuHeader h2 {
            margin: 0;
            font-size: 1.15rem;
            font-weight: 700;
            font-family: var(--font-display), sans-serif;
            background: var(--grad-text);
            -webkit-background-clip: text;
            -webkit-text-fill-color: transparent;
            background-clip: text;
            display: flex;
            align-items: center;
            gap: 10px;
        }
        #cuHeader h2 i {
            color: var(--accent);
            -webkit-text-fill-color: initial;
        }

        #cuStatBar {
            font-size: 0.76rem;
            color: var(--text-muted);
            margin-left: 10px;
            font-weight: 400;
            -webkit-text-fill-color: initial;
        }

        .cu-header-right { display: flex; align-items: center; gap: 10px; }

        #cuRefreshBtn {
            background: var(--accent-bg);
            border: 1px solid var(--accent-glow);
            color: var(--accent);
            border-radius: 10px;
            padding: 6px 14px;
            font-size: 0.8rem;
            cursor: pointer;
            display: flex;
            align-items: center;
            gap: 6px;
            transition: all 0.2s var(--transition);
            font-family: var(--font-display), sans-serif;
            font-weight: 600;
        }
        #cuRefreshBtn:hover {
            background: var(--accent-glow-soft);
            border-color: var(--border-hover);
        }
        #cuRefreshBtn:disabled { opacity: 0.45; cursor: not-allowed; }

        #cuCloseBtn {
            background: none;
            border: none;
            color: var(--text-secondary);
            font-size: 1.1rem;
            cursor: pointer;
            padding: 4px 8px;
            border-radius: 50%;
            transition: all 0.2s;
            line-height: 1;
            display: flex;
            align-items: center;
            justify-content: center;
        }
        #cuCloseBtn:hover {
            color: var(--accent-light);
            background: var(--accent-bg);
        }
        body.light-mode #cuCloseBtn:hover {
            color: var(--accent);
        }

        /* ── Body ── */
        #cuBody {
            padding: 22px 24px;
            overflow-y: auto;
            flex: 1;
        }

        /* ── Loading / empty states ── */
        .cu-state {
            display: flex;
            flex-direction: column;
            align-items: center;
            justify-content: center;
            gap: 14px;
            padding: 60px 0;
            color: var(--text-muted);
            font-size: 0.9rem;
            text-align: center;
        }
        .cu-state i { font-size: 2.2rem; opacity: 0.4; }

        /* ── Cluster grid ── */
        #cuGrid {
            display: grid;
            grid-template-columns: repeat(auto-fill, minmax(260px, 1fr));
            gap: 16px;
        }

        /* ── Cluster card ── */
        .cu-card {
            background: var(--bg-glass);
            border: 1px solid var(--border);
            border-radius: 16px;
            padding: 18px 18px 14px;
            display: flex;
            flex-direction: column;
            gap: 10px;
            transition: transform 0.18s, box-shadow 0.18s, border-color 0.18s;
            cursor: default;
        }
        .cu-card:hover {
            transform: translateY(-3px);
            box-shadow: 0 12px 40px rgba(0,0,0,0.3);
            border-color: var(--border-hover);
            background: var(--bg-glass-deep);
        }

        /* colour accent strip on left edge */
        .cu-card { border-left-width: 4px; border-left-style: solid; }

        .cu-card-top {
            display: flex;
            align-items: flex-start;
            justify-content: space-between;
            gap: 8px;
        }

        .cu-label {
            font-size: 0.95rem;
            font-weight: 700;
            font-family: var(--font-display), sans-serif;
            color: var(--text-primary);
            line-height: 1.3;
        }

        .cu-chunk-badge {
            font-size: 0.7rem;
            padding: 3px 8px;
            border-radius: 20px;
            white-space: nowrap;
            font-weight: 600;
            opacity: 0.85;
            flex-shrink: 0;
        }

        .cu-summary {
            font-size: 0.8rem;
            color: var(--text-secondary);
            line-height: 1.5;
        }

        /* keyword pills */
        .cu-keywords {
            display: flex;
            flex-wrap: wrap;
            gap: 5px;
        }
        .cu-kw {
            font-size: 0.7rem;
            padding: 3px 9px;
            border-radius: 20px;
            border: 1px solid var(--border);
            background: var(--bg-glass);
            color: var(--text-secondary);
            opacity: 0.8;
        }

        /* source file tags */
        .cu-sources {
            display: flex;
            flex-wrap: wrap;
            gap: 5px;
            margin-top: 2px;
        }
        .cu-src-tag {
            font-size: 0.68rem;
            padding: 2px 8px;
            border-radius: 6px;
            background: var(--bg-glass);
            color: var(--text-muted);
            border: 1px solid var(--border);
            display: flex;
            align-items: center;
            gap: 4px;
        }
        .cu-src-tag i { font-size: 0.6rem; opacity: 0.7; }

        /* ── Scrollbar ── */
        #cuBody::-webkit-scrollbar { width: 5px; }
        #cuBody::-webkit-scrollbar-track { background: transparent; }
        #cuBody::-webkit-scrollbar-thumb {
            background: var(--accent-glow);
            border-radius: 99px;
        }
        `;
        document.head.appendChild(style);

        /* ---- HTML ---- */
        const overlay = document.createElement("div");
        overlay.id = "cuOverlay";
        overlay.innerHTML = `
        <div id="cuModal">
            <div id="cuHeader">
                <h2>
                    <i class="fa-solid fa-circle-nodes"></i>
                    Cluster Universe
                    <span id="cuStatBar"></span>
                </h2>
                <div class="cu-header-right">
                    <button id="cuRefreshBtn" onclick="ClusterUniverse.load(true)">
                        <i class="fa-solid fa-rotate"></i> Re-cluster
                    </button>
                    <button id="cuCloseBtn" onclick="ClusterUniverse.close()">
                        <i class="fa-solid fa-xmark"></i>
                    </button>
                </div>
            </div>
            <div id="cuBody">
                <div class="cu-state" id="cuStateMsg">
                    <i class="fa-solid fa-circle-nodes"></i>
                    <span>Click <strong>Re-cluster</strong> to analyse your documents.</span>
                </div>
                <div id="cuGrid" style="display:none"></div>
            </div>
        </div>`;
        (document.getElementById("featureWorkspace") || document.body).appendChild(overlay);

        // Close on backdrop click
        overlay.addEventListener("click", e => {
            if (e.target === overlay) ClusterUniverse.close();
        });
    }

    // ── Helpers ──────────────────────────────────────────────────────────
    function _getSelectedFiles() {
        const checked = [];
        document.querySelectorAll("#documentCheckboxList input:checked")
            .forEach(cb => checked.push(cb.value));
        return checked;
    }

    function _setState(html) {
        document.getElementById("cuStateMsg").innerHTML = html;
        document.getElementById("cuStateMsg").style.display = "flex";
        document.getElementById("cuGrid").style.display = "none";
    }

    function _setStatBar(stats) {
        if (!stats) { document.getElementById("cuStatBar").textContent = ""; return; }
        document.getElementById("cuStatBar").textContent =
            `· ${stats.cluster_count} clusters · ${stats.total_chunks} chunks · ${stats.sources_scanned} sources`;
    }

    // ── Render clusters ───────────────────────────────────────────────────
    function _render(clusters) {
        const grid = document.getElementById("cuGrid");
        grid.innerHTML = "";

        if (!clusters || clusters.length === 0) {
            _setState(`<i class="fa-solid fa-inbox"></i><span>No clusters found. Try uploading more documents.</span>`);
            return;
        }

        clusters.forEach((cl, idx) => {
            const color = CLUSTER_COLORS[idx % CLUSTER_COLORS.length];

            // keyword pills
            const kwHTML = (cl.keywords || []).map(kw =>
                `<span class="cu-kw" style="color:${color};border-color:${color}40">${kw}</span>`
            ).join("");

            // source file tags
            const srcHTML = (cl.sources || []).map(s =>
                `<span class="cu-src-tag"><i class="fa-solid fa-file-lines"></i>${s}</span>`
            ).join("");

            const card = document.createElement("div");
            card.className = "cu-card";
            card.style.borderLeftColor = color;
            card.innerHTML = `
                <div class="cu-card-top">
                    <div class="cu-label">${cl.label}</div>
                    <span class="cu-chunk-badge"
                          style="background:${color}22;color:${color}">
                        ${cl.chunks} chunk${cl.chunks !== 1 ? "s" : ""}
                    </span>
                </div>
                <div class="cu-summary">${cl.summary}</div>
                ${kwHTML ? `<div class="cu-keywords">${kwHTML}</div>` : ""}
                ${srcHTML ? `<div class="cu-sources">${srcHTML}</div>` : ""}`;
            grid.appendChild(card);
        });

        document.getElementById("cuStateMsg").style.display = "none";
        grid.style.display = "grid";
    }

    // ── Public API ────────────────────────────────────────────────────────
    async function load(force = false) {
        const files    = _getSelectedFiles();
        const cacheKey = files.join("|");

        // Skip re-fetch if same file set and not forced
        if (!force && cacheKey === _lastFetchKey) return;
        _lastFetchKey = cacheKey;

        const btn = document.getElementById("cuRefreshBtn");
        if (btn) { btn.disabled = true; btn.innerHTML = `<i class="fa-solid fa-spinner fa-spin"></i> Clustering…`; }

        _setState(`<i class="fa-solid fa-spinner fa-spin"></i><span>Analysing documents and building clusters…</span>`);
        _setStatBar(null);

        try {
            const sessionId = (typeof session_id !== "undefined" && session_id) ? session_id : "cu-session";

            const res = await fetch("/cluster_universe", {
                method:  "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ files, session_id: sessionId }),
            });

            if (!res.ok) {
                const err = await res.json().catch(() => ({}));
                _setState(`<i class="fa-solid fa-circle-exclamation"></i><span>Error: ${err.message || res.statusText}</span>`);
                return;
            }

            const data = await res.json();
            _setStatBar(data.stats);
            _render(data.clusters);

        } catch (e) {
            console.error("[CU] fetch error:", e);
            _setState(`<i class="fa-solid fa-triangle-exclamation"></i><span>Network error. Please try again.</span>`);
        } finally {
            if (btn) { btn.disabled = false; btn.innerHTML = `<i class="fa-solid fa-rotate"></i> Re-cluster`; }
        }
    }

    function open() {
        _inject();
        document.getElementById("cuOverlay").classList.add("cu-active");
        // Auto-load on first open
        if (_lastFetchKey === null) load(true);
    }

    function close() {
        const overlay = document.getElementById("cuOverlay");
        if (overlay) overlay.classList.remove("cu-active");
        if (typeof backToChatbot === "function") backToChatbot();
    }

    /** Call this after a document is deleted so next open re-fetches. */
    function invalidate() {
        _lastFetchKey = null;
    }

    return { open, close, load, invalidate };

})();
