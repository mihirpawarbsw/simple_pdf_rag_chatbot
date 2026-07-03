/**
 * pulse_grid.js  —  Nexora AI  |  PulseGrid
 * ===================================================================
 * Renders two live, data-driven views inside featureWorkspace:
 *   🎯 StatCards       — animated gauge / big-number cards + a
 *                         cross-source sparkline (built from regex,
 *                         zero synthesis LLM calls).
 *   🗺️ CredibilityGrid — topic × source-tier heatmap (pure counting).
 * Also provides a PDF download button that posts to /pulse_grid/export_pdf.
 *
 * Include in index.html:
 *   <script src="{{ url_for('static', filename='js/pulse_grid.js') }}" defer></script>
 *
 * Trigger from Explore Features dropdown:
 *   onclick="openFeatureInWorkspace('PulseGrid', () => PulseGrid.open())"
 *
 * Add to app.py:
 *   from pulse_grid import pulse_grid_bp
 *   app.register_blueprint(pulse_grid_bp)
 */

const PulseGrid = (() => {
    "use strict";

    // ── State ─────────────────────────────────────────────────────────────
    let _injected     = false;
    let _generating    = false;
    let _currentReport = null;   // last fully loaded ReportJSON
    let originalBackToChatbot = null;

    // ── Topic color palette (matches PDF export colors) ─────────────────────
    const TOPIC_COLORS = ["#0d9488", "#f59e0b", "#7c3aed", "#3b82f6", "#ec4899"];

    const TIER_ICONS = {
        news:     '<i class="fa-solid fa-newspaper"></i>',
        research: '<i class="fa-solid fa-flask"></i>',
        blog:     '<i class="fa-solid fa-pen-nib"></i>',
        forum:    '<i class="fa-solid fa-comments"></i>',
        social:   '<i class="fa-brands fa-x-twitter"></i>',
        other:    '<i class="fa-solid fa-link"></i>',
        default:  '<i class="fa-solid fa-link"></i>'
    };

    const PROGRESS_STEPS = [
        { pct: 10, icon: 'fa-file-lines',      label: "Fetching document chunks from ChromaDB…" },
        { pct: 26, icon: 'fa-layer-group',     label: "Extracting key topics with AI…" },
        { pct: 48, icon: 'fa-earth-americas',  label: "Searching the web via Tavily…" },
        { pct: 70, icon: 'fa-magnifying-glass-chart', label: "Regex-extracting statistics…" },
        { pct: 86, icon: 'fa-table-cells',     label: "Counting sources into the credibility grid…" },
        { pct: 96, icon: 'fa-flag-checkered',  label: "Finalising report…" }
    ];

    // ── Inject once ───────────────────────────────────────────────────────
    function _inject() {
        if (_injected) return;
        _injected = true;

        // ── CSS ──────────────────────────────────────────────────────────
        const style = document.createElement("style");
        style.textContent = `
        #pgOverlay {
            display: none;
            position: fixed;
            inset: 0;
            background: rgba(0,0,0,0.72);
            backdrop-filter: blur(16px);
            -webkit-backdrop-filter: blur(16px);
            z-index: 1200;
            align-items: center;
            justify-content: center;
        }
        #pgOverlay.pg-active { display: flex; }

        #pgModal {
            background: var(--bg-glass-deep);
            border: 1px solid var(--border);
            border-radius: 24px;
            width: min(1080px, 96vw);
            max-height: 90vh;
            display: flex;
            flex-direction: column;
            overflow: hidden;
            box-shadow: 0 24px 64px rgba(0,0,0,0.6), 0 0 40px var(--accent-glow-soft);
            backdrop-filter: blur(30px);
            -webkit-backdrop-filter: blur(30px);
            animation: pg-slide-up 0.22s cubic-bezier(0.22,1,0.36,1);
            font-family: var(--font-body), sans-serif;
        }
        @keyframes pg-slide-up {
            from { opacity:0; transform:translateY(18px) scale(0.98); }
            to   { opacity:1; transform:translateY(0) scale(1);        }
        }

        #featureWorkspace #pgOverlay {
            position: absolute !important;
            inset: 0 !important;
            width: 100% !important;
            height: 100% !important;
            background: transparent !important;
            backdrop-filter: none !important;
            -webkit-backdrop-filter: none !important;
            padding: 0 !important;
            margin: 0 !important;
            z-index: 1 !important;
            box-shadow: none !important;
            border: none !important;
        }
        #featureWorkspace #pgOverlay.pg-active {
            display: flex !important;
            align-items: stretch !important;
            justify-content: stretch !important;
        }
        #featureWorkspace #pgModal {
            position: absolute !important;
            inset: 0 !important;
            width: 100% !important;
            height: 100% !important;
            max-width: none !important;
            max-height: none !important;
            border: none !important;
            border-radius: 0 !important;
            box-shadow: none !important;
            backdrop-filter: none !important;
            -webkit-backdrop-filter: none !important;
            background: var(--bg-glass-deep) !important;
            animation: none !important;
        }

        /* ── Header ── */
        #pgHeader {
            display: flex;
            align-items: center;
            justify-content: space-between;
            gap: 12px;
            padding: 16px 22px;
            border-bottom: 1px solid var(--border);
            background: var(--accent-bg);
            flex-shrink: 0;
        }
        #pgHeader h3 {
            margin: 0;
            font-size: 1.1rem;
            font-weight: 700;
            background: var(--grad-text);
            -webkit-background-clip: text;
            -webkit-text-fill-color: transparent;
            background-clip: text;
            letter-spacing: .02em;
            flex: 1;
            font-family: var(--font-display), sans-serif;
            display: flex;
            align-items: center;
            gap: 8px;
        }
        #pgHeader h3 i { -webkit-text-fill-color: initial; color: var(--accent); }
        #pgHeader .pg-icon { color: var(--accent); font-size: 1.2rem; }
        #pgHeader .hdr-subtitle {
            font-size: 0.72rem !important;
            font-style: italic !important;
            color: var(--text-secondary, #9aa0b4) !important;
            display: block !important;
            margin-top: 2px !important;
        }
        .pg-header-right { margin-left: auto; display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
        .pg-header-btn {
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
            transition: all 0.2s;
            font-family: var(--font-display), sans-serif;
            font-weight: 600;
        }
        .pg-header-btn:hover { background: var(--accent-glow-soft); border-color: var(--border-hover); }
        .pg-header-btn:disabled { opacity: 0.45; cursor: not-allowed; }
        .pg-header-btn.primary {
            background: var(--grad-button) !important;
            color: var(--swal-btn-text) !important;
            border: none;
            box-shadow: 0 4px 14px var(--accent-glow-soft);
        }
        .pg-header-btn.primary:hover:not(:disabled) {
            filter: brightness(1.08);
            transform: translateY(-1px);
            box-shadow: 0 6px 18px var(--accent-glow);
        }
        #pgCloseBtn {
            background: transparent; border: none; cursor: pointer;
            color: var(--text-muted); font-size: 1.2rem;
            padding: 6px 10px; border-radius: 50%;
            transition: color .2s, background .2s;
            display: flex; align-items: center; justify-content: center;
        }
        #pgCloseBtn:hover { color: var(--accent-light) !important; background: var(--accent-bg) !important; }
        body.light-mode #pgCloseBtn:hover { color: var(--accent) !important; }

        #pgStats { display: flex; gap: 10px; font-size: .72rem; color: var(--text-secondary); align-items: center; }
        #pgStats span { background: var(--bg-glass); border: 1px solid var(--border); border-radius: 20px; padding: 3px 10px; }

        /* ── Body ── */
        #pgBody { padding: 22px 26px; overflow-y: auto; flex: 1; scroll-behavior: smooth; }
        #pgBody::-webkit-scrollbar { width: 5px; }
        #pgBody::-webkit-scrollbar-track { background: transparent; }
        #pgBody::-webkit-scrollbar-thumb { background: var(--accent-glow); border-radius: 99px; }

        .pg-state {
            display: flex; flex-direction: column; align-items: center; justify-content: center;
            gap: 14px; padding: 60px 20px; text-align: center; color: var(--text-secondary);
        }
        .pg-state i { font-size: 2.4rem; color: var(--accent); opacity: 0.7; margin-bottom: 4px; }

        /* ── Progress ── */
        #pgProgressWrap {
            margin: 25px auto; max-width: 620px; padding: 24px;
            background: var(--bg-glass-deep); border: 1px solid var(--border);
            border-radius: 20px; box-shadow: 0 12px 40px rgba(0,0,0,0.3); display: none;
        }
        .pg-progress-track { height: 4px; background: var(--accent-glow-soft); border-radius: 99px; overflow: hidden; margin-bottom: 22px; }
        .pg-progress-fill { height: 100%; width: 0%; background: var(--grad-button); border-radius: 99px; transition: width 0.7s cubic-bezier(0.4,0,0.2,1); }
        .pg-progress-stream { display: flex; flex-direction: column; gap: 16px; position: relative; padding-left: 28px; text-align: left; }
        .pg-progress-stream::before {
            content: ""; position: absolute; left: 9px; top: 4px; bottom: 4px; width: 2px;
            border-left: 2px dashed rgba(255,255,255,0.15);
        }
        body.light-mode .pg-progress-stream::before { border-left-color: rgba(0,0,0,0.15); }
        .pg-stream-step { display: flex; align-items: center; gap: 14px; position: relative; font-size: 12px; color: var(--text-muted); transition: color 0.4s ease; }
        .pg-stream-node {
            position: absolute; left: -28px; width: 20px; height: 20px; border-radius: 50%;
            background: var(--bg-glass-deep); border: 2px solid var(--border);
            display: flex; align-items: center; justify-content: center;
            font-size: 10px; color: var(--text-muted); z-index: 2; transition: all 0.4s ease;
        }
        .pg-stream-step.active { color: var(--accent); font-weight: 600; }
        .pg-stream-step.active .pg-stream-node { border-color: var(--accent); background: var(--accent-bg); color: var(--accent-light); box-shadow: 0 0 10px var(--accent-glow); }
        .pg-stream-step.done { color: var(--text-primary); }
        .pg-stream-step.done .pg-stream-node { border-color: #22c55e; background: rgba(34,197,94,0.15); color: #22c55e; }

        /* ── Freshness / meta strip ── */
        .pg-meta-card {
            border: 1px solid var(--border); border-radius: 16px; padding: 16px 20px;
            margin-bottom: 20px; background: var(--bg-glass); animation: pg-fade-in 0.4s ease;
        }
        @keyframes pg-fade-in { from{opacity:0;transform:translateY(10px)} to{opacity:1;transform:translateY(0)} }
        .pg-freshness {
            display: inline-flex; align-items: center; gap: 6px; font-size: 11px; padding: 3px 10px;
            border-radius: 20px; border: 1px solid var(--border); color: var(--text-muted);
            background: var(--bg-glass); margin-top: 2px; margin-right: 8px;
        }
        .pg-freshness i { color: #22c55e; font-size: 8px; }

        /* ── Topic context strip ── */
        .pg-topic-strip { display: flex; flex-wrap: wrap; gap: 10px; margin-bottom: 22px; }
        .pg-topic-chip {
            border: 1px solid var(--border); border-radius: 12px; padding: 10px 14px;
            background: var(--bg-glass); flex: 1; min-width: 200px;
            border-left-width: 3px; border-left-style: solid;
        }
        .pg-topic-chip .tc-title { font-size: 12.5px; font-weight: 700; color: var(--text-primary); margin-bottom: 3px; }
        .pg-topic-chip .tc-context { font-size: 11.5px; color: var(--text-secondary); line-height: 1.4; }

        /* ── Section heading ── */
        .pg-section-heading {
            display: flex; align-items: center; gap: 10px; margin: 24px 0 18px;
            font-family: var(--font-display), sans-serif; font-size: 13px; font-weight: 700;
            color: var(--text-secondary); text-transform: uppercase; letter-spacing: .07em;
            padding-bottom: 8px; border-bottom: 1px solid var(--border);
        }
        .pg-section-heading i { color: var(--accent); font-size: 14px; }

        /* ── Stat cards ── */
        .pg-cards-grid {
            display: grid; grid-template-columns: repeat(auto-fill, minmax(230px, 1fr));
            gap: 14px; margin-bottom: 8px;
        }
        .pg-card {
            border: 1px solid var(--border); border-left-width: 3px; border-left-style: solid;
            border-radius: 14px; padding: 16px 18px; background: var(--bg-glass);
            animation: pg-fade-in 0.4s ease; transition: border-color 0.15s, transform 0.15s;
        }
        .pg-card:hover { border-color: var(--border-hover); transform: translateY(-2px); }
        .pg-card-topic { font-size: 10.5px; font-weight: 700; text-transform: uppercase; letter-spacing: .05em; margin-bottom: 8px; }
        .pg-card-value-row { display: flex; align-items: baseline; gap: 8px; margin-bottom: 8px; }
        .pg-card-value { font-size: 26px; font-weight: 800; color: var(--text-primary); font-family: var(--font-display), sans-serif; }
        .pg-card-trend { font-size: 13px; font-weight: 700; }
        .pg-card-trend.up { color: #22c55e; }
        .pg-card-trend.down { color: #ef4444; }
        .pg-card-spark { margin: 4px 0 10px; height: 26px; }
        .pg-card-context { font-size: 11px; color: var(--text-secondary); line-height: 1.4; margin-bottom: 8px; }
        .pg-card-src {
            font-size: 10.5px; color: var(--text-secondary); text-decoration: none;
            display: inline-flex; align-items: center; gap: 5px; opacity: 0.85;
        }
        .pg-card-src:hover { opacity: 1; color: var(--accent); text-decoration: underline; }

        /* ── Credibility heatmap table ── */
        .pg-grid-wrap { overflow-x: auto; margin-bottom: 8px; }
        table.pg-heat-table { border-collapse: separate; border-spacing: 4px; width: 100%; min-width: 520px; }
        table.pg-heat-table th {
            font-size: 10.5px; font-weight: 700; text-transform: uppercase; letter-spacing: .05em;
            color: var(--text-secondary); padding: 6px 8px; text-align: center;
        }
        table.pg-heat-table th:first-child { text-align: left; }
        table.pg-heat-table td.pg-heat-topic {
            font-size: 12px; font-weight: 700; color: var(--text-primary); padding: 10px 12px;
            background: var(--bg-glass); border-radius: 8px; border-left: 3px solid transparent;
            white-space: nowrap;
        }
        table.pg-heat-table td.pg-heat-cell {
            text-align: center; font-size: 13px; font-weight: 700; padding: 10px 6px;
            border-radius: 8px; color: #0f172a; min-width: 52px;
        }
        table.pg-heat-table td.pg-heat-total {
            text-align: center; font-size: 12px; font-weight: 800; color: var(--text-primary);
            background: var(--accent-bg); border-radius: 8px; padding: 10px 8px;
        }
        .pg-heat-legend { display:flex; align-items:center; gap:8px; font-size:11px; color:var(--text-muted); margin-top:10px; }
        .pg-heat-legend .swatch { width:14px; height:14px; border-radius:4px; display:inline-block; }

        /* ── History panel ── */
        .pg-history-item {
            display: flex; align-items: center; gap: 12px; padding: 10px 14px;
            border: 1px solid var(--border); border-radius: 12px; margin-bottom: 8px;
            background: var(--bg-glass); cursor: pointer; transition: all 0.15s;
        }
        .pg-history-item:hover { border-color: var(--border-hover); background: var(--accent-bg); }
        .pg-history-meta { flex: 1; min-width: 0; }
        .pg-history-title { font-size: 13px; font-weight: 600; color: var(--text-primary); margin-bottom: 3px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
        .pg-history-sub { font-size: 11px; color: var(--text-muted); }
        .pg-history-del { background: none; border: none; color: var(--text-muted); cursor: pointer; padding: 4px 6px; border-radius: 6px; font-size: 12px; }
        .pg-history-del:hover { color: #ef4444; background: rgba(239,68,68,.1); }

        /* ── Tabs ── */
        .pg-tabs { display: flex; gap: 4px; margin-bottom: 18px; border-bottom: 1px solid var(--border); }
        .pg-tab-btn {
            background: none; border: none; padding: 8px 16px; font-size: 13px; font-weight: 600;
            color: var(--text-muted); cursor: pointer; border-bottom: 2px solid transparent;
            margin-bottom: -1px; transition: all 0.18s; font-family: var(--font-display), sans-serif;
            display: flex; align-items: center; gap: 6px;
        }
        .pg-tab-btn:hover { color: var(--text-primary); }
        .pg-tab-btn.active { color: var(--accent); border-bottom-color: var(--accent); }
        `;
        document.head.appendChild(style);

        // ── HTML ──────────────────────────────────────────────────────────
        const overlay = document.createElement("div");
        overlay.id = "pgOverlay";
        overlay.innerHTML = `
        <div id="pgModal">
            <div id="pgHeader">
                <div class="pg-header-left" style="display:flex;align-items:center;gap:10px">
                    <i class="fa-solid fa-chart-pie pg-icon"></i>
                    <div>
                        <h3 style="margin:0;">StatSonar</h3>
                        <span class="hdr-subtitle">Live stat cards & source-credibility heatmaps, pulled fresh from the web.</span>
                    </div>
                </div>
                <div class="pg-header-right">
                    <div id="pgStats"><span>No report generated</span></div>
                    <button class="pg-header-btn" id="pgHistoryBtn" onclick="PulseGrid._toggleTab('history')">
                        <i class="fa-solid fa-clock-rotate-left"></i> History
                    </button>
                    <button class="pg-header-btn primary" id="pgRegenerateBtn" onclick="PulseGrid.generate()" style="display:none">
                        <i class="fa-solid fa-wand-magic-sparkles"></i> Regenerate
                    </button>
                    <button class="pg-header-btn" id="pgDownloadBtn" onclick="PulseGrid.downloadPDF()" style="display:none">
                        <i class="fa-solid fa-file-arrow-down"></i> Download PDF
                    </button>
                    <button id="pgCloseBtn" onclick="PulseGrid.close()" title="Close StatSonar" style="margin-left:8px;background:transparent;border:none;color:var(--text-secondary);cursor:pointer;font-size:18px;display:inline-flex;align-items:center;justify-content:center;padding:4px;transition:color 0.2s;">
                        <i class="fa-solid fa-xmark"></i>
                    </button>
                </div>
            </div>

            <div id="pgBody">

                <div id="pgProgressWrap">
                    <div style="font-family:var(--font-display);font-size:13px;font-weight:700;color:var(--text-primary);margin-bottom:16px;display:flex;align-items:center;gap:8px;">
                        <i class="fa-solid fa-circle-notch fa-spin" style="color:var(--accent);"></i>
                        <span>Building StatSonar...</span>
                    </div>
                    <div class="pg-progress-track"><div class="pg-progress-fill" id="pgProgressFill"></div></div>
                    <div id="pgProgressStepsRow" class="pg-progress-stream"></div>
                </div>

                <div id="pgTabsRow" class="pg-tabs" style="display:none">
                    <button class="pg-tab-btn active" id="pgTabReport" onclick="PulseGrid._toggleTab('report')">
                        <i class="fa-solid fa-chart-pie"></i> Report
                    </button>
                    <button class="pg-tab-btn" id="pgTabHistory" onclick="PulseGrid._toggleTab('history')">
                        <i class="fa-solid fa-clock-rotate-left"></i> History
                    </button>
                </div>

                <div id="pgReportPanel">
                    <div class="pg-state" id="pgIdleState">
                        <i class="fa-solid fa-chart-pie" style="font-size:32px;color:var(--accent);margin-bottom:8px;"></i>
                        <h3 style="margin:0;font-family:var(--font-display);font-size:1.1rem;color:var(--text-primary)">Doc Topics → Live Stats & Credibility</h3>
                        <p style="margin:0 0 12px;font-size:.88rem;max-width:520px;line-height:1.5">
                            Select documents from the sidebar, then click <strong>Build Grid</strong> to pull real current statistics
                            and see which topics are backed by credible sources vs. only forum chatter.
                        </p>
                        <button id="pgGenerateBtn" onclick="PulseGrid.generate()"
                            style="background:var(--grad-button);color:var(--swal-btn-text);border:none;padding:8px 16px;border-radius:10px;font-family:var(--font-display);font-size:0.8rem;font-weight:600;cursor:pointer;box-shadow:0 4px 12px var(--accent-glow);display:inline-flex;align-items:center;gap:8px">
                            <i class="fa-solid fa-wand-magic-sparkles"></i> Build Grid
                        </button>
                    </div>
                    <div id="pgReportContent" style="display:none"></div>
                </div>

                <div id="pgHistoryPanel" style="display:none">
                    <div class="pg-section-heading"><i class="fa-solid fa-clock-rotate-left"></i> Saved Reports</div>
                    <div id="pgHistoryList"><div class="pg-state"><i class="fa-solid fa-circle-notch fa-spin"></i><span>Loading…</span></div></div>
                </div>

            </div>
        </div>`;

        (document.getElementById("featureWorkspace") || document.body).appendChild(overlay);

        overlay.addEventListener("click", e => { if (e.target === overlay) PulseGrid.close(); });
    }

    // ── Helpers ───────────────────────────────────────────────────────────
    function _getSelectedFiles() {
        const checked = [];
        document.querySelectorAll("#documentCheckboxList input:checked").forEach(cb => checked.push(cb.value));
        return checked;
    }

    function _topicColor(topicId, topics) {
        const idx = (topics || []).findIndex(t => t.id === topicId);
        return TOPIC_COLORS[idx >= 0 ? idx % TOPIC_COLORS.length : 0];
    }

    function _setProgress(pct, label, stepIdx) {
        const fill = document.getElementById("pgProgressFill");
        if (fill) fill.style.width = pct + "%";

        const row = document.getElementById("pgProgressStepsRow");
        if (row && stepIdx !== undefined) {
            row.querySelectorAll(".pg-stream-step").forEach((step, i) => {
                step.classList.remove("active", "done");
                const node = step.querySelector(".pg-stream-node");
                if (i < stepIdx || pct === 100) {
                    step.classList.add("done");
                    if (node) node.innerHTML = `<i class="fa-solid fa-circle-check"></i>`;
                } else if (i === stepIdx) {
                    step.classList.add("active");
                    if (node) node.innerHTML = `<i class="fa-solid ${PROGRESS_STEPS[i].icon} fa-spin"></i>`;
                } else {
                    if (node) node.innerHTML = `<i class="fa-solid ${PROGRESS_STEPS[i].icon}"></i>`;
                }
            });
        }
    }

    function _showProgress(show) {
        const wrap = document.getElementById("pgProgressWrap");
        if (!wrap) return;
        if (show) {
            const row = document.getElementById("pgProgressStepsRow");
            row.innerHTML = PROGRESS_STEPS.map((s, i) => `
                <div class="pg-stream-step" data-idx="${i}">
                    <div class="pg-stream-node"><i class="fa-solid ${s.icon}"></i></div>
                    <span>${_escHtml(s.label)}</span>
                </div>`).join("");
            wrap.style.display = "block";
            document.getElementById("pgProgressFill").style.width = "0%";
        } else {
            wrap.style.display = "none";
        }
    }

    function _statBar(report) {
        const bar = document.getElementById("pgStats");
        if (!bar) return;
        if (!report) { bar.innerHTML = `<span>No report generated</span>`; return; }
        const cardCount  = (report.stat_cards || []).length;
        const topicCount = (report.topics || []).length;
        const srcCount   = (report.sources || []).length;
        bar.innerHTML = `<span>${cardCount} stat cards · ${topicCount} topics · ${srcCount} sources · ${_escHtml(report.freshness_label || "")}</span>`;
    }

    function _escHtml(str) {
        return String(str || "")
            .replace(/&/g,"&amp;").replace(/</g,"&lt;")
            .replace(/>/g,"&gt;").replace(/"/g,"&quot;");
    }

    function _toggleTab(tab) {
        const reportPanel  = document.getElementById("pgReportPanel");
        const historyPanel = document.getElementById("pgHistoryPanel");
        const tabReport    = document.getElementById("pgTabReport");
        const tabHistory   = document.getElementById("pgTabHistory");
        const tabsRow      = document.getElementById("pgTabsRow");

        tabsRow.style.display = "flex";

        if (tab === "report") {
            reportPanel.style.display  = "block";
            historyPanel.style.display = "none";
            tabReport.classList.add("active");
            tabHistory.classList.remove("active");
        } else {
            reportPanel.style.display  = "none";
            historyPanel.style.display = "block";
            tabReport.classList.remove("active");
            tabHistory.classList.add("active");
            _loadHistory();
        }
    }

    // ── Sparkline (inline SVG polyline from 0-100 normalized points) ───────
    function _sparklineSvg(points, color) {
        if (!points || points.length < 2) return "";
        const w = 90, h = 26;
        const step = w / (points.length - 1);
        const coords = points.map((p, i) => `${(i*step).toFixed(1)},${(h - (p/100*h)).toFixed(1)}`).join(" ");
        return `<svg width="${w}" height="${h}" viewBox="0 0 ${w} ${h}" class="pg-card-spark">
            <polyline points="${coords}" fill="none" stroke="${color}" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>
        </svg>`;
    }

    // ── Heatmap color (light -> deep teal by count intensity) ──────────────
    function _heatColor(count, maxCount) {
        if (!count || !maxCount) return "var(--bg-glass)";
        const ratio = Math.min(1, count / maxCount);
        const r = Math.round(209 + (13 - 209) * ratio);
        const g = Math.round(250 + (148 - 250) * ratio);
        const b = Math.round(229 + (136 - 229) * ratio);
        return `rgb(${r},${g},${b})`;
    }

    // ── Render report ─────────────────────────────────────────────────────
    function _renderReport(report) {
        _currentReport = report;
        _statBar(report);

        document.getElementById("pgIdleState").style.display = "none";
        const content = document.getElementById("pgReportContent");
        content.style.display = "block";
        content.innerHTML = "";

        const topics = report.topics || [];
        const genAt  = report.generated_at || "";
        const fresh  = report.freshness_label || "";

        // ── Meta strip ──────────────────────────────────────────────────
        const metaCard = document.createElement("div");
        metaCard.className = "pg-meta-card";
        metaCard.innerHTML = `
            <div>
                <span class="pg-freshness"><i class="fa-solid fa-circle"></i>${_escHtml(fresh)}</span>
                <span class="pg-freshness"><i class="fa-solid fa-file-lines"></i>${_escHtml((report.doc_names||[]).join(", "))}</span>
                <span class="pg-freshness"><i class="fa-regular fa-clock"></i>${_escHtml(genAt)}</span>
            </div>`;
        content.appendChild(metaCard);

        // ── Topic context strip ────────────────────────────────────────
        if (topics.length > 0) {
            const stripHeading = document.createElement("div");
            stripHeading.className = "pg-section-heading";
            stripHeading.innerHTML = `<i class="fa-solid fa-layer-group"></i> Topics Identified`;
            content.appendChild(stripHeading);

            const strip = document.createElement("div");
            strip.className = "pg-topic-strip";
            strip.innerHTML = topics.map((t, i) => {
                const color = TOPIC_COLORS[i % TOPIC_COLORS.length];
                return `<div class="pg-topic-chip" style="border-left-color:${color}">
                    <div class="tc-title" style="color:${color}">${_escHtml(t.title || "")}</div>
                    <div class="tc-context">${_escHtml(t.doc_context || "")}</div>
                </div>`;
            }).join("");
            content.appendChild(strip);
        }

        // ── StatCards ───────────────────────────────────────────────────
        const cardsHeading = document.createElement("div");
        cardsHeading.className = "pg-section-heading";
        cardsHeading.innerHTML = `<i class="fa-solid fa-chart-simple"></i> Stat Cards`;
        content.appendChild(cardsHeading);

        const cards = report.stat_cards || [];
        if (cards.length === 0) {
            const empty = document.createElement("div");
            empty.className = "pg-state";
            empty.style.padding = "30px 20px";
            empty.innerHTML = `<i class="fa-solid fa-magnifying-glass" style="font-size:22px"></i><span>No statistics were found in the web results.</span>`;
            content.appendChild(empty);
        } else {
            const grid = document.createElement("div");
            grid.className = "pg-cards-grid";
            grid.innerHTML = cards.map(c => {
                const color = _topicColor(c.topic_id, topics);
                const icon  = TIER_ICONS[c.source_type] || TIER_ICONS.default;
                const trendIcon = c.trend === "up"
                    ? `<i class="fa-solid fa-arrow-trend-up"></i>` : c.trend === "down"
                    ? `<i class="fa-solid fa-arrow-trend-down"></i>` : "";
                const spark = _sparklineSvg(c.sparkline, color);
                const srcLine = c.source_url
                    ? `<a class="pg-card-src" href="${_escHtml(c.source_url)}" target="_blank" rel="noopener">${icon} ${_escHtml((c.source_title||"").substring(0,36))}</a>`
                    : `<span class="pg-card-src" style="opacity:.5">No source link</span>`;
                return `
                <div class="pg-card" style="border-left-color:${color}">
                    <div class="pg-card-topic" style="color:${color}">${_escHtml(c.topic_title || "")}</div>
                    <div class="pg-card-value-row">
                        <span class="pg-card-value">${_escHtml(c.display_value || "")}</span>
                        <span class="pg-card-trend ${c.trend||''}">${trendIcon}</span>
                    </div>
                    ${spark}
                    <div class="pg-card-context">${_escHtml((c.context||"").substring(0,110))}</div>
                    ${srcLine}
                </div>`;
            }).join("");
            content.appendChild(grid);
        }

        // ── CredibilityGrid ─────────────────────────────────────────────
        const gridHeading = document.createElement("div");
        gridHeading.className = "pg-section-heading";
        gridHeading.innerHTML = `<i class="fa-solid fa-table-cells"></i> Credibility Grid`;
        content.appendChild(gridHeading);

        const cg = report.credibility_grid || { tiers: [], rows: [] };
        const tiers = cg.tiers || [];
        const rows  = cg.rows || [];

        if (rows.length === 0) {
            const empty = document.createElement("div");
            empty.className = "pg-state";
            empty.style.padding = "30px 20px";
            empty.innerHTML = `<i class="fa-solid fa-table-cells" style="font-size:22px"></i><span>No source data collected yet.</span>`;
            content.appendChild(empty);
        } else {
            let maxCount = 0;
            rows.forEach(r => tiers.forEach(t => { maxCount = Math.max(maxCount, (r.counts||{})[t] || 0); }));

            const wrap = document.createElement("div");
            wrap.className = "pg-grid-wrap";
            const headCells = tiers.map(t => `<th>${TIER_ICONS[t]||""} ${_escHtml(t)}</th>`).join("");
            const bodyRows = rows.map(r => {
                const color = _topicColor(r.topic_id, topics);
                const cells = tiers.map(t => {
                    const count = (r.counts||{})[t] || 0;
                    return `<td class="pg-heat-cell" style="background:${_heatColor(count, maxCount)}">${count}</td>`;
                }).join("");
                return `<tr>
                    <td class="pg-heat-topic" style="border-left-color:${color}">${_escHtml(r.topic_title||"")}</td>
                    ${cells}
                    <td class="pg-heat-total">${r.total||0}</td>
                </tr>`;
            }).join("");
            wrap.innerHTML = `
                <table class="pg-heat-table">
                    <thead><tr><th>Topic</th>${headCells}<th>Total</th></tr></thead>
                    <tbody>${bodyRows}</tbody>
                </table>
                <div class="pg-heat-legend">
                    <span class="swatch" style="background:${_heatColor(0,1)}"></span> none
                    <span class="swatch" style="background:${_heatColor(1,2)}"></span> some
                    <span class="swatch" style="background:${_heatColor(2,2)}"></span> most credible-source coverage
                </div>`;
            content.appendChild(wrap);
        }

        // Show buttons + tabs
        const dlBtn = document.getElementById("pgDownloadBtn");
        if (dlBtn) dlBtn.style.display = "flex";
        const regenBtn = document.getElementById("pgRegenerateBtn");
        if (regenBtn) regenBtn.style.display = "flex";

        document.getElementById("pgTabsRow").style.display = "flex";
        document.getElementById("pgTabReport").classList.add("active");
        document.getElementById("pgTabHistory").classList.remove("active");
    }

    // ── Load history ──────────────────────────────────────────────────────
    async function _loadHistory() {
        const list = document.getElementById("pgHistoryList");
        if (!list) return;
        list.innerHTML = `<div class="pg-state"><i class="fa-solid fa-circle-notch fa-spin"></i><span>Loading history…</span></div>`;
        try {
            const res  = await fetch("/pulse_grid/history");
            const data = await res.json();
            const reports = data.reports || [];

            if (reports.length === 0) {
                list.innerHTML = `<div class="pg-state"><i class="fa-solid fa-inbox"></i><span>No saved reports yet.</span></div>`;
                return;
            }

            list.innerHTML = "";
            reports.forEach(r => {
                const item = document.createElement("div");
                item.className = "pg-history-item";
                item.innerHTML = `
                    <i class="fa-solid fa-chart-pie" style="color:var(--accent);font-size:20px;flex-shrink:0"></i>
                    <div class="pg-history-meta">
                        <div class="pg-history-title">${_escHtml(r.title || "StatSonar Report")}</div>
                        <div class="pg-history-sub">
                            ${_escHtml((r.doc_names||[]).join(", ") || "—")}
                            · ${_escHtml(r.generated_at || "")}
                            · ${r.card_count || 0} stat cards · ${r.topic_count || 0} topics
                        </div>
                    </div>
                    <button class="pg-history-del" onclick="event.stopPropagation();PulseGrid._deleteReport('${_escHtml(r.id)}',this)" title="Delete">
                        <i class="fa-solid fa-trash"></i>
                    </button>`;
                item.addEventListener("click", () => PulseGrid._loadReportById(r.id));
                list.appendChild(item);
            });
        } catch(e) {
            list.innerHTML = `<div class="pg-state"><i class="fa-solid fa-triangle-exclamation"></i><span>Failed to load history.</span></div>`;
        }
    }

    async function _loadReportById(id) {
        try {
            const res  = await fetch(`/pulse_grid/history/${id}`);
            const data = await res.json();
            if (data.status === "ok" && data.report) {
                _toggleTab("report");
                _renderReport(data.report);
            }
        } catch(e) {
            console.error("[PG] load report error:", e);
        }
    }

    async function _deleteReport(id, btn) {
        if (!confirm("Delete this report?")) return;
        btn.innerHTML = `<i class="fa-solid fa-circle-notch fa-spin"></i>`;
        try {
            await fetch(`/pulse_grid/history/${id}`, { method: "DELETE" });
            btn.closest(".pg-history-item").remove();
        } catch(e) {
            btn.innerHTML = `<i class="fa-solid fa-trash"></i>`;
        }
    }

    function _toast(msg, type = "info") {
        if (typeof Swal !== "undefined") {
            Swal.fire({
                toast: true, position: "top-end",
                icon: type === "success" ? "success" : type === "warning" ? "warning" : "error",
                title: msg, showConfirmButton: false, timer: 3500, timerProgressBar: true,
            });
        } else { alert(msg); }
    }

    // ════════════════════════════════════════════════════════════════════════
    // Public API
    // ════════════════════════════════════════════════════════════════════════

    function open() {
        _inject();
        document.getElementById("pgOverlay").classList.add("pg-active");
    }

    function close() {
        const overlay = document.getElementById("pgOverlay");
        if (overlay) overlay.classList.remove("pg-active");
        if (typeof originalBackToChatbot === "function") {
            originalBackToChatbot();
        } else if (typeof backToChatbot === "function") {
            backToChatbot();
        }
    }

    async function generate() {
        if (_generating) return;
        _generating = true;

        const files     = _getSelectedFiles();
        const sessionId = (typeof window.currentSessionId !== "undefined" && window.currentSessionId)
            ? window.currentSessionId : "pg-session";

        document.getElementById("pgIdleState").style.display     = "none";
        document.getElementById("pgReportContent").style.display = "none";
        document.getElementById("pgTabsRow").style.display       = "none";

        const dlBtn = document.getElementById("pgDownloadBtn");
        if (dlBtn) dlBtn.style.display = "none";
        const regenBtn = document.getElementById("pgRegenerateBtn");
        if (regenBtn) regenBtn.style.display = "none";

        const genBtn = document.getElementById("pgGenerateBtn");
        genBtn.disabled = true;
        genBtn.innerHTML = `<span class="nexora-spinner inline"></span> Building…`;

        _showProgress(true);

        let stepIdx = 0;
        _setProgress(PROGRESS_STEPS[0].pct, PROGRESS_STEPS[0].label, 0);
        const stepTimer = setInterval(() => {
            if (stepIdx < PROGRESS_STEPS.length - 2) {
                stepIdx++;
                const s = PROGRESS_STEPS[stepIdx];
                _setProgress(s.pct, s.label, stepIdx);
            }
        }, 2800);

        try {
            const res = await fetch("/pulse_grid/generate", {
                method:  "POST",
                headers: { "Content-Type": "application/json" },
                body:    JSON.stringify({ files, session_id: sessionId }),
            });

            clearInterval(stepTimer);

            if (!res.ok) {
                const err = await res.json().catch(() => ({}));
                throw new Error(err.message || `Server error ${res.status}`);
            }

            const data = await res.json();
            if (data.status !== "ok" || !data.report) throw new Error(data.message || "No report returned");

            _setProgress(100, "Report ready!", PROGRESS_STEPS.length - 1);
            await new Promise(r => setTimeout(r, 500));

            _showProgress(false);
            _renderReport(data.report);

        } catch(e) {
            clearInterval(stepTimer);
            _showProgress(false);
            document.getElementById("pgIdleState").style.display = "flex";
            _toast("Report generation failed: " + e.message, "error");
            console.error("[PG] generate error:", e);
        } finally {
            _generating = false;
            genBtn.disabled = false;
            genBtn.innerHTML = `<i class="fa-solid fa-wand-magic-sparkles"></i> Build Grid`;
            const regenBtn = document.getElementById("pgRegenerateBtn");
            if (regenBtn) {
                regenBtn.disabled = false;
                regenBtn.innerHTML = `<i class="fa-solid fa-wand-magic-sparkles"></i> Regenerate`;
            }
        }
    }

    async function downloadPDF() {
        if (!_currentReport) {
            _toast("No report loaded yet.", "warning");
            return;
        }
        const btn = document.getElementById("pgDownloadBtn");
        btn.disabled = true;
        btn.innerHTML = `<span class="nexora-spinner inline"></span> Preparing PDF…`;

        try {
            const res = await fetch("/pulse_grid/export_pdf", {
                method:  "POST",
                headers: { "Content-Type": "application/json" },
                body:    JSON.stringify({ report: _currentReport }),
            });

            if (!res.ok) throw new Error(`Server error ${res.status}`);

            const blob = await res.blob();
            const disposition = res.headers.get("Content-Disposition") || "";
            const nameMatch   = disposition.match(/filename="?([^"]+)"?/);
            const fileName    = nameMatch ? nameMatch[1] : `nexora_pulse_grid.pdf`;

            const url  = URL.createObjectURL(blob);
            const link = document.createElement("a");
            link.href  = url;
            link.download = fileName;
            link.click();
            URL.revokeObjectURL(url);

            _toast("PDF downloaded!", "success");
        } catch(e) {
            _toast("PDF export failed: " + e.message, "error");
        } finally {
            btn.disabled = false;
            btn.innerHTML = `<i class="fa-solid fa-file-arrow-down"></i> Download PDF`;
        }
    }

    // Hook into global functions so PulseGrid closes when returning to chatbot or opening other features
    if (typeof window.backToChatbot === "function") {
        originalBackToChatbot = window.backToChatbot;
        window.backToChatbot = function() {
            try {
                const overlay = document.getElementById("pgOverlay");
                if (overlay) overlay.classList.remove("pg-active");
            } catch(e){}
            if (typeof originalBackToChatbot === "function") {
                originalBackToChatbot.apply(this, arguments);
            }
        };
    }
    if (typeof window.openFeatureInWorkspace === "function") {
        const originalOpenFeatureInWorkspace = window.openFeatureInWorkspace;
        window.openFeatureInWorkspace = function(featureName, openFunc) {
            if (featureName !== 'PulseGrid') {
                try {
                    const overlay = document.getElementById("pgOverlay");
                    if (overlay) overlay.classList.remove("pg-active");
                } catch(e){}
            }
            originalOpenFeatureInWorkspace.apply(this, arguments);
        };
    }

    return {
        open, close, generate, downloadPDF,
        _toggleTab, _loadReportById, _deleteReport,
    };

})();
