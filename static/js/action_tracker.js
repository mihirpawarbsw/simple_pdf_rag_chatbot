/* =============================================================
   action_tracker.js  —  Smart Action Items & Decision Tracker
   Save to: static/js/action_tracker.js
   ============================================================= */

const ActionTracker = (() => {

    /* ── State ──────────────────────────────────────────────── */
    let _items        = [];
    let _view         = 'kanban';   // 'kanban' | 'timeline'
    let _filterType   = '';         // '' | 'action' | 'deadline' | 'decision'
    let _filterStatus = '';         // '' | 'todo' | 'inprogress' | 'done'
    let _isExtracting = false;

    /* ── DOM refs (resolved lazily) ─────────────────────────── */
    const $ = id => document.getElementById(id);

    /* ── Public: open modal ─────────────────────────────────── */
    function open() {
        $('actionTrackerOverlay').classList.add('open');
        _load();
    }

    /* ── Public: close modal ────────────────────────────────── */
    function close() {
        $('actionTrackerOverlay').classList.remove('open');
        if (typeof backToChatbot === "function") backToChatbot();
    }

    /* ── Load items from backend ────────────────────────────── */
    async function _load() {
        _showLoading();
        try {
            let url = `/get_action_items?`;
            if (_filterType)   url += `item_type=${_filterType}&`;
            if (_filterStatus) url += `status=${_filterStatus}&`;

            const res  = await fetch(url);
            const data = await res.json();
            if (data.status !== 'success') throw new Error(data.message);

            _items = data.items || [];
            _updateStatBar(data);
            _updateAlertBanner(data.overdue_count || 0);
            _render();
        } catch (e) {
            _showEmpty(`Failed to load items: ${e.message}`);
        }
    }

    /* ── Extract action items ───────────────────────────────── */
    async function extract(forceRefresh = false) {
        if (_isExtracting) return;
        _isExtracting = true;

        const btn = $('atExtractBtn');
        if (btn) { btn.disabled = true; btn.innerHTML = '<i class="fa-solid fa-spinner at-spin"></i> Scanning…'; }

        // Collect selected docs from the existing document filter checkboxes
        const checked = document.querySelectorAll('#documentCheckboxList input[type=checkbox]:checked');
        const selectedDocs = Array.from(checked).map(cb => cb.value);

        try {
            const res  = await fetch('/extract_action_items', {
                method:  'POST',
                headers: { 'Content-Type': 'application/json' },
                body:    JSON.stringify({ selected_docs: selectedDocs, force_refresh: forceRefresh }),
            });
            const data = await res.json();

            if (data.status === 'success') {
                Swal.fire({
                    icon:  'success',
                    title: 'Extraction Complete',
                    text:  data.message,
                    timer: 2800,
                    showConfirmButton: false,
                    background:   getComputedStyle(document.body).getPropertyValue('--card-bg') || '#1a1a2e',
                    color:        '#eee',
                });
                _load();
            } else {
                Swal.fire({ icon: 'error', title: 'Error', text: data.message });
            }
        } catch (e) {
            Swal.fire({ icon: 'error', title: 'Network Error', text: e.message });
        } finally {
            _isExtracting = false;
            if (btn) { btn.disabled = false; btn.innerHTML = '<i class="fa-solid fa-bolt"></i> Extract Items'; }
        }
    }

    /* ── Clear all items ────────────────────────────────────── */
    async function clearAll() {
        const result = await Swal.fire({
            title: 'Clear all items?',
            text:  'This removes all extracted action items, deadlines and decisions for your account.',
            icon:  'warning',
            showCancelButton:  true,
            confirmButtonText: 'Yes, clear',
            confirmButtonColor:'#ff6b6b',
            background: getComputedStyle(document.body).getPropertyValue('--card-bg') || '#1a1a2e',
            color: '#eee',
        });
        if (!result.isConfirmed) return;

        await fetch('/clear_action_items', { method: 'POST' });
        _items = [];
        _updateAlertBanner(0);
        _render();
    }

    /* ── Update item status ─────────────────────────────────── */
    async function updateStatus(itemId, newStatus) {
        await fetch('/update_item_status', {
            method:  'POST',
            headers: { 'Content-Type': 'application/json' },
            body:    JSON.stringify({ item_id: itemId, status: newStatus }),
        });
        _load();
    }

    /* ── Delete one item ────────────────────────────────────── */
    async function deleteItem(itemId) {
        await fetch('/delete_action_item', {
            method:  'POST',
            headers: { 'Content-Type': 'application/json' },
            body:    JSON.stringify({ item_id: itemId }),
        });
        _load();
    }

    /* ── Open citation inspector (reuse existing function from index.html) ─ */
    function viewCitation(sourceDoc, sourcePage) {
        if (typeof openCitationInspector === 'function') {
            openCitationInspector(sourceDoc, sourcePage);
        } else {
            Swal.fire({ title: 'Source', text: `${sourceDoc} — page ${sourcePage}`, icon: 'info' });
        }
    }

    /* ── View toggle ────────────────────────────────────────── */
    function setView(v) {
        _view = v;
        document.querySelectorAll('.at-view-btn').forEach(b => {
            b.classList.toggle('active', b.dataset.view === v);
        });
        _render();
    }

    /* ── Filter helpers ─────────────────────────────────────── */
    function setFilterType(val)   { _filterType   = val; _load(); }
    function setFilterStatus(val) { _filterStatus = val; _load(); }

    /* ═══════════════════════════════════════════════
       RENDERING
    ═══════════════════════════════════════════════ */

    function _showLoading() {
        $('atBody').innerHTML = `
            <div class="at-loading">
                <i class="fa-solid fa-spinner at-spin"></i>
                <span>Loading items…</span>
            </div>`;
    }

    function _showEmpty(msg = 'No items found. Click <strong>Extract Items</strong> to scan your documents.') {
        $('atBody').innerHTML = `
            <div class="at-empty">
                <i class="fa-solid fa-inbox"></i>
                <span>${msg}</span>
            </div>`;
    }

    function _updateStatBar(data) {
        const items      = data.items || [];
        const overdue    = items.filter(i => i.urgency === 'overdue' && i.status !== 'done').length;
        const soon       = items.filter(i => i.urgency === 'soon'    && i.status !== 'done').length;
        const actions    = items.filter(i => i.item_type === 'action').length;
        const decisions  = items.filter(i => i.item_type === 'decision').length;
        const deadlines  = items.filter(i => i.item_type === 'deadline').length;

        $('atStatBar').innerHTML = `
            ${overdue   ? `<div class="at-stat-chip overdue"><i class="fa-solid fa-circle-exclamation"></i> ${overdue} overdue</div>` : ''}
            ${soon      ? `<div class="at-stat-chip soon"><i class="fa-solid fa-clock"></i> ${soon} due soon</div>` : ''}
            <div class="at-stat-chip action"><i class="fa-solid fa-list-check"></i> ${actions} actions</div>
            <div class="at-stat-chip decision"><i class="fa-solid fa-gavel"></i> ${decisions} decisions</div>
            <div class="at-stat-chip deadline"><i class="fa-solid fa-calendar-day"></i> ${deadlines} deadlines</div>
            <div class="at-stat-chip"><i class="fa-solid fa-layer-group"></i> ${items.length} total</div>`;
    }

    function _updateAlertBanner(overdueCount) {
        const banner = $('actionAlertBanner');
        if (!banner) return;
        if (overdueCount > 0) {
            banner.classList.remove('hidden');
            banner.innerHTML = `<i class="fa-solid fa-triangle-exclamation"></i> ${overdueCount} overdue item${overdueCount > 1 ? 's' : ''}`;
        } else {
            banner.classList.add('hidden');
        }
    }

    /* ── Render dispatcher ── */
    function _render() {
        if (!_items.length) { _showEmpty(); return; }
        if (_view === 'kanban')   _renderKanban();
        else                      _renderTimeline();
    }

    /* ── Badge helper ── */
    function _typeBadge(type) {
        const map = {
            action:   { icon: 'fa-list-check',   cls: 'badge-action',   label: 'Action'   },
            deadline: { icon: 'fa-calendar-day',  cls: 'badge-deadline', label: 'Deadline' },
            decision: { icon: 'fa-gavel',         cls: 'badge-decision', label: 'Decision' },
        };
        const t = map[type] || map.action;
        return `<span class="at-card-type-badge ${t.cls}"><i class="fa-solid ${t.icon}"></i>${t.label}</span>`;
    }

    /* ── Due date label ── */
    function _dueDateLabel(item) {
        if (!item.due_date) return '';
        const cls = item.urgency === 'overdue' ? 'due-overdue' : item.urgency === 'soon' ? 'due-soon' : '';
        return `<span class="${cls}"><i class="fa-solid fa-calendar-check"></i>${item.due_date}${item.urgency === 'overdue' ? ' ⚠' : ''}</span>`;
    }

    /* ── Card action buttons ── */
    function _cardButtons(item) {
        const nextStatus = { todo: 'inprogress', inprogress: 'done', done: 'todo' };
        const nextLabel  = { todo: 'Start',       inprogress: 'Done',  done: 'Reopen' };
        const nextIcon   = { todo: 'fa-play',      inprogress: 'fa-check', done: 'fa-rotate-left' };
        const ns = nextStatus[item.status];
        return `
            <button class="at-move-btn"   onclick="ActionTracker.updateStatus(${item.id},'${ns}')">
                <i class="fa-solid ${nextIcon[item.status]}"></i>${nextLabel[item.status]}
            </button>
            <button class="at-cite-btn"   onclick="ActionTracker.viewCitation('${_esc(item.source_doc)}',${item.source_page})">
                <i class="fa-solid fa-link"></i>Source
            </button>
            <button class="at-delete-btn" onclick="ActionTracker.deleteItem(${item.id})">
                <i class="fa-solid fa-trash"></i>
            </button>`;
    }

    function _esc(s) { return (s || '').replace(/'/g, "\\'"); }

    /* ══════════════════════════════════════
       KANBAN
    ══════════════════════════════════════ */
    function _renderKanban() {
        const cols = {
            todo:       { items: [], label: 'To Do',       cls: 'col-todo'   },
            inprogress: { items: [], label: 'In Progress', cls: 'col-inprog' },
            done:       { items: [], label: 'Done',        cls: 'col-done'   },
        };
        _items.forEach(i => {
            (cols[i.status] || cols.todo).items.push(i);
        });

        let html = '<div class="at-kanban">';
        for (const [key, col] of Object.entries(cols)) {
            const total = col.items.length;
            const donePct = key === 'done' ? 100
                : key === 'inprogress' ? 50
                : 0;

            const cards = col.items.map(item => `
                <div class="at-card ${item.urgency}" data-id="${item.id}">
                    ${_typeBadge(item.item_type)}
                    <div class="at-card-text">${_escHtml(item.text)}</div>
                    <div class="at-card-meta">
                        ${item.owner    ? `<span><i class="fa-solid fa-user"></i>${_escHtml(item.owner)}</span>` : ''}
                        ${_dueDateLabel(item)}
                        <span><i class="fa-solid fa-file-lines"></i>${_escHtml(item.source_doc || '')} p.${item.source_page + 1}</span>
                    </div>
                    <div class="at-card-actions">${_cardButtons(item)}</div>
                </div>`).join('');

            html += `
                <div class="at-column ${col.cls}">
                    <div class="at-column-header">
                        <span>${col.label}</span>
                        <span class="col-count">${total}</span>
                    </div>
                    <div class="at-cards">${cards || '<div style="text-align:center;padding:20px;color:var(--text-secondary,#888);font-size:12px">Empty</div>'}</div>
                    ${key === 'done' ? `<div class="at-col-progress"><div class="at-col-progress-bar" style="width:${total>0?100:0}%"></div></div>` : ''}
                </div>`;
        }
        html += '</div>';
        $('atBody').innerHTML = html;
    }

    /* ══════════════════════════════════════
       TIMELINE
    ══════════════════════════════════════ */
    function _renderTimeline() {
        const groups = {
            overdue:  { label: 'Overdue',         cls: 'group-overdue',  icon: 'fa-circle-exclamation', items: [] },
            soon:     { label: 'Due Within 7 Days',cls: 'group-soon',    icon: 'fa-clock',              items: [] },
            upcoming: { label: 'Upcoming',         cls: 'group-upcoming', icon: 'fa-calendar',           items: [] },
            none:     { label: 'No Date',          cls: 'group-none',     icon: 'fa-minus',              items: [] },
        };
        _items.forEach(i => (groups[i.urgency] || groups.none).items.push(i));

        let html = '<div class="at-timeline">';
        for (const [, g] of Object.entries(groups)) {
            if (!g.items.length) continue;
            html += `
                <div class="at-timeline-group">
                    <div class="at-timeline-label ${g.cls}">
                        <i class="fa-solid ${g.icon}"></i>${g.label} (${g.items.length})
                    </div>
                    <div class="at-timeline-items">
                        ${g.items.map(item => `
                            <div class="at-timeline-item ${item.urgency} ${item.status === 'done' ? 'done' : ''}">
                                <div class="at-tl-content">
                                    ${_typeBadge(item.item_type)}
                                    <div class="at-tl-text">${_escHtml(item.text)}</div>
                                    <div class="at-tl-meta">
                                        ${item.owner   ? `<span><i class="fa-solid fa-user"></i>${_escHtml(item.owner)}</span>` : ''}
                                        ${_dueDateLabel(item)}
                                        <span><i class="fa-solid fa-file-lines"></i>${_escHtml(item.source_doc || '')} p.${item.source_page + 1}</span>
                                        <span><i class="fa-solid fa-tag"></i>${item.status}</span>
                                    </div>
                                </div>
                                <div class="at-tl-actions">${_cardButtons(item)}</div>
                            </div>`).join('')}
                    </div>
                </div>`;
        }
        html += '</div>';
        $('atBody').innerHTML = html;
    }

    /* ── Escape HTML ── */
    function _escHtml(s) {
        return (s || '').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
    }

    /* ── Init: check overdue count on page load ── */
    async function _initBanner() {
        try {
            const res  = await fetch('/get_action_items');
            const data = await res.json();
            if (data.status === 'success') _updateAlertBanner(data.overdue_count || 0);
        } catch (_) {}
    }

    // Run banner check after page loads
    window.addEventListener('DOMContentLoaded', _initBanner);

    /* ── Public API ── */
    return { open, close, extract, clearAll, updateStatus, deleteItem, viewCitation, setView, setFilterType, setFilterStatus };

})();
