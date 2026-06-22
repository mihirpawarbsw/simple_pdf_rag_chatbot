/**
 * full_report.js  —  Nexora AI  |  Full Report Generator (frontend)
 *
 * Include in index.html:
 *   <script src="{{ url_for('static', filename='js/full_report.js') }}" defer></script>
 *
 * Exposes:
 *   openReportModal()    — open the report configuration modal
 *   closeReportModal()   — close the modal
 *   generateFullReport() — trigger report generation & download
 */

(function () {
  "use strict";

  /* ──────────────────────────────────────────────────────────────────────────
     MODAL HTML  — injected once into DOM on first call
  ────────────────────────────────────────────────────────────────────────── */
  const MODAL_ID = "nexoraReportModal";

  function injectModal() {
    if (document.getElementById(MODAL_ID)) return;

    const target = document.getElementById("featureWorkspace") || document.body;
    target.insertAdjacentHTML(
      "beforeend",
      `
<!-- ======= FULL REPORT MODAL ======= -->
<div id="${MODAL_ID}" class="report-modal-overlay" style="display:none" onclick="if(event.target===this)closeReportModal()">
  <div class="report-modal-card">

    <!-- Header -->
    <div class="report-modal-header">
      <span class="report-modal-icon"><i class="fa-solid fa-file-invoice"></i></span>
      <div>
        <h2 class="report-modal-title">Generate Full Report</h2>
        <p class="report-modal-sub" style="font-style: italic; font-size: 0.72rem; color: var(--text-secondary);">Generates a comprehensive, formatted executive document summarizing the key findings, metrics, and details of your files.</p>
      </div>
      <button class="report-close-btn" onclick="closeReportModal()" title="Close">
        <i class="fa-solid fa-xmark"></i>
      </button>
    </div>

    <!-- Format selector -->
    <div class="report-section-label">Output Format</div>
    <div class="report-format-row" id="reportFormatRow">
      <button class="report-format-btn active" data-fmt="pdf" onclick="selectReportFormat(this,'pdf')">
        <i class="fa-solid fa-file-pdf"></i> PDF
      </button>
      <button class="report-format-btn" data-fmt="docx" onclick="selectReportFormat(this,'docx')">
        <i class="fa-solid fa-file-word"></i> Word (.docx)
      </button>
    </div>

    <!-- Document list -->
    <div class="report-section-label" style="margin-top:18px">
      Documents to Include
      <span class="report-doc-count" id="reportDocCount"></span>
    </div>
    <div class="report-doc-list" id="reportDocList">
      <div class="report-doc-loading"><span class="nexora-spinner inline"></span> Loading documents…</div>
    </div>

    <!-- Select all / none -->
    <div class="report-sel-row">
      <button class="report-sel-link" onclick="reportSelectAll(true)">Select All</button>
      <span style="color:var(--border);margin:0 6px">|</span>
      <button class="report-sel-link" onclick="reportSelectAll(false)">Deselect All</button>
    </div>

    <!-- Progress / status -->
    <div class="report-progress-area" id="reportProgressArea" style="display:none">
      <div class="report-progress-bar-wrap">
        <div class="report-progress-bar" id="reportProgressBar"></div>
      </div>
      <div class="report-progress-label" id="reportProgressLabel">Preparing…</div>
    </div>

    <!-- Action buttons -->
    <div class="report-modal-footer">
      <button class="report-cancel-btn" onclick="closeReportModal()">Cancel</button>
      <button class="report-generate-btn" id="reportGenerateBtn" onclick="generateFullReport()">
        <i class="fa-solid fa-wand-magic-sparkles"></i> Generate Report
      </button>
    </div>

  </div>
</div>
      `
    );
  }

  /* ──────────────────────────────────────────────────────────────────────────
     STATE
  ────────────────────────────────────────────────────────────────────────── */
  let _selectedFormat = "pdf";
  let _generating     = false;
  const _progressSteps = [
    { pct: 10, label: "Fetching document chunks…"        },
    { pct: 25, label: "Writing Executive Summary…"        },
    { pct: 45, label: "Identifying Key Findings…"         },
    { pct: 65, label: "Running Detailed Analysis…"        },
    { pct: 82, label: "Drafting Conclusion & Recommendations…" },
    { pct: 92, label: "Rendering report file…"            },
    { pct: 100, label: "Done! Downloading…"              },
  ];

  /* ──────────────────────────────────────────────────────────────────────────
     PUBLIC — open modal
  ────────────────────────────────────────────────────────────────────────── */
  window.openReportModal = function () {
    injectModal();
    document.getElementById(MODAL_ID).style.display = "flex";
    _resetModal();
    _loadDocuments();
  };

  window.closeReportModal = function () {
    const el = document.getElementById(MODAL_ID);
    if (el) el.style.display = "none";
    _generating = false;
    if (typeof backToChatbot === "function") backToChatbot();
  };

  window.selectReportFormat = function (btn, fmt) {
    _selectedFormat = fmt;
    document.querySelectorAll(".report-format-btn").forEach(b => b.classList.remove("active"));
    btn.classList.add("active");
  };

  window.reportSelectAll = function (checked) {
    document.querySelectorAll(".report-doc-check").forEach(cb => (cb.checked = checked));
    _updateDocCount();
  };

  /* ──────────────────────────────────────────────────────────────────────────
     Load documents from server
  ────────────────────────────────────────────────────────────────────────── */
  function _loadDocuments() {
    fetch("/get_user_documents")
      .then(r => r.json())
      .then(docs => {
        const list = document.getElementById("reportDocList");
        if (!docs || docs.length === 0) {
          list.innerHTML = `<div class="report-doc-empty">No documents indexed yet. Please upload files first.</div>`;
          return;
        }
        list.innerHTML = docs
          .map(
            (d, i) => `
          <label class="report-doc-item" for="rdoc_${i}">
            <input type="checkbox" class="report-doc-check" id="rdoc_${i}" value="${_escHtml(d)}" checked
                   onchange="_updateDocCount()">
            <i class="fa-solid fa-file-lines report-doc-icon"></i>
            <span class="report-doc-name">${_escHtml(d)}</span>
          </label>`
          )
          .join("");
        _updateDocCount();
      })
      .catch(() => {
        document.getElementById("reportDocList").innerHTML =
          `<div class="report-doc-empty" style="color:#f87171">Could not load documents.</div>`;
      });
  }

  window._updateDocCount = function _updateDocCount() {
    const total   = document.querySelectorAll(".report-doc-check").length;
    const checked = document.querySelectorAll(".report-doc-check:checked").length;
    const el = document.getElementById("reportDocCount");
    if (el) el.textContent = `${checked} / ${total} selected`;
  }

  /* ──────────────────────────────────────────────────────────────────────────
     PUBLIC — generate report
  ────────────────────────────────────────────────────────────────────────── */
  window.generateFullReport = async function () {
    if (_generating) return;

    const checked = [...document.querySelectorAll(".report-doc-check:checked")].map(cb => cb.value);
    if (checked.length === 0) {
      _showToast("Please select at least one document.", "warning");
      return;
    }

    _generating = true;
    _showProgress(true);
    document.getElementById("reportGenerateBtn").disabled = true;

    // Animate progress bar through fake steps while we wait
    let stepIdx = 0;
    const stepTimer = setInterval(() => {
      if (stepIdx < _progressSteps.length - 1) {
        _setProgress(_progressSteps[stepIdx].pct, _progressSteps[stepIdx].label);
        stepIdx++;
      }
    }, 2500);

    try {
      const sessionId = window.currentSessionId || "";  // set by your existing chat JS

      const resp = await fetch("/generate_report", {
        method:  "POST",
        headers: { "Content-Type": "application/json" },
        body:    JSON.stringify({
          session_id:    sessionId,
          selected_docs: checked,
          format:        _selectedFormat,
        }),
      });

      clearInterval(stepTimer);

      if (!resp.ok) {
        const errData = await resp.json().catch(() => ({}));
        throw new Error(errData.message || `Server error ${resp.status}`);
      }

      _setProgress(100, "Done! Downloading…");

      // Trigger file download
      const blob        = await resp.blob();
      const disposition = resp.headers.get("Content-Disposition") || "";
      const nameMatch   = disposition.match(/filename="?([^"]+)"?/);
      const fileName    = nameMatch ? nameMatch[1] : `nexora_report.${_selectedFormat}`;

      const url  = URL.createObjectURL(blob);
      const link = document.createElement("a");
      link.href  = url;
      link.download = fileName;
      link.click();
      URL.revokeObjectURL(url);

      setTimeout(() => {
        closeReportModal();
        _showToast("Report downloaded successfully!", "success");
      }, 800);

    } catch (err) {
      clearInterval(stepTimer);
      _showProgress(false);
      document.getElementById("reportGenerateBtn").disabled = false;
      _generating = false;
      _showToast("Report generation failed: " + err.message, "error");
    }
  };

  /* ──────────────────────────────────────────────────────────────────────────
     Helpers
  ────────────────────────────────────────────────────────────────────────── */
  function _resetModal() {
    _generating     = false;
    _selectedFormat = "pdf";
    document.querySelectorAll(".report-format-btn").forEach((b, i) => {
      b.classList.toggle("active", i === 0);
    });
    _showProgress(false);
    const btn = document.getElementById("reportGenerateBtn");
    if (btn) btn.disabled = false;
  }

  function _showProgress(show) {
    const el = document.getElementById("reportProgressArea");
    if (el) el.style.display = show ? "block" : "none";
    if (show) _setProgress(5, "Starting…");
  }

  function _setProgress(pct, label) {
    const bar   = document.getElementById("reportProgressBar");
    const lbl   = document.getElementById("reportProgressLabel");
    if (bar) bar.style.width = pct + "%";
    if (lbl) lbl.textContent = label;
  }

  function _escHtml(str) {
    return String(str)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  function _showToast(msg, type = "info") {
    if (typeof Swal !== "undefined") {
      Swal.fire({
        toast:             true,
        position:          "top-end",
        icon:              type === "success" ? "success" : type === "warning" ? "warning" : "error",
        title:             msg,
        showConfirmButton: false,
        timer:             3500,
        timerProgressBar:  true,
      });
    } else {
      alert(msg);
    }
  }
})();
