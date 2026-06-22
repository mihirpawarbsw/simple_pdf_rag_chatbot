/**
 * nlp_analytics.js — Nexora AI  |  NLP Analytics Suite
 * ======================================================
 * Renders Sentiment, Word Cloud, Topic Modelling, Keyphrase Extractor,
 * Named Entity Recognition, and Readability Score inside a modal.
 *
 * Trigger: NLPAnalytics.open()
 * Requires: Font Awesome 6, CSS variables from style.css
 * No external chart library needed — all canvas/SVG drawn natively.
 */

const NLPAnalytics = (() => {

    // ── Palette ──────────────────────────────────────────────────────────
    let COLORS = [
        "#b18fcf","#7ec8e3","#f4a261","#57cc99",
        "#e76f51","#a8dadc","#ffd166","#c77dff",
        "#06d6a0","#ef476f","#118ab2","#ffc43d",
    ];
    let ENTITY_COLORS = {
        PERSON:   "#b18fcf", ORG:      "#7ec8e3",
        LOCATION: "#57cc99", DATE:     "#ffd166",
        PRODUCT:  "#f4a261", CONCEPT:  "#c77dff",
        OTHER:    "#a8a8a8",
    };

    function _updateColorsForMode() {
        const isLight = document.body.classList.contains("light-mode");
        if (isLight) {
            COLORS = [
                "#7c3aed", // lavender/purple
                "#0369a1", // blue
                "#15803d", // green
                "#c2410c", // orange
                "#b45309", // amber/dark yellow
                "#7e22ce", // purple deep
                "#be123c", // red
                "#0f766e", // teal
                "#1d4ed8", // dark blue
                "#b45309", // amber
                "#9a3412", // rust
                "#0369a1"  // slate blue
            ];
            ENTITY_COLORS = {
                PERSON:   "#7c3aed", ORG:      "#0369a1",
                LOCATION: "#15803d", DATE:     "#b45309",
                PRODUCT:  "#c2410c", CONCEPT:  "#7e22ce",
                OTHER:    "#475569",
            };
        } else {
            COLORS = [
                "#b18fcf","#7ec8e3","#f4a261","#57cc99",
                "#e76f51","#a8dadc","#ffd166","#c77dff",
                "#06d6a0","#ef476f","#118ab2","#ffc43d",
            ];
            ENTITY_COLORS = {
                PERSON:   "#b18fcf", ORG:      "#7ec8e3",
                LOCATION: "#57cc99", DATE:     "#ffd166",
                PRODUCT:  "#f4a261", CONCEPT:  "#c77dff",
                OTHER:    "#a8a8a8",
            };
        }
    }

    let _injected   = false;
    let _activeTab  = "sentiment";
    let _data       = null;

    function _inject() {
        if (_injected) return;
        _injected = true;

        const style = document.createElement("style");
        style.textContent = `
/* =============================================================
   nlp_analytics.css — Nexora AI  |  NLP Analytics Suite
   ============================================================= */

/* ── Toolbar button accent ────────────────────────────────────── */
.nlp-analytics-btn {
    position: relative;
}

.nlp-analytics-btn::after {
    content: "NLP";
    position: absolute;
    top: -6px;
    right: -6px;
    font-size: 0.52rem;
    font-weight: 800;
    letter-spacing: 0.04em;
    background: var(--accent);
    color: var(--bg-base);
    border-radius: 99px;
    padding: 1px 5px;
    pointer-events: none;
    opacity: 0.9;
}
body.light-mode .nlp-analytics-btn::after {
    color: #fff;
}

/* ── Overlay ──────────────────────────────────────────────────── */
#nlpOverlay {
    display: none;
    position: fixed;
    inset: 0;
    background: rgba(0, 0, 0, 0.72);
    backdrop-filter: blur(16px);
    -webkit-backdrop-filter: blur(16px);
    z-index: 1300;
    align-items: center;
    justify-content: center;
}

#nlpOverlay.nlp-active {
    display: flex;
}

/* ── Modal shell ─────────────────────────────────────────────── */
#nlpModal {
    background: var(--bg-glass-deep);
    border: 1px solid var(--border);
    border-radius: 24px;
    width: min(1060px, 97vw);
    max-height: 90vh;
    display: flex;
    flex-direction: column;
    overflow: hidden;
    box-shadow: 0 24px 64px rgba(0,0,0,0.6), 0 0 40px var(--accent-glow-soft);
    backdrop-filter: blur(30px);
    -webkit-backdrop-filter: blur(30px);
    animation: nlp-slide-up 0.22s cubic-bezier(0.22, 1, 0.36, 1);
    font-family: var(--font-body), sans-serif;
}

@keyframes nlp-slide-up {
    from { opacity: 0; transform: translateY(18px) scale(0.98); }
    to   { opacity: 1; transform: translateY(0)    scale(1);    }
}

/* ── Header ──────────────────────────────────────────────────── */
#nlpHeader {
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 18px 26px 14px;
    border-bottom: 1px solid var(--border);
    flex-shrink: 0;
}

#nlpHeader h2 {
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

#nlpHeader h2 i {
    color: var(--accent);
    -webkit-text-fill-color: initial;
}

#nlpStatBar {
    font-size: 0.75rem;
    color: var(--text-muted);
    margin-left: 8px;
    font-weight: 400;
    -webkit-text-fill-color: initial;
}

.nlp-hdr-right {
    display: flex;
    align-items: center;
    gap: 10px;
}

/* ── Run + Close buttons ──────────────────────────────────────── */
#nlpRunBtn {
    background: var(--accent-bg);
    border: 1px solid var(--accent-glow);
    color: var(--accent);
    border-radius: 10px;
    padding: 6px 16px;
    font-size: 0.8rem;
    cursor: pointer;
    display: flex;
    align-items: center;
    gap: 6px;
    transition: all 0.2s var(--transition);
    font-family: var(--font-display), sans-serif;
    font-weight: 600;
}

#nlpRunBtn:hover {
    background: var(--accent-glow-soft);
    border-color: var(--border-hover);
}

#nlpRunBtn:disabled {
    opacity: 0.4;
    cursor: not-allowed;
}

#nlpCloseBtn {
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

#nlpCloseBtn:hover {
    color: var(--accent-light);
    background: var(--accent-bg);
}
body.light-mode #nlpCloseBtn:hover {
    color: var(--accent);
}

/* ── Tab bar ──────────────────────────────────────────────────── */
#nlpTabs {
    display: flex;
    gap: 2px;
    padding: 10px 26px 0;
    border-bottom: 1px solid var(--border);
    flex-shrink: 0;
    overflow-x: auto;
    scrollbar-width: none;
}

#nlpTabs::-webkit-scrollbar {
    height: 0;
}

.nlp-tab {
    padding: 8px 16px;
    border: none;
    background: none;
    color: var(--text-muted);
    font-size: 0.8rem;
    font-weight: 600;
    cursor: pointer;
    border-radius: 8px 8px 0 0;
    white-space: nowrap;
    border-bottom: 2px solid transparent;
    transition: all 0.2s;
    display: flex;
    align-items: center;
    gap: 6px;
    font-family: var(--font-display), sans-serif;
}

.nlp-tab:hover {
    color: var(--text-secondary);
    background: var(--bg-glass);
}

.nlp-tab.active {
    color: var(--accent);
    border-bottom-color: var(--accent);
}

/* ── Body ────────────────────────────────────────────────────── */
#nlpBody {
    padding: 24px 26px;
    overflow-y: auto;
    flex: 1;
}

#nlpBody::-webkit-scrollbar {
    width: 5px;
}

#nlpBody::-webkit-scrollbar-thumb {
    background: var(--accent-glow);
    border-radius: 99px;
}

/* ── Empty / loading state ───────────────────────────────────── */
.nlp-state {
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    gap: 14px;
    padding: 70px 0;
    color: var(--text-muted);
    font-size: 0.9rem;
    text-align: center;
}

.nlp-state i {
    font-size: 2.4rem;
    opacity: 0.35;
}

/* ── Panel visibility ────────────────────────────────────────── */
.nlp-panel {
    display: none;
}

.nlp-panel.active {
    display: block;
    animation: nlp-fade-in 0.18s ease;
}

@keyframes nlp-fade-in {
    from { opacity: 0; }
    to   { opacity: 1; }
}

/* ── Sentiment ── */
.sent-overall {
    display: flex;
    align-items: center;
    gap: 20px;
    margin-bottom: 24px;
}

.sent-dial {
    position: relative;
    width: 130px;
    height: 130px;
    flex-shrink: 0;
}

.sent-dial canvas {
    width: 130px;
    height: 130px;
}

.sent-dial-label {
    position: absolute;
    inset: 0;
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
}

.sent-dial-pct {
    font-size: 1.4rem;
    font-weight: 800;
    font-family: var(--font-display), sans-serif;
    color: var(--text-primary);
}

.sent-dial-word {
    font-size: 0.72rem;
    color: var(--text-muted);
}

.sent-info h3 {
    margin: 0 0 6px;
    font-size: 1.1rem;
    font-family: var(--font-display), sans-serif;
    color: var(--text-primary);
}

.sent-info p {
    margin: 0;
    font-size: 0.82rem;
    color: var(--text-secondary);
}

.sent-breakdown {
    display: flex;
    flex-direction: column;
    gap: 10px;
}

.sent-row {
    display: grid;
    grid-template-columns: 160px 80px 1fr;
    align-items: center;
    gap: 12px;
}

.sent-row-name {
    font-size: 0.78rem;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
    color: var(--text-primary);
}

.sent-pill {
    font-size: 0.7rem;
    padding: 2px 10px;
    border-radius: 20px;
    text-align: center;
    font-weight: 600;
}

.sent-bar-wrap {
    height: 6px;
    background: var(--border);
    border-radius: 99px;
    overflow: hidden;
}

.sent-bar {
    height: 100%;
    border-radius: 99px;
    transition: width 0.6s cubic-bezier(0.22, 1, 0.36, 1);
}

/* ── Word Cloud ── */
#wcCanvas {
    width: 100%;
    height: 420px;
    display: block;
    cursor: default;
}

/* ── Topics ── */
.topic-grid {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(240px, 1fr));
    gap: 14px;
}

.topic-card {
    background: var(--bg-glass);
    border: 1px solid var(--border);
    border-radius: 12px;
    padding: 16px;
    border-left: 3px solid transparent;
    transition: all 0.2s;
}

.topic-card:hover {
    background: var(--bg-glass-deep);
    border-color: var(--border-hover);
}

.topic-card-top {
    display: flex;
    justify-content: space-between;
    align-items: center;
    margin-bottom: 10px;
}

.topic-label {
    font-size: 0.9rem;
    font-weight: 700;
    font-family: var(--font-display), sans-serif;
    color: var(--text-primary);
}

.topic-weight {
    font-size: 0.72rem;
    padding: 2px 8px;
    border-radius: 20px;
    font-weight: 600;
}

.topic-bar-wrap {
    height: 4px;
    background: var(--border);
    border-radius: 99px;
    overflow: hidden;
    margin-bottom: 12px;
}

.topic-bar {
    height: 100%;
    border-radius: 99px;
    transition: width 0.6s cubic-bezier(0.22, 1, 0.36, 1);
}

.topic-kws {
    display: flex;
    flex-wrap: wrap;
    gap: 5px;
}

.topic-kw {
    font-size: 0.68rem;
    padding: 2px 8px;
    border-radius: 20px;
    border: 1px solid var(--border);
    background: var(--bg-glass);
    color: var(--text-secondary);
    opacity: 0.8;
}

/* ── Keyphrases ── */
.kp-list {
    display: flex;
    flex-direction: column;
    gap: 8px;
}

.kp-row {
    display: grid;
    grid-template-columns: 1fr 120px 60px;
    align-items: center;
    gap: 12px;
    padding: 10px 14px;
    background: var(--bg-glass);
    border-radius: 10px;
    border: 1px solid var(--border);
    transition: all 0.2s;
}

.kp-row:hover {
    background: var(--bg-glass-deep);
    border-color: var(--border-hover);
}

.kp-phrase {
    font-size: 0.85rem;
    font-weight: 600;
    color: var(--text-primary);
}

.kp-src {
    font-size: 0.7rem;
    color: var(--text-muted);
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
}

.kp-score {
    font-size: 0.78rem;
    font-weight: 700;
    text-align: right;
}

/* ── NER ─────────────────────────────────────────────────────── */
.ner-section {
    margin-bottom: 22px;
}

.ner-type-label {
    font-size: 0.72rem;
    font-weight: 700;
    letter-spacing: 0.06em;
    margin-bottom: 10px;
    color: var(--text-muted);
    text-transform: uppercase;
    font-family: var(--font-display), sans-serif;
}

.ner-tags {
    display: flex;
    flex-wrap: wrap;
    gap: 8px;
}

.ner-tag {
    display: flex;
    align-items: center;
    gap: 6px;
    padding: 5px 12px;
    border-radius: 20px;
    border: 1px solid var(--border);
    background: var(--bg-glass);
    color: var(--text-secondary);
    font-size: 0.78rem;
    transition: all 0.2s;
}

.ner-tag:hover {
    border-color: var(--border-hover);
    background: var(--bg-glass-deep);
}

.ner-count {
    font-size: 0.68rem;
    padding: 1px 6px;
    border-radius: 99px;
    font-weight: 700;
    background: var(--accent-bg);
    color: var(--accent);
}

/* ── Readability ─────────────────────────────────────────────── */
.read-grid {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(160px, 1fr));
    gap: 14px;
    margin-bottom: 28px;
}

.read-card {
    background: var(--bg-glass);
    border: 1px solid var(--border);
    border-radius: 12px;
    padding: 18px;
    text-align: center;
    transition: all 0.2s;
}

.read-card:hover {
    background: var(--bg-glass-deep);
    border-color: var(--border-hover);
}

.read-val {
    font-size: 1.8rem;
    font-weight: 800;
    font-family: var(--font-display), sans-serif;
    color: var(--accent);
}

.read-lbl {
    font-size: 0.75rem;
    margin-top: 4px;
    color: var(--text-secondary);
}

.read-gauge-wrap {
    max-width: 420px;
    margin: 0 auto;
}

.read-gauge-label {
    display: flex;
    justify-content: space-between;
    font-size: 0.7rem;
    color: var(--text-muted);
    margin-bottom: 6px;
}

.read-gauge-track {
    height: 12px;
    border-radius: 99px;
    background: linear-gradient(90deg, #ef476f 0%, #ffd166 40%, #57cc99 100%);
    position: relative;
}

.read-gauge-thumb {
    position: absolute;
    top: 50%;
    transform: translate(-50%, -50%);
    width: 18px;
    height: 18px;
    border-radius: 50%;
    background: #fff;
    border: 3px solid var(--accent);
    box-shadow: 0 2px 8px rgba(0, 0, 0, 0.4);
    transition: left 0.6s cubic-bezier(0.22, 1, 0.36, 1);
}

/* ── Responsive ──────────────────────────────────────────────── */
@media (max-width: 680px) {
    #nlpModal {
        border-radius: 16px 16px 0 0;
        max-height: 95vh;
        align-self: flex-end;
        width: 100%;
    }

    .sent-row {
        grid-template-columns: 130px 70px 1fr;
    }

    .kp-row {
        grid-template-columns: 1fr 80px 50px;
    }

    .read-grid {
        grid-template-columns: repeat(2, 1fr);
    }
}

        `;
        document.head.appendChild(style);

        /* ---------- HTML ---------- */
        const overlay = document.createElement("div");
        overlay.id = "nlpOverlay";
        overlay.innerHTML = `
        <div id="nlpModal">

          <div id="nlpHeader">
            <div class="nlp-header-left">
              <i class="fa-solid fa-flask-vial"></i>
              <div>
                <h2>
                  NLP Analytics Suite
                  <span id="nlpStatBar"></span>
                </h2>
                <span class="hdr-subtitle">Performs advanced natural language tasks including Entity Extraction, Sentiment Analysis, and Summarization.</span>
              </div>
            </div>
            <div class="nlp-hdr-right">
              <button id="nlpRunBtn" onclick="NLPAnalytics.run()">
                <i class="fa-solid fa-rotate"></i> Analyse
              </button>
              <button id="nlpCloseBtn" onclick="NLPAnalytics.close()">
                <i class="fa-solid fa-xmark"></i>
              </button>
            </div>
          </div>

          <div id="nlpTabs">
            <button class="nlp-tab active" data-tab="sentiment"   onclick="NLPAnalytics.tab('sentiment')">
              <i class="fa-solid fa-face-smile"></i> Sentiment
            </button>
            <button class="nlp-tab" data-tab="wordcloud"   onclick="NLPAnalytics.tab('wordcloud')">
              <i class="fa-solid fa-cloud"></i> Word Cloud
            </button>
            <button class="nlp-tab" data-tab="topics"      onclick="NLPAnalytics.tab('topics')">
              <i class="fa-solid fa-layer-group"></i> Topics
            </button>
            <button class="nlp-tab" data-tab="keyphrases"  onclick="NLPAnalytics.tab('keyphrases')">
              <i class="fa-solid fa-key"></i> Keyphrases
            </button>
            <button class="nlp-tab" data-tab="entities"    onclick="NLPAnalytics.tab('entities')">
              <i class="fa-solid fa-tag"></i> Entities
            </button>
            <button class="nlp-tab" data-tab="readability" onclick="NLPAnalytics.tab('readability')">
              <i class="fa-solid fa-book-open"></i> Readability
            </button>
          </div>

          <div id="nlpBody">
            <div class="nlp-state" id="nlpState" style="display: flex; flex-direction: column; align-items: center; justify-content: center; text-align: center; padding: 0; gap: 8px; color: var(--text-secondary); width: 100%; height: 100%;">
              <i class="fa-solid fa-flask-vial" style="margin-bottom: 2px; font-size: 24px; color: var(--accent);"></i>
              <h3 style="margin: 0; font-family: var(--font-display); font-size: 1.0rem; color: var(--text-primary);">NLP Analytics Suite</h3>
              <p style="margin: 0 0 8px; font-size: 0.78rem; max-width: 480px; line-height: 1.3;">
                Analyze your documents to extract emotional tone, key terms, themes, entities, and readability complexity.
              </p>
              
              <div style="display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 8px; width: 100%; max-width: 740px; text-align: left; margin-bottom: 12px;">
                <div style="background: var(--bg-glass); border: 1px solid var(--border); padding: 8px 12px; border-radius: 10px;">
                  <strong style="color: var(--accent); display: block; font-size: 0.78rem; margin-bottom: 2px;"><i class="fa-solid fa-face-smile"></i> Sentiment Analysis</strong>
                  <span style="font-size: 0.7rem; color: var(--text-secondary); line-height: 1.3; display: block;">Measures emotional tone (positive/negative/neutral) of text.</span>
                </div>
                <div style="background: var(--bg-glass); border: 1px solid var(--border); padding: 8px 12px; border-radius: 10px;">
                  <strong style="color: var(--accent); display: block; font-size: 0.78rem; margin-bottom: 2px;"><i class="fa-solid fa-cloud"></i> Word Cloud</strong>
                  <span style="font-size: 0.7rem; color: var(--text-secondary); line-height: 1.3; display: block;">Highlights the most frequent words and key terms.</span>
                </div>
                <div style="background: var(--bg-glass); border: 1px solid var(--border); padding: 8px 12px; border-radius: 10px;">
                  <strong style="color: var(--accent); display: block; font-size: 0.78rem; margin-bottom: 2px;"><i class="fa-solid fa-layer-group"></i> Topic Modeling</strong>
                  <span style="font-size: 0.7rem; color: var(--text-secondary); line-height: 1.3; display: block;">Discovers the main themes and groups related concepts.</span>
                </div>
                <div style="background: var(--bg-glass); border: 1px solid var(--border); padding: 8px 12px; border-radius: 10px;">
                  <strong style="color: var(--accent); display: block; font-size: 0.78rem; margin-bottom: 2px;"><i class="fa-solid fa-key"></i> Keyphrases</strong>
                  <span style="font-size: 0.7rem; color: var(--text-secondary); line-height: 1.3; display: block;">Pulls out the most important keywords and search terms.</span>
                </div>
                <div style="background: var(--bg-glass); border: 1px solid var(--border); padding: 8px 12px; border-radius: 10px;">
                  <strong style="color: var(--accent); display: block; font-size: 0.78rem; margin-bottom: 2px;"><i class="fa-solid fa-tag"></i> Named Entities</strong>
                  <span style="font-size: 0.7rem; color: var(--text-secondary); line-height: 1.3; display: block;">Extracts names of people, companies, dates, and locations.</span>
                </div>
                <div style="background: var(--bg-glass); border: 1px solid var(--border); padding: 8px 12px; border-radius: 10px;">
                  <strong style="color: var(--accent); display: block; font-size: 0.78rem; margin-bottom: 2px;"><i class="fa-solid fa-book-open"></i> Readability</strong>
                  <span style="font-size: 0.7rem; color: var(--text-secondary); line-height: 1.3; display: block;">Calculates how easy or difficult text is to read.</span>
                </div>
              </div>
              
              <button onclick="NLPAnalytics.run()" style="background: var(--grad-button); color: var(--swal-btn-text); border: none; padding: 8px 20px; border-radius: 10px; font-family: var(--font-display); font-size: 0.82rem; font-weight: 700; cursor: pointer; transition: transform 0.2s, box-shadow 0.2s; box-shadow: 0 4px 12px var(--accent-glow); display: inline-flex; align-items: center; gap: 6px;">
                <i class="fa-solid fa-rotate"></i> Run Full Analysis
              </button>
            </div>

            <!-- Sentiment -->
            <div class="nlp-panel" id="panel-sentiment">
              <p style="font-size: 0.75rem; font-style: italic; color: var(--text-secondary); margin-bottom: 14px; margin-top: -4px;">
                Measures the emotional tone of the text (positive, negative, or neutral) and shows sentiment distribution across sentences.
              </p>
              <div class="sent-overall">
                <div class="sent-dial">
                  <canvas id="sentDialCanvas" width="260" height="260"></canvas>
                  <div class="sent-dial-label">
                    <span class="sent-dial-pct" id="sentPct">—</span>
                    <span class="sent-dial-word" id="sentWord">—</span>
                  </div>
                </div>
                <div class="sent-info">
                  <h3 id="sentOverallLabel">—</h3>
                  <p id="sentOverallDesc">Run analysis to see results.</p>
                </div>
              </div>
              <div class="sent-breakdown" id="sentBreakdown"></div>
            </div>

            <!-- Word Cloud -->
            <div class="nlp-panel" id="panel-wordcloud">
              <p style="font-size: 0.75rem; font-style: italic; color: var(--text-secondary); margin-bottom: 14px; margin-top: -4px;">
                Highlights the most frequently occurring keywords and terms; larger words indicate higher frequency.
              </p>
              <canvas id="wcCanvas"></canvas>
            </div>

            <!-- Topics -->
            <div class="nlp-panel" id="panel-topics">
              <p style="font-size: 0.75rem; font-style: italic; color: var(--text-secondary); margin-bottom: 14px; margin-top: -4px;">
                Groups related concepts together using unsupervised clustering to discover the main themes of the document.
              </p>
              <div class="topic-grid" id="topicGrid"></div>
            </div>

            <!-- Keyphrases -->
            <div class="nlp-panel" id="panel-keyphrases">
              <p style="font-size: 0.75rem; font-style: italic; color: var(--text-secondary); margin-bottom: 14px; margin-top: -4px;">
                Pulls out the most critical multi-word keyphrases and semantic search terms from the content.
              </p>
              <div class="kp-list" id="kpList"></div>
            </div>

            <!-- Entities -->
            <div class="nlp-panel" id="panel-entities">
              <p style="font-size: 0.75rem; font-style: italic; color: var(--text-secondary); margin-bottom: 14px; margin-top: -4px;">
                Extracts and classifies named entities such as people, organizations, locations, quantities, and dates.
              </p>
              <div id="nerContainer"></div>
            </div>

            <!-- Readability -->
            <div class="nlp-panel" id="panel-readability">
              <p style="font-size: 0.75rem; font-style: italic; color: var(--text-secondary); margin-bottom: 14px; margin-top: -4px;">
                Calculates document complexity and grade level using word-to-sentence ratios and readability formulas.
              </p>
              <div class="read-grid" id="readGrid"></div>
              <div class="read-gauge-wrap">
                <div class="read-gauge-label">
                  <span>Very Difficult (0)</span><span>Easy (100)</span>
                </div>
                <div class="read-gauge-track">
                  <div class="read-gauge-thumb" id="readThumb" style="left:50%"></div>
                </div>
              </div>
            </div>
          </div>

        </div>`;
        (document.getElementById("featureWorkspace") || document.body).appendChild(overlay);
        overlay.addEventListener("click", e => { if (e.target === overlay) NLPAnalytics.close(); });
    }

    // ── Helpers ───────────────────────────────────────────────────────────
    function _getFiles() {
        const f = [];
        document.querySelectorAll("#documentCheckboxList input:checked")
            .forEach(cb => f.push(cb.value));
        return f;
    }

    function _setState(html) {
        const s = document.getElementById("nlpState");
        s.innerHTML = html;
        s.style.display = "flex";
        document.querySelectorAll(".nlp-panel").forEach(p => p.classList.remove("active"));
    }

    function _clearState() {
        document.getElementById("nlpState").style.display = "none";
    }

    function _setStatBar(stats) {
        if (!stats) { document.getElementById("nlpStatBar").textContent = ""; return; }
        document.getElementById("nlpStatBar").textContent =
            `· ${stats.chunks} chunks · ${stats.sources} sources · ${stats.total_words.toLocaleString()} words`;
    }

    // ── Sentiment dial ────────────────────────────────────────────────────
    function _drawDial(score, color) {
        const canvas = document.getElementById("sentDialCanvas");
        const ctx    = canvas.getContext("2d");
        const cx = 130, cy = 130, r = 100;
        ctx.clearRect(0, 0, 260, 260);

        // Track
        ctx.beginPath();
        ctx.arc(cx, cy, r, Math.PI * 0.75, Math.PI * 2.25);
        ctx.strokeStyle = document.body.classList.contains("light-mode") ? "rgba(0,0,0,0.08)" : "rgba(255,255,255,0.07)";
        ctx.lineWidth = 14; ctx.lineCap = "round";
        ctx.stroke();

        // Fill
        const end = Math.PI * 0.75 + (Math.PI * 1.5 * score);
        ctx.beginPath();
        ctx.arc(cx, cy, r, Math.PI * 0.75, end);
        ctx.strokeStyle = color;
        ctx.lineWidth = 14; ctx.lineCap = "round";
        ctx.stroke();
    }

    // ── Word Cloud ────────────────────────────────────────────────────────
    function _drawWordCloud(words) {
        const canvas  = document.getElementById("wcCanvas");
        const W = canvas.offsetWidth || 700;
        const H = 420;
        canvas.width  = W;
        canvas.height = H;
        const ctx = canvas.getContext("2d");
        ctx.clearRect(0, 0, W, H);

        const placed = [];
        const maxW   = words[0]?.weight || 100;

        for (const item of words.slice(0, 80)) {
            const size  = Math.round(13 + (item.weight / maxW) * 42);
            const color = COLORS[Math.floor(Math.random() * COLORS.length)];
            ctx.font    = `${600} ${size}px Sora, sans-serif`;
            const tw    = ctx.measureText(item.word).width;

            let placed_ = false;
            for (let attempt = 0; attempt < 120; attempt++) {
                const x = Math.random() * (W - tw - 20) + 10;
                const y = Math.random() * (H - size - 10) + size;
                const rect = { x, y: y - size, w: tw, h: size + 4 };

                const overlap = placed.some(p =>
                    rect.x < p.x + p.w && rect.x + rect.w > p.x &&
                    rect.y < p.y + p.h && rect.y + rect.h > p.y
                );
                if (!overlap) {
                    ctx.fillStyle = color;
                    ctx.globalAlpha = 0.75 + (item.weight / maxW) * 0.25;
                    ctx.fillText(item.word, x, y);
                    ctx.globalAlpha = 1;
                    placed.push(rect);
                    placed_ = true;
                    break;
                }
            }
        }
    }

    // ── Sentiment panel ───────────────────────────────────────────────────
    function _renderSentiment(data) {
        const score  = data.score || 0.5;
        const label  = data.overall || "Neutral";
        const color  = score >= 0.65 ? "#57cc99" : score <= 0.35 ? "#ef476f" : "#ffd166";

        _drawDial(score, color);
        document.getElementById("sentPct").textContent  = Math.round(score * 100) + "%";
        document.getElementById("sentWord").textContent = label;
        document.getElementById("sentOverallLabel").textContent = `Overall: ${label}`;
        document.getElementById("sentOverallDesc").textContent =
            `Sentiment score ${Math.round(score * 100)}% across ${data.breakdown?.length || 0} sources.`;

        const bd = document.getElementById("sentBreakdown");
        bd.innerHTML = "";
        (data.breakdown || []).forEach(b => {
            const c = b.score >= 0.65 ? "#57cc99" : b.score <= 0.35 ? "#ef476f" : "#ffd166";
            bd.insertAdjacentHTML("beforeend", `
            <div class="sent-row">
              <span class="sent-row-name" title="${b.source}">${b.source}</span>
              <span class="sent-pill" style="background:${c}22;color:${c}">${b.label}</span>
              <div class="sent-bar-wrap">
                <div class="sent-bar" style="width:${Math.round(b.score*100)}%;background:${c}"></div>
              </div>
            </div>`);
        });
    }

    // ── Topic panel ───────────────────────────────────────────────────────
    function _renderTopics(topics) {
        const grid = document.getElementById("topicGrid");
        grid.innerHTML = "";
        topics.forEach((t, i) => {
            const color = COLORS[i % COLORS.length];
            const pct   = Math.round((t.weight || 0.2) * 100);
            const kws   = (t.keywords || []).map(k =>
                `<span class="topic-kw" style="color:${color};border-color:${color}40">${k}</span>`
            ).join("");
            grid.insertAdjacentHTML("beforeend", `
            <div class="topic-card" style="border-left-color:${color}">
              <div class="topic-card-top">
                <span class="topic-label">${t.label}</span>
                <span class="topic-weight" style="background:${color}20;color:${color}">${pct}%</span>
              </div>
              <div class="topic-bar-wrap">
                <div class="topic-bar" style="width:${pct}%;background:${color}"></div>
              </div>
              <div class="topic-kws">${kws}</div>
            </div>`);
        });
    }

    // ── Keyphrase panel ───────────────────────────────────────────────────
    function _renderKeyphrases(phrases) {
        const list = document.getElementById("kpList");
        list.innerHTML = "";
        phrases.forEach((p, i) => {
            const color = COLORS[i % COLORS.length];
            list.insertAdjacentHTML("beforeend", `
            <div class="kp-row">
              <span class="kp-phrase">${p.phrase}</span>
              <span class="kp-src" title="${p.source}">${p.source}</span>
              <span class="kp-score" style="color:${color}">${Math.round(p.score * 100)}%</span>
            </div>`);
        });
    }

    // ── NER panel ─────────────────────────────────────────────────────────
    function _renderEntities(entities) {
        const container = document.getElementById("nerContainer");
        container.innerHTML = "";

        const grouped = {};
        entities.forEach(e => {
            if (!grouped[e.type]) grouped[e.type] = [];
            grouped[e.type].push(e);
        });

        Object.entries(grouped).forEach(([type, items]) => {
            const color = ENTITY_COLORS[type] || "#a8a8a8";
            const tags  = items.map(e => `
                <span class="ner-tag" style="color:${color};border-color:${color}40;background:${color}12">
                  ${e.text}
                  <span class="ner-count" style="background:${color}30;color:${color}">${e.count}</span>
                </span>`).join("");
            container.insertAdjacentHTML("beforeend", `
            <div class="ner-section">
              <div class="ner-type-label" style="color:${color}">${type}</div>
              <div class="ner-tags">${tags}</div>
            </div>`);
        });
    }

    // ── Readability panel ─────────────────────────────────────────────────
    function _renderReadability(data) {
        const grid = document.getElementById("readGrid");
        grid.innerHTML = "";
        const cards = [
            { val: data.score,            lbl: "Flesch Score",        desc: "Measures reading ease from 0-100 (higher means easier)" },
            { val: data.grade,            lbl: "Reading Grade",       desc: "Estimated US school grade level required to understand" },
            { val: data.avg_sentence_len, lbl: "Avg Sentence Length", desc: "Average word count per sentence in text" },
            { val: data.total_words?.toLocaleString(), lbl: "Total Words", desc: "Total count of words processed in selection" },
            { val: data.total_sentences,  lbl: "Total Sentences",     desc: "Total count of sentences detected in text" },
        ];
        cards.forEach(c => {
            grid.insertAdjacentHTML("beforeend", `
            <div class="read-card">
              <div class="read-val">${c.val ?? "—"}</div>
              <div class="read-lbl">${c.lbl}</div>
              <div style="font-size: 0.68rem; font-style: italic; color: var(--text-secondary); margin-top: 4px; line-height: 1.2;">${c.desc}</div>
            </div>`);
        });
        const pct = Math.min(100, Math.max(0, data.score || 50));
        document.getElementById("readThumb").style.left = pct + "%";
    }

    // ── Render all ────────────────────────────────────────────────────────
    function _render(data) {
        _updateColorsForMode();
        _clearState();
        _setStatBar(data.stats);

        if (data.sentiment)   _renderSentiment(data.sentiment);
        if (data.wordcloud)   _drawWordCloud(data.wordcloud);
        if (data.topics)      _renderTopics(data.topics);
        if (data.keyphrases)  _renderKeyphrases(data.keyphrases);
        if (data.entities)    _renderEntities(data.entities);
        if (data.readability) _renderReadability(data.readability);

        // Show active tab panel
        document.querySelectorAll(".nlp-panel").forEach(p => p.classList.remove("active"));
        const active = document.getElementById(`panel-${_activeTab}`);
        if (active) active.classList.add("active");
    }

    // ── Public: tab switch ─────────────────────────────────────────────────
    function tab(name) {
        _activeTab = name;
        document.querySelectorAll(".nlp-tab").forEach(t => t.classList.remove("active"));
        document.querySelectorAll(`.nlp-tab[data-tab="${name}"]`).forEach(t => t.classList.add("active"));
        document.querySelectorAll(".nlp-panel").forEach(p => p.classList.remove("active"));
        const panel = document.getElementById(`panel-${name}`);
        if (panel && _data) {
            _updateColorsForMode();
            panel.classList.add("active");
            document.getElementById("nlpState").style.display = "none";
            // Redraw canvas-based panels on tab switch
            if (name === "wordcloud" && _data.wordcloud) _drawWordCloud(_data.wordcloud);
            if (name === "sentiment" && _data.sentiment) _renderSentiment(_data.sentiment);
        }
    }

    // ── Public: run analysis ───────────────────────────────────────────────
    async function run() {
        const btn = document.getElementById("nlpRunBtn");
        btn.disabled = true;
        btn.innerHTML = `<span class="nexora-spinner inline"></span> Analysing…`;
        _setState(`<span class="nexora-spinner large" style="margin-bottom: 8px;"></span><span>Running NLP analysis on your documents…</span>`);
        _setStatBar(null);
        _data = null;

        try {
            const sid = (typeof session_id !== "undefined" && session_id) ? session_id : "nlp-session";
            const res = await fetch("/nlp_analytics", {
                method:  "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ session_id: sid, files: _getFiles(), analyses: ["all"] }),
            });

            if (!res.ok) {
                const e = await res.json().catch(() => ({}));
                _setState(`<i class="fa-solid fa-circle-exclamation"></i><span>Error: ${e.message || res.statusText}</span>`);
                return;
            }

            _data = await res.json();
            _render(_data);

        } catch (e) {
            console.error("[NLP]", e);
            _setState(`<i class="fa-solid fa-triangle-exclamation"></i><span>Network error. Please try again.</span>`);
        } finally {
            btn.disabled = false;
            btn.innerHTML = `<i class="fa-solid fa-rotate"></i> Analyse`;
        }
    }

    // ── Public: open / close ──────────────────────────────────────────────
    function open() {
        _inject();
        document.getElementById("nlpOverlay").classList.add("nlp-active");
    }

    function close() {
        const o = document.getElementById("nlpOverlay");
        if (o) o.classList.remove("nlp-active");
        if (typeof backToChatbot === "function") backToChatbot();
    }

    return { open, close, run, tab };

})();