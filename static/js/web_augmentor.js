/**
 * web_augmentor.js  —  Nexora AI  |  Web-Grounded Research Augmentor
 * ===================================================================
 * Renders the full "Doc vs. World" report inside featureWorkspace
 * and provides a PDF download button that posts to /web_augmentor/export_pdf.
 *
 * Include in index.html:
 *   <script src="{{ url_for('static', filename='js/web_augmentor.js') }}" defer></script>
 *   <link  rel="stylesheet" href="{{ url_for('static', filename='css/web_augmentor.css') }}">
 *
 * Trigger from Explore Features dropdown:
 *   onclick="openFeatureInWorkspace('TrendLens', () => WebAugmentor.open())"
 *
 * Add to app.py:
 *   from web_augmentor import web_augmentor_bp
 *   app.register_blueprint(web_augmentor_bp)
 */

const WebAugmentor = (() => {
    "use strict";

    // ── State ─────────────────────────────────────────────────────────────
    let _injected    = false;
    let _generating  = false;
    let _currentReport = null;          // last fully loaded ReportJSON

    // ── Verdict config ────────────────────────────────────────────────────
    const VERDICTS = {
        confirmed:           { label: `<i class="fa-solid fa-circle-check" style="margin-right: 4px;"></i> Confirmed`,          color: "#22c55e", bg: "#f0fdf4", border: "#bbf7d0" },
        partially_outdated:  { label: `<i class="fa-solid fa-triangle-exclamation" style="margin-right: 4px;"></i> Partially Outdated`, color: "#f59e0b", bg: "#fffbeb", border: "#fde68a" },
        contradicted:        { label: `<i class="fa-solid fa-circle-xmark" style="margin-right: 4px;"></i> Contradicted`,        color: "#ef4444", bg: "#fef2f2", border: "#fecaca" },
        new_development:     { label: `<i class="fa-solid fa-circle-plus" style="margin-right: 4px;"></i> New Development`,    color: "#3b82f6", bg: "#eff6ff", border: "#bfdbfe" },
        no_data:             { label: `<i class="fa-solid fa-minus" style="margin-right: 4px;"></i> No Data`,             color: "#6b7280", bg: "#f9fafb", border: "#e5e7eb" },
    };

    const OVERALL_VERDICTS = {
        validated: { label: "Validated",     color: "#22c55e", icon: "fa-circle-check"    },
        mixed:     { label: "Mixed Results", color: "#f59e0b", icon: "fa-circle-half-stroke" },
        outdated:  { label: "Needs Review",  color: "#ef4444", icon: "fa-circle-exclamation" },
    };

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
        { pct: 8,  icon: 'fa-file-lines',       label: "Fetching document chunks from ChromaDB…" },
        { pct: 18, icon: 'fa-brain',            label: "Extracting key topics with AI…" },
        { pct: 28, icon: 'fa-earth-americas',    label: "Searching web for articles & news…" },
        { pct: 42, icon: 'fa-x-twitter',        label: "Pulling latest tweets & reviews…", isBrand: true },
        { pct: 56, icon: 'fa-flask',            label: "Scanning research papers & blogs…" },
        { pct: 68, icon: 'fa-scale-balanced',   label: "Comparing doc claims vs web evidence…" },
        { pct: 78, icon: 'fa-chart-simple',     label: "Scoring trends & synthesising topics…" },
        { pct: 88, icon: 'fa-pen-nib',          label: "Writing executive summary…" },
        { pct: 95, icon: 'fa-flag-checkered',   label: "Finalising report…" }
    ];

    // ── Inject once ───────────────────────────────────────────────────────
    function _inject() {
        if (_injected) return;
        _injected = true;

        // ── CSS ──────────────────────────────────────────────────────────
        const style = document.createElement("style");
        style.textContent = `
        /* ── Overlay (same pattern as cluster/mindmap) ── */
        #warOverlay {
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
        #warOverlay.war-active { display: flex; }

        /* ── Modal shell ── */
        #warModal {
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
            animation: war-slide-up 0.22s cubic-bezier(0.22,1,0.36,1);
            font-family: var(--font-body), sans-serif;
        }
        @keyframes war-slide-up {
            from { opacity:0; transform:translateY(18px) scale(0.98); }
            to   { opacity:1; transform:translateY(0) scale(1);        }
        }

        /* ── Override styles when inside #featureWorkspace to fit exactly like graph.js ── */
        #featureWorkspace #warOverlay {
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
        #featureWorkspace #warOverlay.war-active {
            display: flex !important;
            align-items: stretch !important;
            justify-content: stretch !important;
        }
        #featureWorkspace #warModal {
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
        #warHeader {
            display: flex;
            align-items: center;
            gap: 12px;
            padding: 16px 22px;
            border-bottom: 1px solid var(--border);
            background: var(--accent-bg);
            flex-shrink: 0;
        }
        #warHeader h3 {
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
        #warHeader h3 i {
            -webkit-text-fill-color: initial;
            color: var(--accent);
        }
        #warHeader .war-icon {
            color: var(--accent);
            font-size: 1.2rem;
        }
        #warHeader .hdr-subtitle {
            font-size: 0.72rem !important;
            font-style: italic !important;
            color: var(--text-secondary, #9aa0b4) !important;
            display: block !important;
            margin-top: 2px !important;
        }
        .war-header-right {
            margin-left: auto;
            display: flex;
            align-items: center;
            gap: 10px;
            flex-wrap: wrap;
        }
        .war-header-btn {
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
        .war-header-btn:hover { background: var(--accent-glow-soft); border-color: var(--border-hover); }
        .war-header-btn:disabled { opacity: 0.45; cursor: not-allowed; }
        .war-header-btn.primary {
            background: var(--grad-button) !important;
            color: var(--swal-btn-text) !important;
            border: none;
            box-shadow: 0 4px 14px var(--accent-glow-soft);
        }
        .war-header-btn.primary:hover:not(:disabled) {
            filter: brightness(1.08);
            transform: translateY(-1px);
            box-shadow: 0 6px 18px var(--accent-glow);
        }
        #warCloseBtn {
            background: transparent;
            border: none; cursor: pointer;
            color: var(--text-muted); font-size: 1.2rem;
            padding: 4px 8px; border-radius: 6px;
            transition: color .2s, background .2s;
            display: flex; align-items: center; justify-content: center;
        }
        #warCloseBtn:hover { color: var(--accent); background: var(--accent-bg); }

        #warStats {
            display: flex; gap: 10px; font-size: .72rem;
            color: var(--text-secondary);
            align-items: center;
        }
        #warStats span {
            background: var(--bg-glass);
            border: 1px solid var(--border);
            border-radius: 20px; padding: 3px 10px;
        }
        #warStats b { color: var(--accent); }

        /* ── Body ── */
        #warBody {
            padding: 22px 26px;
            overflow-y: auto;
            flex: 1;
            scroll-behavior: smooth;
        }
        #warBody::-webkit-scrollbar { width: 5px; }
        #warBody::-webkit-scrollbar-track { background: transparent; }
        #warBody::-webkit-scrollbar-thumb { background: var(--accent-glow); border-radius: 99px; }

        /* ── Idle / loading state ── */
        .war-state {
            display: flex;
            flex-direction: column;
            align-items: center;
            justify-content: center;
            gap: 14px;
            padding: 60px 20px;
            text-align: center;
            color: var(--text-secondary);
        }
        .war-state i { font-size: 2.4rem; color: var(--accent); opacity: 0.7; margin-bottom: 4px; }

        /* ── Progress bar & stream ── */
        #warProgressWrap {
            margin: 25px auto;
            max-width: 620px;
            padding: 24px;
            background: var(--bg-glass-deep);
            border: 1px solid var(--border);
            border-radius: 20px;
            box-shadow: 0 12px 40px rgba(0,0,0,0.3);
            display: none;
        }
        .war-progress-track {
            height: 4px;
            background: var(--accent-glow-soft);
            border-radius: 99px;
            overflow: hidden;
            margin-bottom: 22px;
        }
        .war-progress-fill {
            height: 100%;
            width: 0%;
            background: var(--grad-button);
            border-radius: 99px;
            transition: width 0.7s cubic-bezier(0.4,0,0.2,1);
        }
        .war-progress-stream {
            display: flex;
            flex-direction: column;
            gap: 16px;
            position: relative;
            padding-left: 28px;
            text-align: left;
        }
        .war-progress-stream::before {
            content: "";
            position: absolute;
            left: 9px;
            top: 4px;
            bottom: 4px;
            width: 2px;
            border-left: 2px dashed rgba(255,255,255,0.15);
        }
        body.light-mode .war-progress-stream::before {
            border-left-color: rgba(0,0,0,0.15);
        }
        .war-stream-step {
            display: flex;
            align-items: center;
            gap: 14px;
            position: relative;
            font-size: 12px;
            color: var(--text-muted);
            transition: color 0.4s ease;
        }
        .war-stream-node {
            position: absolute;
            left: -28px;
            width: 20px;
            height: 20px;
            border-radius: 50%;
            background: var(--bg-glass-deep);
            border: 2px solid var(--border);
            display: flex;
            align-items: center;
            justify-content: center;
            font-size: 10px;
            color: var(--text-muted);
            z-index: 2;
            transition: all 0.4s ease;
        }
        .war-stream-step.active {
            color: var(--accent);
            font-weight: 600;
        }
        .war-stream-step.active .war-stream-node {
            border-color: var(--accent);
            background: var(--accent-bg);
            color: var(--accent-light);
            box-shadow: 0 0 10px var(--accent-glow);
        }
        .war-stream-step.done {
            color: var(--text-primary);
        }
        .war-stream-step.done .war-stream-node {
            border-color: #22c55e;
            background: rgba(34,197,94,0.15);
            color: #22c55e;
        }

        /* ── Executive summary card ── */
        .war-exec-card {
            border: 1px solid var(--border);
            border-radius: 16px;
            padding: 20px 22px;
            margin-bottom: 22px;
            background: var(--bg-glass);
            display: flex;
            gap: 18px;
            align-items: flex-start;
            animation: war-fade-in 0.4s ease;
        }
        @keyframes war-fade-in { from{opacity:0;transform:translateY(10px)} to{opacity:1;transform:translateY(0)} }

        .war-verdict-badge {
            flex-shrink: 0;
            display: flex;
            flex-direction: column;
            align-items: center;
            gap: 6px;
            padding: 14px 18px;
            border-radius: 12px;
            font-family: var(--font-display), sans-serif;
        }
        .war-verdict-badge i { font-size: 1.8rem; }
        .war-verdict-badge span { font-size: 11px; font-weight: 700; letter-spacing: .04em; text-transform: uppercase; }

        .war-exec-content h3 {
            font-size: 13px;
            font-weight: 700;
            color: var(--text-muted);
            text-transform: uppercase;
            letter-spacing: .07em;
            margin-bottom: 8px;
            font-family: var(--font-display), sans-serif;
        }
        .war-exec-content p {
            font-size: 14px;
            line-height: 1.7;
            color: var(--text-primary);
            margin: 0;
        }

        /* ── Freshness pill ── */
        .war-freshness {
            display: inline-flex;
            align-items: center;
            gap: 6px;
            font-size: 11px;
            padding: 3px 10px;
            border-radius: 20px;
            border: 1px solid var(--border);
            color: var(--text-muted);
            background: var(--bg-glass);
            margin-top: 10px;
        }
        .war-freshness i { color: #22c55e; font-size: 8px; }

        /* ── Section heading ── */
        .war-section-heading {
            display: flex;
            align-items: center;
            gap: 10px;
            margin: 24px 0 14px;
            font-family: var(--font-display), sans-serif;
            font-size: 13px;
            font-weight: 700;
            color: var(--text-secondary);
            text-transform: uppercase;
            letter-spacing: .07em;
            padding-bottom: 8px;
            border-bottom: 1px solid var(--border);
        }
        .war-section-heading i { color: var(--accent); font-size: 14px; }

        /* ── Topic card ── */
        .war-topic-card {
            border: 1px solid var(--border);
            border-radius: 16px;
            overflow: hidden;
            margin-bottom: 16px;
            animation: war-fade-in 0.4s ease;
        }
        .war-topic-card:hover { border-color: var(--border-hover); }

        .war-topic-header {
            display: flex;
            align-items: center;
            justify-content: space-between;
            padding: 14px 18px;
            background: var(--bg-glass);
            border-bottom: 1px solid var(--border);
            cursor: pointer;
            gap: 12px;
            flex-wrap: wrap;
        }
        .war-topic-header:hover { background: var(--accent-bg); }

        .war-topic-title {
            font-size: 15px;
            font-weight: 700;
            color: var(--text-primary);
            font-family: var(--font-display), sans-serif;
        }
        .war-topic-num {
            font-size: 10px;
            font-weight: 600;
            color: var(--text-muted);
            letter-spacing: .06em;
            text-transform: uppercase;
            display: block;
        }

        .war-topic-badges {
            display: flex;
            align-items: center;
            gap: 8px;
            flex-wrap: wrap;
        }
        .war-verdict-pill {
            font-size: 11px;
            font-weight: 700;
            padding: 4px 12px;
            border-radius: 20px;
            border: 1px solid;
        }
        .war-trend-badge {
            font-size: 11px;
            padding: 3px 10px;
            border-radius: 20px;
            background: var(--bg-glass);
            border: 1px solid var(--border);
            color: var(--text-secondary);
            display: flex;
            align-items: center;
            gap: 4px;
        }
        .war-chevron {
            color: var(--text-muted);
            font-size: 12px;
            transition: transform 0.2s;
        }
        .war-topic-card.open .war-chevron { transform: rotate(180deg); }

        /* ── Topic body (collapsible) ── */
        .war-topic-body {
            display: none;
            padding: 18px;
            gap: 20px;
        }
        .war-topic-card.open .war-topic-body { display: grid; grid-template-columns: 1fr 1fr; }
        @media (max-width: 720px) {
            .war-topic-card.open .war-topic-body { grid-template-columns: 1fr; }
        }

        .war-col-label {
            font-size: 11px;
            font-weight: 700;
            letter-spacing: .07em;
            text-transform: uppercase;
            margin-bottom: 10px;
        }
        .war-doc-claim {
            font-size: 13px;
            line-height: 1.65;
            color: var(--text-primary);
            margin: 0 0 14px;
        }
        .war-synthesis-box {
            padding: 12px 14px;
            border-radius: 10px;
            border-left: 3px solid var(--accent);
            background: var(--accent-bg);
            font-size: 12px;
            line-height: 1.6;
            color: var(--text-primary);
        }
        .war-synthesis-box .war-synthesis-label {
            font-size: 10px;
            font-weight: 700;
            color: var(--accent);
            text-transform: uppercase;
            letter-spacing: .07em;
            margin-bottom: 6px;
        }

        /* ── Web evidence items ── */
        .war-evidence-item {
            border: 1px solid var(--border);
            border-radius: 10px;
            padding: 10px 12px;
            margin-bottom: 8px;
            background: var(--bg-glass);
            transition: border-color 0.15s;
        }
        .war-evidence-item:hover { border-color: var(--border-hover); }
        .war-evidence-type {
            font-size: 10px;
            font-weight: 600;
            color: var(--text-muted);
            text-transform: uppercase;
            letter-spacing: .05em;
            margin-bottom: 4px;
        }
        .war-evidence-title {
            font-size: 13px;
            font-weight: 600;
            color: var(--text-primary);
            margin-bottom: 4px;
            line-height: 1.4;
        }
        .war-evidence-snippet {
            font-size: 11.5px;
            color: var(--text-secondary);
            line-height: 1.5;
            margin-bottom: 6px;
        }
        .war-evidence-url {
            font-size: 10.5px;
            color: var(--accent);
            text-decoration: none;
            word-break: break-all;
            opacity: 0.8;
        }
        .war-evidence-url:hover { opacity: 1; text-decoration: underline; }

        /* ── Opportunity / Risk panels ── */
        .war-opp-risk-grid {
            display: grid;
            grid-template-columns: 1fr 1fr;
            gap: 16px;
            margin-bottom: 22px;
            animation: war-fade-in 0.5s ease;
        }
        @media (max-width: 640px) { .war-opp-risk-grid { grid-template-columns: 1fr; } }

        .war-opp-panel, .war-risk-panel {
            border: 1px solid;
            border-radius: 14px;
            padding: 16px 18px;
        }
        .war-opp-panel { border-color: #22c55e44; background: rgba(34,197,94,.05); }
        .war-risk-panel { border-color: #ef444444; background: rgba(239,68,68,.05); }

        .war-panel-title {
            font-size: 11px;
            font-weight: 700;
            text-transform: uppercase;
            letter-spacing: .07em;
            margin-bottom: 10px;
            font-family: var(--font-display), sans-serif;
        }
        .war-opp-panel .war-panel-title { color: #22c55e; }
        .war-risk-panel .war-panel-title { color: #ef4444; }

        .war-opp-panel ul, .war-risk-panel ul {
            list-style: none;
            padding: 0;
            margin: 0;
        }
        .war-opp-panel li, .war-risk-panel li {
            font-size: 12.5px;
            color: var(--text-primary);
            line-height: 1.5;
            padding: 6px 0;
            border-bottom: 1px solid var(--border);
            display: flex;
            gap: 8px;
        }
        .war-opp-panel li:last-child, .war-risk-panel li:last-child { border-bottom: none; }

        /* ── Sources footer ── */
        .war-sources-grid {
            display: grid;
            grid-template-columns: repeat(auto-fill, minmax(220px, 1fr));
            gap: 8px;
            animation: war-fade-in 0.5s ease;
        }
        .war-source-tag {
            font-size: 11px;
            padding: 6px 10px;
            border-radius: 8px;
            border: 1px solid var(--border);
            background: var(--bg-glass);
            color: var(--text-secondary);
            display: flex;
            align-items: center;
            gap: 6px;
            overflow: hidden;
            text-decoration: none;
            transition: border-color 0.15s, color 0.15s;
        }
        .war-source-tag:hover { border-color: var(--border-hover); color: var(--accent); }
        .war-source-tag span { white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }

        /* ── History panel ── */
        .war-history-item {
            display: flex;
            align-items: center;
            gap: 12px;
            padding: 10px 14px;
            border: 1px solid var(--border);
            border-radius: 12px;
            margin-bottom: 8px;
            background: var(--bg-glass);
            cursor: pointer;
            transition: all 0.15s;
        }
        .war-history-item:hover { border-color: var(--border-hover); background: var(--accent-bg); }
        .war-history-meta { flex: 1; min-width: 0; }
        .war-history-title { font-size: 13px; font-weight: 600; color: var(--text-primary); margin-bottom: 3px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
        .war-history-sub   { font-size: 11px; color: var(--text-muted); }
        .war-history-del   { background: none; border: none; color: var(--text-muted); cursor: pointer; padding: 4px 6px; border-radius: 6px; font-size: 12px; }
        .war-history-del:hover { color: #ef4444; background: rgba(239,68,68,.1); }

        /* ── Tab buttons ── */
        .war-tabs {
            display: flex;
            gap: 4px;
            margin-bottom: 18px;
            border-bottom: 1px solid var(--border);
            padding-bottom: 0;
        }
        .war-tab-btn {
            background: none; border: none;
            padding: 8px 16px;
            font-size: 13px;
            font-weight: 600;
            color: var(--text-muted);
            cursor: pointer;
            border-bottom: 2px solid transparent;
            margin-bottom: -1px;
            transition: all 0.18s;
            font-family: var(--font-display), sans-serif;
            display: flex; align-items: center; gap: 6px;
        }
        .war-tab-btn:hover { color: var(--text-primary); }
        .war-tab-btn.active { color: var(--accent); border-bottom-color: var(--accent); }
        `;
        document.head.appendChild(style);

        // ── HTML ──────────────────────────────────────────────────────────
        const overlay = document.createElement("div");
        overlay.id = "warOverlay";
        overlay.innerHTML = `
        <div id="warModal">
            <!-- Header -->
            <div id="warHeader">
                <div class="kg-header-left" style="display:flex;align-items:center;gap:12px">
                    <i class="fa-solid fa-earth-americas war-icon"></i>
                    <div>
                        <h3 style="margin: 0;">TrendLens</h3>
                        <span class="hdr-subtitle">Compares your documents against live web intelligence — news, tweets, reviews, research &amp; trends.</span>
                    </div>
                </div>
                <div class="war-header-right">
                    <div id="warStats">
                        <span>No report generated</span>
                    </div>
                    <button class="war-header-btn" id="warHistoryBtn" onclick="WebAugmentor._toggleTab('history')">
                        <i class="fa-solid fa-clock-rotate-left"></i> History
                    </button>
                    <button class="war-header-btn" id="warDownloadBtn" onclick="WebAugmentor.downloadPDF()" style="display:none">
                        <i class="fa-solid fa-file-arrow-down"></i> Download PDF
                    </button>
                </div>
            </div>

            <!-- Body -->
            <div id="warBody">

                <!-- Progress -->
                <div id="warProgressWrap">
                    <div style="font-family: var(--font-display); font-size: 13px; font-weight: 700; color: var(--text-primary); margin-bottom: 16px; display: flex; align-items: center; gap: 8px;">
                        <i class="fa-solid fa-circle-notch fa-spin" style="color: var(--accent);"></i>
                        <span>Researching Live Intelligence...</span>
                    </div>
                    <div class="war-progress-track">
                        <div class="war-progress-fill" id="warProgressFill"></div>
                    </div>
                    <div id="warProgressStepsRow" class="war-progress-stream"></div>
                </div>

                <!-- Tabs: Report / History -->
                <div id="warTabsRow" class="war-tabs" style="display:none">
                    <button class="war-tab-btn active" id="warTabReport"  onclick="WebAugmentor._toggleTab('report')">
                        <i class="fa-solid fa-file-lines"></i> Report
                    </button>
                    <button class="war-tab-btn"         id="warTabHistory" onclick="WebAugmentor._toggleTab('history')">
                        <i class="fa-solid fa-clock-rotate-left"></i> History
                    </button>
                </div>

                <!-- Report panel -->
                <div id="warReportPanel">
                    <!-- Idle state -->
                    <div class="war-state" id="warIdleState">
                        <i class="fa-solid fa-earth-americas" style="font-size: 32px; color: var(--accent); margin-bottom: 8px;"></i>
                        <h3 style="margin:0;font-family:var(--font-display);font-size:1.1rem;color:var(--text-primary)">Web-Grounded Intelligence</h3>
                        <p style="margin:0 0 12px;font-size:.88rem;max-width:480px;line-height:1.5">
                            Select documents from the sidebar, then click <strong>Generate Report</strong> to compare your docs against live news, articles, tweets, reviews, and research.
                        </p>
                        <button id="warGenerateBtn" onclick="WebAugmentor.generate()"
                            style="background:var(--grad-button);color:var(--swal-btn-text);border:none;padding:8px 16px;border-radius:10px;font-family:var(--font-display);font-size:0.8rem;font-weight:600;cursor:pointer;box-shadow:0 4px 12px var(--accent-glow);display:inline-flex;align-items:center;gap:8px">
                            <i class="fa-solid fa-wand-magic-sparkles"></i> Generate Report
                        </button>
                    </div>
                    <!-- Rendered report injected here -->
                    <div id="warReportContent" style="display:none"></div>
                </div>

                <!-- History panel -->
                <div id="warHistoryPanel" style="display:none">
                    <div class="war-section-heading"><i class="fa-solid fa-clock-rotate-left"></i> Saved Reports</div>
                    <div id="warHistoryList"><div class="war-state"><i class="fa-solid fa-circle-notch fa-spin"></i><span>Loading…</span></div></div>
                </div>

            </div>
        </div>`;

        (document.getElementById("featureWorkspace") || document.body).appendChild(overlay);

        // Close on backdrop
        overlay.addEventListener("click", e => { if (e.target === overlay) WebAugmentor.close(); });
    }

    // ── Helpers ───────────────────────────────────────────────────────────
    function _getSelectedFiles() {
        const checked = [];
        document.querySelectorAll("#documentCheckboxList input:checked")
            .forEach(cb => checked.push(cb.value));
        return checked;
    }

    function _setProgress(pct, label, stepIdx) {
        const fill = document.getElementById("warProgressFill");
        if (fill) fill.style.width = pct + "%";

        const row = document.getElementById("warProgressStepsRow");
        if (row && stepIdx !== undefined) {
            row.querySelectorAll(".war-stream-step").forEach((step, i) => {
                step.classList.remove("active", "done");
                const node = step.querySelector(".war-stream-node");
                
                if (i < stepIdx || pct === 100) {
                    step.classList.add("done");
                    if (node) node.innerHTML = `<i class="fa-solid fa-circle-check"></i>`;
                } else if (i === stepIdx) {
                    step.classList.add("active");
                    const iconClass = PROGRESS_STEPS[i].isBrand ? 'fa-brands' : 'fa-solid';
                    if (node) node.innerHTML = `<i class="${iconClass} ${PROGRESS_STEPS[i].icon} fa-spin"></i>`;
                } else {
                    const iconClass = PROGRESS_STEPS[i].isBrand ? 'fa-brands' : 'fa-solid';
                    if (node) node.innerHTML = `<i class="${iconClass} ${PROGRESS_STEPS[i].icon}"></i>`;
                }
            });
        }
    }

    function _showProgress(show) {
        const wrap = document.getElementById("warProgressWrap");
        if (!wrap) return;
        if (show) {
            const row = document.getElementById("warProgressStepsRow");
            row.innerHTML = PROGRESS_STEPS.map((s, i) => {
                const iconClass = s.isBrand ? 'fa-brands' : 'fa-solid';
                return `<div class="war-stream-step" data-idx="${i}">
                    <div class="war-stream-node">
                        <i class="${iconClass} ${s.icon}"></i>
                    </div>
                    <span>${_escHtml(s.label)}</span>
                </div>`;
            }).join("");
            wrap.style.display = "block";
            document.getElementById("warProgressFill").style.width = "0%";
        } else {
            wrap.style.display = "none";
        }
    }

    function _statBar(report) {
        const bar = document.getElementById("warStatBar");
        if (!bar || !report) { if(bar) bar.textContent=""; return; }
        const topicCount = (report.topics || []).length;
        const srcCount   = (report.sources  || []).length;
        bar.textContent  = `· ${topicCount} topics · ${srcCount} sources · ${report.freshness_label || ""}`;
    }

    function _escHtml(str) {
        return String(str || "")
            .replace(/&/g,"&amp;").replace(/</g,"&lt;")
            .replace(/>/g,"&gt;").replace(/"/g,"&quot;");
    }

    // ── Tab switching ─────────────────────────────────────────────────────
    function _toggleTab(tab) {
        const reportPanel  = document.getElementById("warReportPanel");
        const historyPanel = document.getElementById("warHistoryPanel");
        const tabReport    = document.getElementById("warTabReport");
        const tabHistory   = document.getElementById("warTabHistory");
        const tabsRow      = document.getElementById("warTabsRow");

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

        document.getElementById("warIdleState").style.display = "none";
        const content = document.getElementById("warReportContent");
        content.style.display = "block";
        content.innerHTML     = "";

        const ov     = OVERALL_VERDICTS[report.overall_verdict] || OVERALL_VERDICTS.mixed;
        const genAt  = report.generated_at || "";
        const fresh  = report.freshness_label || "";

        // ── Executive summary ───────────────────────────────────────────
        const execCard = document.createElement("div");
        execCard.className = "war-exec-card";
        execCard.innerHTML = `
            <div class="war-verdict-badge" style="background:${ov.color}18;color:${ov.color}">
                <i class="fa-solid ${ov.icon}" style="font-size:1.8rem"></i>
                <span>${_escHtml(ov.label)}</span>
            </div>
            <div class="war-exec-content" style="flex:1">
                <h3><i class="fa-solid fa-clipboard-list" style="margin-right: 6px;"></i> Executive Summary</h3>
                <p>${_escHtml(report.executive_summary || "")}</p>
                <div style="display:flex;gap:10px;flex-wrap:wrap;margin-top:10px">
                    <span class="war-freshness"><i class="fa-solid fa-circle" style="color:#22c55e;font-size:8px"></i>${_escHtml(fresh)}</span>
                    <span class="war-freshness"><i class="fa-solid fa-file-lines"></i>${_escHtml((report.doc_names||[]).join(", "))}</span>
                    <span class="war-freshness"><i class="fa-regular fa-clock"></i>${_escHtml(genAt)}</span>
                </div>
            </div>`;
        content.appendChild(execCard);

        // ── Topics ──────────────────────────────────────────────────────
        const topicHeading = document.createElement("div");
        topicHeading.className = "war-section-heading";
        topicHeading.innerHTML = `<i class="fa-solid fa-magnifying-glass-chart"></i> Topic Intelligence Breakdown`;
        content.appendChild(topicHeading);

        (report.topics || []).forEach((topic, idx) => {
            const vc = VERDICTS[topic.verdict] || VERDICTS.no_data;
            const ts = topic.trend_score || 0;
            const card = document.createElement("div");
            card.className = "war-topic-card";
            card.id = `warTopic_${idx}`;

            // Evidence HTML
            const evidenceHtml = (topic.web_evidence || []).slice(0,5).map(ev => {
                const icon = SOURCE_ICONS[ev.source_type] || SOURCE_ICONS.default;
                return `<div class="war-evidence-item">
                    <div class="war-evidence-type">${icon} ${_escHtml(ev.source_type || "")}</div>
                    <div class="war-evidence-title">${_escHtml(ev.title || "")}</div>
                    <div class="war-evidence-snippet">${_escHtml((ev.snippet || "").substring(0, 200))}</div>
                    <a class="war-evidence-url" href="${_escHtml(ev.url || "")}" target="_blank" rel="noopener">
                        ${_escHtml((ev.url || "").substring(0, 75))}${(ev.url||"").length > 75 ? "…" : ""}
                    </a>
                </div>`;
            }).join("") || `<div style="color:var(--text-muted);font-size:13px;padding:10px 0">No web evidence retrieved.</div>`;

            card.innerHTML = `
                <div class="war-topic-header" onclick="WebAugmentor._toggleTopic(${idx})">
                    <div>
                        <span class="war-topic-num">Topic ${idx + 1}</span>
                        <div class="war-topic-title">${_escHtml(topic.title || "")}</div>
                    </div>
                    <div class="war-topic-badges">
                        <span class="war-verdict-pill"
                              style="color:${vc.color};background:${vc.bg};border-color:${vc.color}44">
                            ${vc.label}
                        </span>
                        <span class="war-trend-badge"><i class="fa-solid fa-fire" style="color: #ff5a5f; margin-right: 4px;"></i> Trend ${ts}/100</span>
                        <i class="fa-solid fa-chevron-down war-chevron"></i>
                    </div>
                </div>
                <div class="war-topic-body">
                    <div>
                        <div class="war-col-label" style="color:var(--accent)">📄 Document Claims</div>
                        <p class="war-doc-claim">${_escHtml(topic.doc_claim || "")}</p>
                        <div class="war-synthesis-box">
                            <div class="war-synthesis-label">🧠 AI Synthesis</div>
                            ${_escHtml(topic.synthesis || "")}
                        </div>
                    </div>
                    <div>
                        <div class="war-col-label" style="color:#3b82f6">🌐 Live Web Intelligence</div>
                        ${evidenceHtml}
                    </div>
                </div>`;
            content.appendChild(card);
        });

        // Auto-open first topic
        if ((report.topics || []).length > 0) {
            setTimeout(() => _toggleTopic(0), 200);
        }

        // ── Opportunities & Risks ────────────────────────────────────────
        const orHeading = document.createElement("div");
        orHeading.className = "war-section-heading";
        orHeading.innerHTML = `<i class="fa-solid fa-bolt-lightning"></i> Opportunities & Risks Revealed by Web`;
        content.appendChild(orHeading);

        const orGrid = document.createElement("div");
        orGrid.className = "war-opp-risk-grid";

        const oppsHtml  = (report.opportunities || []).map(o => `<li><i class="fa-solid fa-arrow-trend-up" style="color: #22c55e; margin-top: 4px;"></i> <span>${_escHtml(o)}</span></li>`).join("") || "<li>None identified.</li>";
        const risksHtml = (report.risks         || []).map(r => `<li><i class="fa-solid fa-triangle-exclamation" style="color: #ef4444; margin-top: 4px;"></i> <span>${_escHtml(r)}</span></li>`).join("") || "<li>None identified.</li>";

        orGrid.innerHTML = `
            <div class="war-opp-panel">
                <div class="war-panel-title"><i class="fa-solid fa-rocket"></i> Opportunities</div>
                <ul>${oppsHtml}</ul>
            </div>
            <div class="war-risk-panel">
                <div class="war-panel-title"><i class="fa-solid fa-triangle-exclamation"></i> Risks Flagged</div>
                <ul>${risksHtml}</ul>
            </div>`;
        content.appendChild(orGrid);

        // ── Sources ──────────────────────────────────────────────────────
        if ((report.sources || []).length > 0) {
            const srcHeading = document.createElement("div");
            srcHeading.className = "war-section-heading";
            srcHeading.innerHTML = `<i class="fa-solid fa-link"></i> Web Sources Consulted`;
            content.appendChild(srcHeading);

            const srcGrid = document.createElement("div");
            srcGrid.className = "war-sources-grid";
            srcGrid.innerHTML = (report.sources || []).slice(0,20).map(s => {
                const icon = SOURCE_ICONS[s.type] || SOURCE_ICONS.default;
                return `<a class="war-source-tag" href="${_escHtml(s.url||"")}" target="_blank" rel="noopener">
                    <span style="flex-shrink:0">${icon}</span>
                    <span>${_escHtml(s.title || s.url || "")}</span>
                </a>`;
            }).join("");
            content.appendChild(srcGrid);
        }

        // Show download button
        const dlBtn = document.getElementById("warDownloadBtn");
        if (dlBtn) dlBtn.style.display = "flex";

        // Show tabs
        document.getElementById("warTabsRow").style.display = "flex";
        document.getElementById("warTabReport").classList.add("active");
        document.getElementById("warTabHistory").classList.remove("active");
    }

    // ── Topic toggle ─────────────────────────────────────────────────────
    function _toggleTopic(idx) {
        const card = document.getElementById(`warTopic_${idx}`);
        if (!card) return;
        card.classList.toggle("open");
    }

    // ── Load history ──────────────────────────────────────────────────────
    async function _loadHistory() {
        const list = document.getElementById("warHistoryList");
        if (!list) return;
        list.innerHTML = `<div class="war-state"><i class="fa-solid fa-circle-notch fa-spin"></i><span>Loading history…</span></div>`;
        try {
            const res  = await fetch("/web_augmentor/history");
            const data = await res.json();
            const reports = data.reports || [];

            if (reports.length === 0) {
                list.innerHTML = `<div class="war-state"><i class="fa-solid fa-inbox"></i><span>No saved reports yet.</span></div>`;
                return;
            }

            list.innerHTML = "";
            reports.forEach(r => {
                const ov = OVERALL_VERDICTS[r.overall_verdict] || OVERALL_VERDICTS.mixed;
                const item = document.createElement("div");
                item.className = "war-history-item";
                item.innerHTML = `
                    <i class="fa-solid fa-file-chart-column" style="color:var(--accent);font-size:20px;flex-shrink:0"></i>
                    <div class="war-history-meta">
                        <div class="war-history-title">${_escHtml(r.title || "Web-Grounded Report")}</div>
                        <div class="war-history-sub">
                            <span style="color:${ov.color};font-weight:600">${ov.label}</span>
                            · ${_escHtml((r.doc_names||[]).join(", ") || "—")}
                            · ${_escHtml(r.generated_at || "")}
                            · ${r.topic_count || 0} topics
                        </div>
                    </div>
                    <button class="war-history-del" onclick="event.stopPropagation();WebAugmentor._deleteReport('${_escHtml(r.id)}',this)" title="Delete">
                        <i class="fa-solid fa-trash"></i>
                    </button>`;
                item.addEventListener("click", () => WebAugmentor._loadReportById(r.id));
                list.appendChild(item);
            });
        } catch(e) {
            list.innerHTML = `<div class="war-state"><i class="fa-solid fa-triangle-exclamation"></i><span>Failed to load history.</span></div>`;
        }
    }

    // ── Load a historic report ────────────────────────────────────────────
    async function _loadReportById(id) {
        try {
            const res  = await fetch(`/web_augmentor/history/${id}`);
            const data = await res.json();
            if (data.status === "ok" && data.report) {
                _toggleTab("report");
                _renderReport(data.report);
            }
        } catch(e) {
            console.error("[WAR] load report error:", e);
        }
    }

    // ── Delete a report ───────────────────────────────────────────────────
    async function _deleteReport(id, btn) {
        if (!confirm("Delete this report?")) return;
        btn.innerHTML = `<i class="fa-solid fa-circle-notch fa-spin"></i>`;
        try {
            await fetch(`/web_augmentor/history/${id}`, { method: "DELETE" });
            btn.closest(".war-history-item").remove();
        } catch(e) {
            btn.innerHTML = `<i class="fa-solid fa-trash"></i>`;
        }
    }

    // ── Toast ─────────────────────────────────────────────────────────────
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

    /** Open the modal. */
    function open() {
        _inject();
        document.getElementById("warOverlay").classList.add("war-active");
        
        // Ensure breadcrumb bar is placed inside our header
        setTimeout(() => {
            const breadcrumbBar = document.getElementById("breadcrumbBar");
            const header = document.getElementById("warHeader");
            if (breadcrumbBar && header) {
                header.insertBefore(breadcrumbBar, header.firstChild);
                breadcrumbBar.style.display = "flex";
            }
        }, 150);
    }

    /** Close the modal. */
    function close() {
        const overlay = document.getElementById("warOverlay");
        if (overlay) overlay.classList.remove("war-active");
        if (typeof backToChatbot === "function") backToChatbot();
    }

    /** Run the full pipeline. */
    async function generate() {
        if (_generating) return;
        _generating = true;

        const files     = _getSelectedFiles();
        const sessionId = (typeof window.currentSessionId !== "undefined" && window.currentSessionId)
            ? window.currentSessionId : "war-session";

        // Hide idle state, show progress
        document.getElementById("warIdleState").style.display      = "none";
        document.getElementById("warReportContent").style.display  = "none";
        document.getElementById("warTabsRow").style.display        = "none";

        const dlBtn = document.getElementById("warDownloadBtn");
        if (dlBtn) dlBtn.style.display = "none";

        const genBtn = document.getElementById("warGenerateBtn");
        genBtn.disabled = true;
        genBtn.innerHTML = `<span class="nexora-spinner inline"></span> Generating…`;

        _showProgress(true);

        // Animate progress steps while request is in-flight
        let stepIdx = 0;
        _setProgress(PROGRESS_STEPS[0].pct, PROGRESS_STEPS[0].label, 0);
        const stepTimer = setInterval(() => {
            if (stepIdx < PROGRESS_STEPS.length - 2) {
                stepIdx++;
                const s = PROGRESS_STEPS[stepIdx];
                _setProgress(s.pct, s.label, stepIdx);
            }
        }, 3200);

        try {
            const res = await fetch("/web_augmentor/generate", {
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
            await new Promise(r => setTimeout(r, 600));

            _showProgress(false);
            _renderReport(data.report);

        } catch(e) {
            clearInterval(stepTimer);
            _showProgress(false);
            document.getElementById("warIdleState").style.display = "flex";
            _toast("Report generation failed: " + e.message, "error");
            console.error("[WAR] generate error:", e);
        } finally {
            _generating = false;
            genBtn.disabled = false;
            genBtn.innerHTML = `<i class="fa-solid fa-wand-magic-sparkles"></i> Generate Report`;
        }
    }

    /** Download current report as PDF. */
    async function downloadPDF() {
        if (!_currentReport) {
            _toast("No report loaded yet.", "warning");
            return;
        }
        const btn = document.getElementById("warDownloadBtn");
        btn.disabled = true;
        btn.innerHTML = `<span class="nexora-spinner inline"></span> Preparing PDF…`;

        try {
            const res = await fetch("/web_augmentor/export_pdf", {
                method:  "POST",
                headers: { "Content-Type": "application/json" },
                body:    JSON.stringify({ report: _currentReport }),
            });

            if (!res.ok) throw new Error(`Server error ${res.status}`);

            const blob = await res.blob();
            const disposition = res.headers.get("Content-Disposition") || "";
            const nameMatch   = disposition.match(/filename="?([^"]+)"?/);
            const fileName    = nameMatch ? nameMatch[1] : `nexora_web_report.pdf`;

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

    // Hook into global functions to ensure WebAugmentor closes when returning to chatbot or opening other features
    if (typeof window.backToChatbot === "function") {
        const originalBackToChatbot = window.backToChatbot;
        window.backToChatbot = function() {
            try { WebAugmentor.close(); } catch(e){}
            originalBackToChatbot.apply(this, arguments);
        };
    }
    if (typeof window.openFeatureInWorkspace === "function") {
        const originalOpenFeatureInWorkspace = window.openFeatureInWorkspace;
        window.openFeatureInWorkspace = function(featureName, openFunc) {
            if (featureName !== 'TrendLens') {
                try {
                    const overlay = document.getElementById("warOverlay");
                    if (overlay) overlay.classList.remove("war-active");
                } catch(e){}
            }
            originalOpenFeatureInWorkspace.apply(this, arguments);
        };
    }

    return {
        open, close, generate, downloadPDF,
        _toggleTab, _toggleTopic,
        _loadReportById, _deleteReport,
    };

})();
