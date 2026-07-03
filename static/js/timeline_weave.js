/**
 * timeline_weave.js  —  Nexora AI  |  TimelineWeave
 * ===================================================================
 * Renders the doc-topics-vs-real-world-events timeline inside
 * featureWorkspace and provides a PDF download button that posts
 * to /timeline_weave/export_pdf.
 *
 * Include in index.html:
 *   <script src="{{ url_for('static', filename='js/timeline_weave.js') }}" defer></script>
 *
 * Trigger from Explore Features dropdown:
 *   onclick="openFeatureInWorkspace('TimelineWeave', () => TimelineWeave.open())"
 *
 * Add to app.py:
 *   from timeline_weave import timeline_weave_bp
 *   app.register_blueprint(timeline_weave_bp)
 */

const TimelineWeave = (() => {
    "use strict";

    // ── State ─────────────────────────────────────────────────────────────
    let _injected      = false;
    let _generating     = false;
    let _currentReport  = null;   // last fully loaded ReportJSON
    let originalBackToChatbot = null;

    // ── Cluster color palette (matches PDF export colors) ───────────────────
    const CLUSTER_COLORS = ["#0d9488", "#f59e0b", "#7c3aed", "#3b82f6", "#ec4899"];

    const SOURCE_ICONS = {
        news:     '<i class="fa-solid fa-newspaper"></i>',
        tweet:    '<i class="fa-brands fa-x-twitter"></i>',
        review:   '<i class="fa-solid fa-star"></i>',
        blog:     '<i class="fa-solid fa-pen-nib"></i>',
        forum:    '<i class="fa-solid fa-comments"></i>',
        research: '<i class="fa-solid fa-flask"></i>',
        article:  '<i class="fa-solid fa-link"></i>',
        default:  '<i class="fa-solid fa-link"></i>'
    };

    const PROGRESS_STEPS = [
        { pct: 10, icon: 'fa-file-lines',      label: "Fetching document chunks from ChromaDB…" },
        { pct: 28, icon: 'fa-layer-group',     label: "Extracting key clusters with AI…" },
        { pct: 50, icon: 'fa-earth-americas',  label: "Searching the web for dated events…" },
        { pct: 74, icon: 'fa-clock-rotate-left', label: "Weaving events into a timeline…" },
        { pct: 90, icon: 'fa-pen-nib',         label: "Writing summary…" },
        { pct: 96, icon: 'fa-flag-checkered',  label: "Finalising timeline…" }
    ];

    // ── Inject once ───────────────────────────────────────────────────────
    function _inject() {
        if (_injected) return;
        _injected = true;

        // ── CSS ──────────────────────────────────────────────────────────
        const style = document.createElement("style");
        style.textContent = `
        #tlwOverlay {
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
        #tlwOverlay.tlw-active { display: flex; }

        #tlwModal {
            background: var(--bg-glass-deep);
            border: 1px solid var(--border);
            border-radius: 24px;
            width: min(1040px, 96vw);
            max-height: 90vh;
            display: flex;
            flex-direction: column;
            overflow: hidden;
            box-shadow: 0 24px 64px rgba(0,0,0,0.6), 0 0 40px var(--accent-glow-soft);
            backdrop-filter: blur(30px);
            -webkit-backdrop-filter: blur(30px);
            animation: tlw-slide-up 0.22s cubic-bezier(0.22,1,0.36,1);
            font-family: var(--font-body), sans-serif;
        }
        @keyframes tlw-slide-up {
            from { opacity:0; transform:translateY(18px) scale(0.98); }
            to   { opacity:1; transform:translateY(0) scale(1);        }
        }

        #featureWorkspace #tlwOverlay {
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
        #featureWorkspace #tlwOverlay.tlw-active {
            display: flex !important;
            align-items: stretch !important;
            justify-content: stretch !important;
        }
        #featureWorkspace #tlwModal {
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
        #tlwHeader {
            display: flex;
            align-items: center;
            justify-content: space-between;
            gap: 12px;
            padding: 16px 22px;
            border-bottom: 1px solid var(--border);
            background: var(--accent-bg);
            flex-shrink: 0;
        }
        #tlwHeader h3 {
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
        #tlwHeader h3 i { -webkit-text-fill-color: initial; color: var(--accent); }
        #tlwHeader .tlw-icon { color: var(--accent); font-size: 1.2rem; }
        #tlwHeader .hdr-subtitle {
            font-size: 0.72rem !important;
            font-style: italic !important;
            color: var(--text-secondary, #9aa0b4) !important;
            display: block !important;
            margin-top: 2px !important;
        }
        .tlw-header-right { margin-left: auto; display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
        .tlw-header-btn {
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
        .tlw-header-btn:hover { background: var(--accent-glow-soft); border-color: var(--border-hover); }
        .tlw-header-btn:disabled { opacity: 0.45; cursor: not-allowed; }
        .tlw-header-btn.primary {
            background: var(--grad-button) !important;
            color: var(--swal-btn-text) !important;
            border: none;
            box-shadow: 0 4px 14px var(--accent-glow-soft);
        }
        .tlw-header-btn.primary:hover:not(:disabled) {
            filter: brightness(1.08);
            transform: translateY(-1px);
            box-shadow: 0 6px 18px var(--accent-glow);
        }
        #tlwCloseBtn {
            background: transparent; border: none; cursor: pointer;
            color: var(--text-muted); font-size: 1.2rem;
            padding: 6px 10px; border-radius: 50%;
            transition: color .2s, background .2s;
            display: flex; align-items: center; justify-content: center;
        }
        #tlwCloseBtn:hover { color: var(--accent-light) !important; background: var(--accent-bg) !important; }
        body.light-mode #tlwCloseBtn:hover { color: var(--accent) !important; }

        #tlwStats { display: flex; gap: 10px; font-size: .72rem; color: var(--text-secondary); align-items: center; }
        #tlwStats span { background: var(--bg-glass); border: 1px solid var(--border); border-radius: 20px; padding: 3px 10px; }

        /* ── Body ── */
        #tlwBody { padding: 22px 26px; overflow-y: auto; flex: 1; scroll-behavior: smooth; }
        #tlwBody::-webkit-scrollbar { width: 5px; }
        #tlwBody::-webkit-scrollbar-track { background: transparent; }
        #tlwBody::-webkit-scrollbar-thumb { background: var(--accent-glow); border-radius: 99px; }

        .tlw-state {
            display: flex; flex-direction: column; align-items: center; justify-content: center;
            gap: 14px; padding: 60px 20px; text-align: center; color: var(--text-secondary);
        }
        .tlw-state i { font-size: 2.4rem; color: var(--accent); opacity: 0.7; margin-bottom: 4px; }

        /* ── Progress ── */
        #tlwProgressWrap {
            margin: 25px auto; max-width: 620px; padding: 24px;
            background: var(--bg-glass-deep); border: 1px solid var(--border);
            border-radius: 20px; box-shadow: 0 12px 40px rgba(0,0,0,0.3); display: none;
        }
        .tlw-progress-track { height: 4px; background: var(--accent-glow-soft); border-radius: 99px; overflow: hidden; margin-bottom: 22px; }
        .tlw-progress-fill { height: 100%; width: 0%; background: var(--grad-button); border-radius: 99px; transition: width 0.7s cubic-bezier(0.4,0,0.2,1); }
        .tlw-progress-stream { display: flex; flex-direction: column; gap: 16px; position: relative; padding-left: 28px; text-align: left; }
        .tlw-progress-stream::before {
            content: ""; position: absolute; left: 9px; top: 4px; bottom: 4px; width: 2px;
            border-left: 2px dashed rgba(255,255,255,0.15);
        }
        body.light-mode .tlw-progress-stream::before { border-left-color: rgba(0,0,0,0.15); }
        .tlw-stream-step { display: flex; align-items: center; gap: 14px; position: relative; font-size: 12px; color: var(--text-muted); transition: color 0.4s ease; }
        .tlw-stream-node {
            position: absolute; left: -28px; width: 20px; height: 20px; border-radius: 50%;
            background: var(--bg-glass-deep); border: 2px solid var(--border);
            display: flex; align-items: center; justify-content: center;
            font-size: 10px; color: var(--text-muted); z-index: 2; transition: all 0.4s ease;
        }
        .tlw-stream-step.active { color: var(--accent); font-weight: 600; }
        .tlw-stream-step.active .tlw-stream-node { border-color: var(--accent); background: var(--accent-bg); color: var(--accent-light); box-shadow: 0 0 10px var(--accent-glow); }
        .tlw-stream-step.done { color: var(--text-primary); }
        .tlw-stream-step.done .tlw-stream-node { border-color: #22c55e; background: rgba(34,197,94,0.15); color: #22c55e; }

        /* ── Summary card ── */
        .tlw-summary-card {
            border: 1px solid var(--border); border-radius: 16px; padding: 20px 22px;
            margin-bottom: 20px; background: var(--bg-glass); animation: tlw-fade-in 0.4s ease;
        }
        @keyframes tlw-fade-in { from{opacity:0;transform:translateY(10px)} to{opacity:1;transform:translateY(0)} }
        .tlw-summary-card h3 {
            font-size: 13px; font-weight: 700; color: var(--text-muted); text-transform: uppercase;
            letter-spacing: .07em; margin-bottom: 8px; font-family: var(--font-display), sans-serif;
            display: flex; align-items: center; gap: 8px;
        }
        .tlw-summary-card p { font-size: 14px; line-height: 1.7; color: var(--text-primary); margin: 0; }
        .tlw-freshness {
            display: inline-flex; align-items: center; gap: 6px; font-size: 11px; padding: 3px 10px;
            border-radius: 20px; border: 1px solid var(--border); color: var(--text-muted);
            background: var(--bg-glass); margin-top: 10px; margin-right: 8px;
        }
        .tlw-freshness i { color: #22c55e; font-size: 8px; }

        /* ── Legend ── */
        .tlw-legend { display: flex; flex-wrap: wrap; gap: 14px; margin-bottom: 22px; }
        .tlw-legend-item { display: flex; align-items: center; gap: 6px; font-size: 12px; color: var(--text-secondary); }
        .tlw-legend-dot { width: 10px; height: 10px; border-radius: 50%; flex-shrink: 0; }

        /* ── Section heading ── */
        .tlw-section-heading {
            display: flex; align-items: center; gap: 10px; margin: 24px 0 18px;
            font-family: var(--font-display), sans-serif; font-size: 13px; font-weight: 700;
            color: var(--text-secondary); text-transform: uppercase; letter-spacing: .07em;
            padding-bottom: 8px; border-bottom: 1px solid var(--border);
        }
        .tlw-section-heading i { color: var(--accent); font-size: 14px; }

        /* ── Timeline ── */
        .tlw-timeline { position: relative; padding-left: 10px; }
        .tlw-tl-item { display: flex; gap: 16px; margin-bottom: 22px; animation: tlw-fade-in 0.4s ease; }
        .tlw-tl-date {
            flex-shrink: 0; width: 76px; text-align: right; font-size: 12px; font-weight: 700;
            padding-top: 3px; font-family: var(--font-display), sans-serif;
        }
        .tlw-tl-rail { flex-shrink: 0; width: 16px; display: flex; flex-direction: column; align-items: center; }
        .tlw-tl-dot { width: 12px; height: 12px; border-radius: 50%; margin-top: 6px; box-shadow: 0 0 0 4px var(--bg-glass); }
        .tlw-tl-line { flex: 1; width: 2px; background: var(--border); margin-top: 2px; min-height: 20px; }
        .tlw-tl-item:last-child .tlw-tl-line { display: none; }
        .tlw-tl-content {
            flex: 1; padding: 12px 16px; border: 1px solid var(--border); border-radius: 12px;
            background: var(--bg-glass); transition: border-color 0.15s;
        }
        .tlw-tl-content:hover { border-color: var(--border-hover); }
        .tlw-tl-cluster { font-size: 10.5px; font-weight: 700; text-transform: uppercase; letter-spacing: .05em; margin-bottom: 4px; }
        .tlw-tl-event { font-size: 13.5px; font-weight: 600; color: var(--text-primary); margin-bottom: 6px; line-height: 1.45; }
        .tlw-tl-src {
            font-size: 11px; color: var(--text-secondary); text-decoration: none;
            display: inline-flex; align-items: center; gap: 5px; opacity: 0.85;
        }
        .tlw-tl-src:hover { opacity: 1; color: var(--accent); text-decoration: underline; }

        /* ── Cluster context strip ── */
        .tlw-cluster-strip { display: flex; flex-wrap: wrap; gap: 10px; margin-bottom: 22px; }
        .tlw-cluster-chip {
            border: 1px solid var(--border); border-radius: 12px; padding: 10px 14px;
            background: var(--bg-glass); flex: 1; min-width: 220px;
            border-left-width: 3px; border-left-style: solid;
        }
        .tlw-cluster-chip .cc-title { font-size: 12.5px; font-weight: 700; color: var(--text-primary); margin-bottom: 3px; }
        .tlw-cluster-chip .cc-context { font-size: 11.5px; color: var(--text-secondary); line-height: 1.4; }

        /* ── History panel ── */
        .tlw-history-item {
            display: flex; align-items: center; gap: 12px; padding: 10px 14px;
            border: 1px solid var(--border); border-radius: 12px; margin-bottom: 8px;
            background: var(--bg-glass); cursor: pointer; transition: all 0.15s;
        }
        .tlw-history-item:hover { border-color: var(--border-hover); background: var(--accent-bg); }
        .tlw-history-meta { flex: 1; min-width: 0; }
        .tlw-history-title { font-size: 13px; font-weight: 600; color: var(--text-primary); margin-bottom: 3px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
        .tlw-history-sub { font-size: 11px; color: var(--text-muted); }
        .tlw-history-del { background: none; border: none; color: var(--text-muted); cursor: pointer; padding: 4px 6px; border-radius: 6px; font-size: 12px; }
        .tlw-history-del:hover { color: #ef4444; background: rgba(239,68,68,.1); }

        /* ── Tabs ── */
        .tlw-tabs { display: flex; gap: 4px; margin-bottom: 18px; border-bottom: 1px solid var(--border); }
        .tlw-tab-btn {
            background: none; border: none; padding: 8px 16px; font-size: 13px; font-weight: 600;
            color: var(--text-muted); cursor: pointer; border-bottom: 2px solid transparent;
            margin-bottom: -1px; transition: all 0.18s; font-family: var(--font-display), sans-serif;
            display: flex; align-items: center; gap: 6px;
        }
        .tlw-tab-btn:hover { color: var(--text-primary); }
        .tlw-tab-btn.active { color: var(--accent); border-bottom-color: var(--accent); }
        `;
        document.head.appendChild(style);

        // ── HTML ──────────────────────────────────────────────────────────
        const overlay = document.createElement("div");
        overlay.id = "tlwOverlay";
        overlay.innerHTML = `
        <div id="tlwModal">
            <div id="tlwHeader">
                <div class="tlw-header-left">
                    <i class="fa-solid fa-timeline tlw-icon"></i>
                    <div>
                        <h3 style="margin:0;">TimelineWeave</h3>
                        <span class="hdr-subtitle">Plots your document's topics against real-world events, live from the web.</span>
                    </div>
                </div>
                <div class="tlw-header-right">
                    <div id="tlwStats"><span>No timeline generated</span></div>
                    <button class="tlw-header-btn" id="tlwHistoryBtn" onclick="TimelineWeave._toggleTab('history')">
                        <i class="fa-solid fa-clock-rotate-left"></i> History
                    </button>
                    <button class="tlw-header-btn primary" id="tlwRegenerateBtn" onclick="TimelineWeave.generate()" style="display:none">
                        <i class="fa-solid fa-wand-magic-sparkles"></i> Regenerate
                    </button>
                    <button class="tlw-header-btn" id="tlwDownloadBtn" onclick="TimelineWeave.downloadPDF()" style="display:none">
                        <i class="fa-solid fa-file-arrow-down"></i> Download PDF
                    </button>
                    <button id="tlwCloseBtn" onclick="TimelineWeave.close()" title="Close TimelineWeave" style="margin-left:8px;background:transparent;border:none;color:var(--text-secondary);cursor:pointer;font-size:18px;display:inline-flex;align-items:center;justify-content:center;padding:4px;transition:color 0.2s;">
                        <i class="fa-solid fa-xmark"></i>
                    </button>
                </div>
            </div>

            <div id="tlwBody">

                <div id="tlwProgressWrap">
                    <div style="font-family:var(--font-display);font-size:13px;font-weight:700;color:var(--text-primary);margin-bottom:16px;display:flex;align-items:center;gap:8px;">
                        <i class="fa-solid fa-circle-notch fa-spin" style="color:var(--accent);"></i>
                        <span>Weaving the Timeline...</span>
                    </div>
                    <div class="tlw-progress-track"><div class="tlw-progress-fill" id="tlwProgressFill"></div></div>
                    <div id="tlwProgressStepsRow" class="tlw-progress-stream"></div>
                </div>

                <div id="tlwTabsRow" class="tlw-tabs" style="display:none">
                    <button class="tlw-tab-btn active" id="tlwTabReport" onclick="TimelineWeave._toggleTab('report')">
                        <i class="fa-solid fa-timeline"></i> Timeline
                    </button>
                    <button class="tlw-tab-btn" id="tlwTabHistory" onclick="TimelineWeave._toggleTab('history')">
                        <i class="fa-solid fa-clock-rotate-left"></i> History
                    </button>
                </div>

                <div id="tlwReportPanel">
                    <div class="tlw-state" id="tlwIdleState">
                        <i class="fa-solid fa-timeline" style="font-size:32px;color:var(--accent);margin-bottom:8px;"></i>
                        <h3 style="margin:0;font-family:var(--font-display);font-size:1.1rem;color:var(--text-primary)">Doc Topics, Mapped to Real Events</h3>
                        <p style="margin:0 0 12px;font-size:.88rem;max-width:480px;line-height:1.5">
                            Select documents from the sidebar, then click <strong>Weave Timeline</strong> to plot your document's topics against dated real-world events.
                        </p>
                        <button id="tlwGenerateBtn" onclick="TimelineWeave.generate()"
                            style="background:var(--grad-button);color:var(--swal-btn-text);border:none;padding:8px 16px;border-radius:10px;font-family:var(--font-display);font-size:0.8rem;font-weight:600;cursor:pointer;box-shadow:0 4px 12px var(--accent-glow);display:inline-flex;align-items:center;gap:8px">
                            <i class="fa-solid fa-wand-magic-sparkles"></i> Weave Timeline
                        </button>
                    </div>
                    <div id="tlwReportContent" style="display:none"></div>
                </div>

                <div id="tlwHistoryPanel" style="display:none">
                    <div class="tlw-section-heading"><i class="fa-solid fa-clock-rotate-left"></i> Saved Timelines</div>
                    <div id="tlwHistoryList"><div class="tlw-state"><i class="fa-solid fa-circle-notch fa-spin"></i><span>Loading…</span></div></div>
                </div>

            </div>
        </div>`;

        (document.getElementById("featureWorkspace") || document.body).appendChild(overlay);

        overlay.addEventListener("click", e => { if (e.target === overlay) TimelineWeave.close(); });
    }

    // ── Helpers ───────────────────────────────────────────────────────────
    function _getSelectedFiles() {
        const checked = [];
        document.querySelectorAll("#documentCheckboxList input:checked").forEach(cb => checked.push(cb.value));
        return checked;
    }

    function _clusterColor(clusterId, clusters) {
        const idx = (clusters || []).findIndex(c => c.id === clusterId);
        return CLUSTER_COLORS[idx >= 0 ? idx % CLUSTER_COLORS.length : 0];
    }

    function _setProgress(pct, label, stepIdx) {
        const fill = document.getElementById("tlwProgressFill");
        if (fill) fill.style.width = pct + "%";

        const row = document.getElementById("tlwProgressStepsRow");
        if (row && stepIdx !== undefined) {
            row.querySelectorAll(".tlw-stream-step").forEach((step, i) => {
                step.classList.remove("active", "done");
                const node = step.querySelector(".tlw-stream-node");
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
        const wrap = document.getElementById("tlwProgressWrap");
        if (!wrap) return;
        if (show) {
            const row = document.getElementById("tlwProgressStepsRow");
            row.innerHTML = PROGRESS_STEPS.map((s, i) => `
                <div class="tlw-stream-step" data-idx="${i}">
                    <div class="tlw-stream-node"><i class="fa-solid ${s.icon}"></i></div>
                    <span>${_escHtml(s.label)}</span>
                </div>`).join("");
            wrap.style.display = "block";
            document.getElementById("tlwProgressFill").style.width = "0%";
        } else {
            wrap.style.display = "none";
        }
    }

    function _statBar(report) {
        const bar = document.getElementById("tlwStats");
        if (!bar) return;
        if (!report) { bar.innerHTML = `<span>No timeline generated</span>`; return; }
        const evCount = (report.timeline || []).length;
        const clCount = (report.clusters || []).length;
        bar.innerHTML = `<span>${evCount} events · ${clCount} clusters · ${_escHtml(report.freshness_label || "")}</span>`;
    }

    function _escHtml(str) {
        return String(str || "")
            .replace(/&/g,"&amp;").replace(/</g,"&lt;")
            .replace(/>/g,"&gt;").replace(/"/g,"&quot;");
    }

    function _toggleTab(tab) {
        const reportPanel  = document.getElementById("tlwReportPanel");
        const historyPanel = document.getElementById("tlwHistoryPanel");
        const tabReport    = document.getElementById("tlwTabReport");
        const tabHistory   = document.getElementById("tlwTabHistory");
        const tabsRow      = document.getElementById("tlwTabsRow");

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

    // ── Render report ─────────────────────────────────────────────────────
    function _renderReport(report) {
        _currentReport = report;
        _statBar(report);

        document.getElementById("tlwIdleState").style.display = "none";
        const content = document.getElementById("tlwReportContent");
        content.style.display = "block";
        content.innerHTML = "";

        const clusters = report.clusters || [];
        const genAt   = report.generated_at || "";
        const fresh   = report.freshness_label || "";

        // ── Summary ─────────────────────────────────────────────────────
        const summaryCard = document.createElement("div");
        summaryCard.className = "tlw-summary-card";
        summaryCard.innerHTML = `
            <h3><i class="fa-solid fa-clipboard-list"></i> Summary</h3>
            <p>${_escHtml(report.summary || "")}</p>
            <div style="margin-top:10px">
                <span class="tlw-freshness"><i class="fa-solid fa-circle"></i>${_escHtml(fresh)}</span>
                <span class="tlw-freshness"><i class="fa-solid fa-file-lines"></i>${_escHtml((report.doc_names||[]).join(", "))}</span>
                <span class="tlw-freshness"><i class="fa-regular fa-clock"></i>${_escHtml(genAt)}</span>
            </div>`;
        content.appendChild(summaryCard);

        // ── Cluster context strip ──────────────────────────────────────
        if (clusters.length > 0) {
            const stripHeading = document.createElement("div");
            stripHeading.className = "tlw-section-heading";
            stripHeading.innerHTML = `<i class="fa-solid fa-layer-group"></i> Clusters Identified`;
            content.appendChild(stripHeading);

            const strip = document.createElement("div");
            strip.className = "tlw-cluster-strip";
            strip.innerHTML = clusters.map((c, i) => {
                const color = CLUSTER_COLORS[i % CLUSTER_COLORS.length];
                return `<div class="tlw-cluster-chip" style="border-left-color:${color}">
                    <div class="cc-title" style="color:${color}">${_escHtml(c.title || "")}</div>
                    <div class="cc-context">${_escHtml(c.doc_context || "")}</div>
                </div>`;
            }).join("");
            content.appendChild(strip);
        }

        // ── Legend ──────────────────────────────────────────────────────
        if (clusters.length > 0) {
            const legend = document.createElement("div");
            legend.className = "tlw-legend";
            legend.innerHTML = clusters.map((c, i) => {
                const color = CLUSTER_COLORS[i % CLUSTER_COLORS.length];
                return `<div class="tlw-legend-item"><span class="tlw-legend-dot" style="background:${color}"></span>${_escHtml(c.title || "")}</div>`;
            }).join("");
            content.appendChild(legend);
        }

        // ── Timeline ────────────────────────────────────────────────────
        const tlHeading = document.createElement("div");
        tlHeading.className = "tlw-section-heading";
        tlHeading.innerHTML = `<i class="fa-solid fa-clock-rotate-left"></i> Event Timeline`;
        content.appendChild(tlHeading);

        const timeline = report.timeline || [];
        if (timeline.length === 0) {
            const empty = document.createElement("div");
            empty.className = "tlw-state";
            empty.style.padding = "30px 20px";
            empty.innerHTML = `<i class="fa-solid fa-magnifying-glass" style="font-size:22px"></i><span>No dated events were found for this document.</span>`;
            content.appendChild(empty);
        } else {
            const tlWrap = document.createElement("div");
            tlWrap.className = "tlw-timeline";
            tlWrap.innerHTML = timeline.map(ev => {
                const color = _clusterColor(ev.cluster_id, clusters);
                const icon  = SOURCE_ICONS[ev.source_type] || SOURCE_ICONS.default;
                const srcLine = ev.source_url
                    ? `<a class="tlw-tl-src" href="${_escHtml(ev.source_url)}" target="_blank" rel="noopener">${icon} ${_escHtml((ev.source_title||"").substring(0,70))}</a>`
                    : `<span class="tlw-tl-src" style="opacity:.5">No source link</span>`;
                return `
                <div class="tlw-tl-item">
                    <div class="tlw-tl-date" style="color:${color}">${_escHtml(ev.date || "")}</div>
                    <div class="tlw-tl-rail">
                        <div class="tlw-tl-dot" style="background:${color}"></div>
                        <div class="tlw-tl-line"></div>
                    </div>
                    <div class="tlw-tl-content">
                        <div class="tlw-tl-cluster" style="color:${color}">${_escHtml(ev.cluster_title || "")}</div>
                        <div class="tlw-tl-event">${_escHtml(ev.event || "")}</div>
                        ${srcLine}
                    </div>
                </div>`;
            }).join("");
            content.appendChild(tlWrap);
        }

        // Show buttons + tabs
        const dlBtn = document.getElementById("tlwDownloadBtn");
        if (dlBtn) dlBtn.style.display = "flex";
        const regenBtn = document.getElementById("tlwRegenerateBtn");
        if (regenBtn) regenBtn.style.display = "flex";

        document.getElementById("tlwTabsRow").style.display = "flex";
        document.getElementById("tlwTabReport").classList.add("active");
        document.getElementById("tlwTabHistory").classList.remove("active");
    }

    // ── Load history ──────────────────────────────────────────────────────
    async function _loadHistory() {
        const list = document.getElementById("tlwHistoryList");
        if (!list) return;
        list.innerHTML = `<div class="tlw-state"><i class="fa-solid fa-circle-notch fa-spin"></i><span>Loading history…</span></div>`;
        try {
            const res  = await fetch("/timeline_weave/history");
            const data = await res.json();
            const reports = data.reports || [];

            if (reports.length === 0) {
                list.innerHTML = `<div class="tlw-state"><i class="fa-solid fa-inbox"></i><span>No saved timelines yet.</span></div>`;
                return;
            }

            list.innerHTML = "";
            reports.forEach(r => {
                const item = document.createElement("div");
                item.className = "tlw-history-item";
                item.innerHTML = `
                    <i class="fa-solid fa-timeline" style="color:var(--accent);font-size:20px;flex-shrink:0"></i>
                    <div class="tlw-history-meta">
                        <div class="tlw-history-title">${_escHtml(r.title || "TimelineWeave Report")}</div>
                        <div class="tlw-history-sub">
                            ${_escHtml((r.doc_names||[]).join(", ") || "—")}
                            · ${_escHtml(r.generated_at || "")}
                            · ${r.event_count || 0} events · ${r.cluster_count || 0} clusters
                        </div>
                    </div>
                    <button class="tlw-history-del" onclick="event.stopPropagation();TimelineWeave._deleteReport('${_escHtml(r.id)}',this)" title="Delete">
                        <i class="fa-solid fa-trash"></i>
                    </button>`;
                item.addEventListener("click", () => TimelineWeave._loadReportById(r.id));
                list.appendChild(item);
            });
        } catch(e) {
            list.innerHTML = `<div class="tlw-state"><i class="fa-solid fa-triangle-exclamation"></i><span>Failed to load history.</span></div>`;
        }
    }

    async function _loadReportById(id) {
        try {
            const res  = await fetch(`/timeline_weave/history/${id}`);
            const data = await res.json();
            if (data.status === "ok" && data.report) {
                _toggleTab("report");
                _renderReport(data.report);
            }
        } catch(e) {
            console.error("[TLW] load report error:", e);
        }
    }

    async function _deleteReport(id, btn) {
        if (!confirm("Delete this timeline?")) return;
        btn.innerHTML = `<i class="fa-solid fa-circle-notch fa-spin"></i>`;
        try {
            await fetch(`/timeline_weave/history/${id}`, { method: "DELETE" });
            btn.closest(".tlw-history-item").remove();
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
        document.getElementById("tlwOverlay").classList.add("tlw-active");
    }

    function close() {
        const overlay = document.getElementById("tlwOverlay");
        if (overlay) overlay.classList.remove("tlw-active");
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
            ? window.currentSessionId : "tlw-session";

        document.getElementById("tlwIdleState").style.display     = "none";
        document.getElementById("tlwReportContent").style.display = "none";
        document.getElementById("tlwTabsRow").style.display       = "none";

        const dlBtn = document.getElementById("tlwDownloadBtn");
        if (dlBtn) dlBtn.style.display = "none";
        const regenBtn = document.getElementById("tlwRegenerateBtn");
        if (regenBtn) regenBtn.style.display = "none";

        const genBtn = document.getElementById("tlwGenerateBtn");
        genBtn.disabled = true;
        genBtn.innerHTML = `<span class="nexora-spinner inline"></span> Weaving…`;

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
            const res = await fetch("/timeline_weave/generate", {
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
            if (data.status !== "ok" || !data.report) throw new Error(data.message || "No timeline returned");

            _setProgress(100, "Timeline ready!", PROGRESS_STEPS.length - 1);
            await new Promise(r => setTimeout(r, 500));

            _showProgress(false);
            _renderReport(data.report);

        } catch(e) {
            clearInterval(stepTimer);
            _showProgress(false);
            document.getElementById("tlwIdleState").style.display = "flex";
            _toast("Timeline generation failed: " + e.message, "error");
            console.error("[TLW] generate error:", e);
        } finally {
            _generating = false;
            genBtn.disabled = false;
            genBtn.innerHTML = `<i class="fa-solid fa-wand-magic-sparkles"></i> Weave Timeline`;
            const regenBtn = document.getElementById("tlwRegenerateBtn");
            if (regenBtn) {
                regenBtn.disabled = false;
                regenBtn.innerHTML = `<i class="fa-solid fa-wand-magic-sparkles"></i> Regenerate`;
            }
        }
    }

    async function downloadPDF() {
        if (!_currentReport) {
            _toast("No timeline loaded yet.", "warning");
            return;
        }
        const btn = document.getElementById("tlwDownloadBtn");
        btn.disabled = true;
        btn.innerHTML = `<span class="nexora-spinner inline"></span> Preparing PDF…`;

        try {
            const res = await fetch("/timeline_weave/export_pdf", {
                method:  "POST",
                headers: { "Content-Type": "application/json" },
                body:    JSON.stringify({ report: _currentReport }),
            });

            if (!res.ok) throw new Error(`Server error ${res.status}`);

            const blob = await res.blob();
            const disposition = res.headers.get("Content-Disposition") || "";
            const nameMatch   = disposition.match(/filename="?([^"]+)"?/);
            const fileName    = nameMatch ? nameMatch[1] : `nexora_timeline_weave.pdf`;

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

    // Hook into global functions so TimelineWeave closes when returning to chatbot or opening other features
    if (typeof window.backToChatbot === "function") {
        originalBackToChatbot = window.backToChatbot;
        window.backToChatbot = function() {
            try {
                const overlay = document.getElementById("tlwOverlay");
                if (overlay) overlay.classList.remove("tlw-active");
            } catch(e){}
            if (typeof originalBackToChatbot === "function") {
                originalBackToChatbot.apply(this, arguments);
            }
        };
    }
    if (typeof window.openFeatureInWorkspace === "function") {
        const originalOpenFeatureInWorkspace = window.openFeatureInWorkspace;
        window.openFeatureInWorkspace = function(featureName, openFunc) {
            if (featureName !== 'TimelineWeave') {
                try {
                    const overlay = document.getElementById("tlwOverlay");
                    if (overlay) overlay.classList.remove("tlw-active");
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
