// Utility for formatting currency
const formatter = new Intl.NumberFormat('es-ES', {
    style: 'currency',
    currency: 'EUR',
});

function escapeHtml(str) {
    if (str === null || str === undefined) return '';
    return String(str).replace(/&/g, '&amp;').replace(/</g, '&lt;')
        .replace(/>/g, '&gt;').replace(/"/g, '&quot;').replace(/'/g, '&#039;');
}

/**
 * liveSearch(inputId, tbodyId)
 * Attaches a real-time text filter to any in-memory table.
 * Hides rows whose visible text doesn't match the query.
 * Shows a "no results" row when nothing matches.
 * Safe to call multiple times — deduplicates listeners via data attribute.
 */
function liveSearch(inputId, tbodyId) {
    const inp = document.getElementById(inputId);
    if (!inp || inp.dataset.liveSearchBound === tbodyId) return;
    inp.dataset.liveSearchBound = tbodyId;

    inp.addEventListener('input', function () {
        const q = this.value.trim().toLowerCase();
        const tbody = document.getElementById(tbodyId);
        if (!tbody) return;
        let visible = 0;
        Array.from(tbody.rows).forEach(tr => {
            if (tr.dataset.searchSkip) return;          // skip detail/accordion rows
            const match = !q || tr.textContent.toLowerCase().includes(q);
            tr.style.display = match ? '' : 'none';
            if (match) visible++;
        });
        // Show/hide the "no results" sentinel row
        let sentinel = tbody.querySelector('tr[data-search-sentinel]');
        if (!visible && q) {
            if (!sentinel) {
                sentinel = document.createElement('tr');
                sentinel.dataset.searchSentinel = '1';
                const td = document.createElement('td');
                td.colSpan = 20;
                td.style.cssText = 'text-align:center;padding:1.5rem;color:var(--text-gray);font-size:.88rem';
                td.textContent = 'Sin resultados para "' + q + '"';
                sentinel.appendChild(td);
                tbody.appendChild(sentinel);
            }
            sentinel.style.display = '';
        } else if (sentinel) {
            sentinel.style.display = 'none';
        }
    });
}

// Close modals on click outside
window.addEventListener('click', (e) => {
    if (e.target.classList.contains('modal-overlay')) {
        closeModal();
        closePdfModal();
    }
});



function getStatusBadge(entity, status) {
    status = parseInt(status);
    let label = 'Unknown';
    let className = 'badge-draft';

    if (entity === 'invoices' || entity === 'purchases') {
        const maps = {
            0: { label: 'BORRADOR',  class: 'badge-draft' },
            1: { label: 'PENDIENTE', class: 'badge-pending' },  // approved, not yet paid (may be overdue — aging widget colors it)
            2: { label: 'PARCIAL',   class: 'badge-partial' },
            3: { label: 'COBRADA',   class: 'badge-paid' },
            4: { label: 'VENCIDA',   class: 'badge-overdue' },  // Holded rarely sends this
            5: { label: 'ANULADA',   class: 'badge-canceled' }
        };
        const m = maps[status] || { label: `STATUS ${status}`, class: 'badge-draft' };
        label = m.label;
        className = m.class;
    } else if (entity === 'estimates') {
        const maps = {
            0: { label: 'BORRADOR', class: 'badge-draft' },
            1: { label: 'PENDIENTE', class: 'badge-pending' },
            2: { label: 'ACEPTADO', class: 'badge-accepted' },
            3: { label: 'RECHAZADO', class: 'badge-overdue' },
            4: { label: 'FACTURADO', class: 'badge-converted' }
        };
        const m = maps[status] || { label: `STATUS ${status}`, class: 'badge-draft' };
        label = m.label;
        className = m.class;
    }

    return `<span class="badge ${className}">${label}</span>`;
}

async function fetchStats(start = null, end = null) {
    try {
        let url = '/api/summary';
        if (start && end) {
            url = `/api/stats/range?start=${start}&end=${end}`;
        }

        console.log(`Fetching stats from: ${url}`);
        const response = await fetch(url);
        const data = await response.json();
        console.log('Stats data received:', data);

        const income = data.totals ? data.totals.income : (data.income || 0);
        const expenses = data.totals ? data.totals.expenses : (data.expenses || 0);
        const balance = data.totals ? data.totals.balance : (income - expenses);

        console.log(`Updating UI with: Income=${income}, Expenses=${expenses}, Balance=${balance}`);
        animateValue('totalIncome', income);
        animateValue('totalExpenses', expenses);
        animateValue('netBalance', balance);
    } catch (error) {
        console.error('Error fetching stats:', error);
    }
}

function animateValue(id, value) {
    const obj = document.getElementById(id);
    if (obj) {
        console.log(`Setting ${id} to ${value}`);
        obj.textContent = formatter.format(value);
    } else {
        console.error(`Element with id "${id}" not found.`);
    }
}

let performanceChartInstance = null; // Renamed to avoid confusion with the ID

async function renderCharts(start = null, end = null) {
    try {
        console.log(`Rendering charts with range: ${start} to ${end}`);
        let url = '/api/stats/monthly';
        if (start && end) {
            url = `/api/stats/monthly?start=${start}&end=${end}`;
        }

        const response = await fetch(url);
        const data = await response.json();

        const canvas = document.getElementById('performanceChart');
        if (!canvas) return;
        const ctx = canvas.getContext('2d');

        // Destroy existing chart if it exists
        if (performanceChartInstance) {
            console.log('Destroying existing chart instance');
            performanceChartInstance.destroy();
        }

        // Gradient for Income
        const incomeGrad = ctx.createLinearGradient(0, 0, 0, 400);
        incomeGrad.addColorStop(0, 'rgba(16, 185, 129, 0.3)');
        incomeGrad.addColorStop(1, 'rgba(16, 185, 129, 0)');

        // Gradient for Expenses
        const expenseGrad = ctx.createLinearGradient(0, 0, 0, 400);
        expenseGrad.addColorStop(0, 'rgba(244, 63, 94, 0.3)');
        expenseGrad.addColorStop(1, 'rgba(244, 63, 94, 0)');

        const months = [...new Set([...data.income.map(d => d.month), ...data.expenses.map(d => d.month)])].sort();
        const incomeData = months.map(m => (data.income.find(d => d.month === m) || { total: 0 }).total);
        const expenseData = months.map(m => (data.expenses.find(d => d.month === m) || { total: 0 }).total);

        performanceChartInstance = new Chart(ctx, {
            type: 'line',
            data: {
                labels: months,
                datasets: [
                    {
                        label: 'Income',
                        data: incomeData,
                        borderColor: '#10b981',
                        backgroundColor: incomeGrad,
                        fill: true,
                        tension: 0.4,
                        borderWidth: 3,
                        pointRadius: 4,
                        pointBackgroundColor: '#10b981'
                    },
                    {
                        label: 'Expenses',
                        data: expenseData,
                        borderColor: '#f43f5e',
                        backgroundColor: expenseGrad,
                        fill: true,
                        tension: 0.4,
                        borderWidth: 3,
                        pointRadius: 4,
                        pointBackgroundColor: '#f43f5e'
                    }
                ]
            },
            options: {
                responsive: true,
                maintainAspectRatio: false,
                interaction: { intersect: false, mode: 'index' },
                plugins: {
                    legend: { position: 'top', labels: { color: '#94a3b8', font: { weight: '600' } } },
                    tooltip: { backgroundColor: '#1e293b', titleColor: '#f8fafc', bodyColor: '#94a3b8', padding: 12, borderRadius: 12 }
                },
                scales: {
                    y: { grid: { color: 'rgba(255, 255, 255, 0.03)' }, ticks: { color: '#94a3b8' } },
                    x: { grid: { display: false }, ticks: { color: '#94a3b8' } }
                }
            }
        });

    } catch (error) {
        console.error('Error rendering charts:', error);
    }
}

async function fetchRecentActivity(start = null, end = null) {
    try {
        let url = '/api/recent';
        if (start && end) {
            url = `/api/recent?start=${start}&end=${end}`;
        }
        const response = await fetch(url);
        const data = await response.json();
        const body = document.getElementById('recentBody');
        body.innerHTML = '';

        data.forEach(item => {
            const date = new Date(item.date * 1000).toLocaleDateString();
            const typeBadge = item.type === 'income' ? 'badge-income' : 'badge-expense';
            const row = `
                <tr>
                    <td><span class="badge ${typeBadge}">${escapeHtml(item.type).toUpperCase()}</span></td>
                    <td style="font-weight: 600">${escapeHtml(item.contact_name)}</td>
                    <td style="color: var(--text-gray)">${escapeHtml(date)}</td>
                    <td style="font-weight: 800">${formatter.format(item.amount)}</td>
                    <td>${getStatusBadge(item.type === 'income' ? 'invoices' : 'purchases', item.status)}</td>
                </tr>
            `;
            body.innerHTML += row;
        });
        liveSearch('recentSearch', 'recentBody');
    } catch (error) {
        console.error('Error fetching recent activity:', error);
    }
}

// ── Overview date picker ─────────────────────────────────────────────────────
var overviewDatePicker = null;
(function initOverviewPicker() {
    if (typeof HDatePicker === 'undefined') {
        console.warn('HDatePicker not loaded yet — deferring');
        window.addEventListener('load', initOverviewPicker);
        return;
    }
    overviewDatePicker = new HDatePicker('overviewDatePicker', function(range) {
        var start = Math.floor(range.start.getTime() / 1000);
        var end   = Math.floor(range.end.getTime()   / 1000);
        fetchStats(start, end);
        renderCharts(start, end);
        fetchRecentActivity(start, end);
    });
})();

document.getElementById('syncBtn').addEventListener('click', async () => {
    const btn = document.getElementById('syncBtn');
    btn.textContent = 'Syncing...';
    btn.disabled = true;
    try {
        const res = await fetch('/api/sync', { method: 'POST' });
        const data = await res.json();
        if (data.status === 'already_running') {
            btn.textContent = 'Sync In Progress...';
        }
        const pollSync = setInterval(async () => {
            try {
                const statusRes = await fetch('/api/sync/status');
                const status = await statusRes.json();
                if (!status.running) {
                    clearInterval(pollSync);
                    btn.textContent = status.last_result === 'success' ? 'Sync Complete' : 'Sync Had Errors';
                    btn.disabled = false;
                    fetchStats();
                    renderCharts();
                    renderDistributionChart();
                    fetchRecentActivity();
                    loadAgingWidget();
                    setTimeout(() => { btn.textContent = 'Sync Now'; }, 3000);
                }
            } catch (err) {
                clearInterval(pollSync);
                btn.textContent = 'Error';
                btn.disabled = false;
            }
        }, 2000);
    } catch (e) {
        btn.textContent = 'Error';
        btn.disabled = false;
    }
});

// View Management
function showView(viewName) {
    console.log(`Showing view: ${viewName}`);
    document.querySelectorAll('.view-container').forEach(v => {
        v.classList.remove('active');
        v.style.display = 'none'; // Backup for CSS
    });
    document.querySelectorAll('.nav-item').forEach(n => n.classList.remove('active'));

    const specialViews = { 'overview': 'view-overview', 'setup': 'view-setup', 'amortizations': 'view-amortizations', 'analysis': 'view-analysis', 'backup': 'view-backup' };
    const targetViewId = specialViews[viewName] || 'view-entity';

    const targetView = document.getElementById(targetViewId);
    if (targetView) {
        targetView.classList.add('active');
        targetView.style.display = 'block'; // Force show

        // Handle navbar active state
        const navItem = document.querySelector(`.nav-item[data-view="${viewName}"]`);
        if (navItem) navItem.classList.add('active');

        if (viewName === 'amortizations') {
            loadAmortizations();
        } else if (viewName === 'analysis') {
            loadAnalysisView();
        } else if (viewName === 'backup') {
            loadBackupView();
        } else if (!specialViews[viewName]) {
            loadEntityData(viewName);
            // Show/hide invoice sub-tabs
            const subTabs = document.getElementById('invoiceSubTabs');
            if (subTabs) subTabs.style.display = viewName === 'invoices' ? 'flex' : 'none';
            if (viewName === 'invoices') {
                // Reset to "all" tab on each navigation
                switchInvoiceTab('all');
            }
        }
    } else {
        console.error(`View not found: ${targetViewId}`);
    }
}

// Global state for entity view
let currentEntityData = [];
let currentSort = { column: null, direction: 'asc' };

async function loadEntityData(entity) {
    const titleMap = {
        'contacts': 'Contacts (Clients & Suppliers)',
        'products': 'Inventory (Products & Services)',
        'invoices': 'Sales Invoices',
        'purchases': 'Expenses (Purchase Invoices)',
        'estimates': 'Estimates (Presupuestos)'
    };

    // Show/hide date picker based on entity type
    const datePickerEl = document.getElementById('entityDatePicker');
    const isFinancial = ['invoices', 'purchases', 'estimates'].includes(entity);
    if (datePickerEl) datePickerEl.style.visibility = isFinancial ? 'visible' : 'hidden';

    document.getElementById('entityViewTitle').textContent = titleMap[entity] || 'Entity Details';
    const thead = document.getElementById('entityThead');
    const tbody = document.getElementById('entityTbody');

    tbody.innerHTML = '<tr><td colspan="100" style="text-align:center">Loading data...</td></tr>';

    try {
        console.log(`Fetching entity: ${entity}`);
        const response = await fetch(`/api/entities/${entity}`);
        if (!response.ok) throw new Error(`Server returned ${response.status}`);

        currentEntityData = await response.json();
        renderEntityTable(entity);
    } catch (e) {
        console.error(`Error in loadEntityData(${entity}):`, e);
        tbody.innerHTML = `<tr><td colspan="100" style="text-align:center; color: var(--danger)">Error: ${e.message}</td></tr>`;
    }
}

function renderEntityTable(entity) {
    const thead = document.getElementById('entityThead');
    const tbody = document.getElementById('entityTbody');
    const searchTerm = document.getElementById('entitySearch').value.toLowerCase();
    // Use HDatePicker range when available, otherwise no date filter
    const startDate = entityDateRange ? entityDateRange.start : null;
    const endDate   = entityDateRange ? entityDateRange.end   : null;

    if (!currentEntityData || currentEntityData.length === 0) {
        tbody.innerHTML = '<tr><td colspan="100" style="text-align:center">No records found.</td></tr>';
        return;
    }

    const allKeys = Object.keys(currentEntityData[0]);
    const showPdf = ['invoices', 'estimates', 'purchases'].includes(entity);
    const showActions = ['invoices', 'estimates', 'purchases', 'contacts', 'products'].includes(entity);

    // ── Feature 1+2: Column visibility with localStorage persistence ──────────
    // Default: hide fields ending in _id (except 'id' itself stays hidden too)
    const DEFAULT_HIDDEN = new Set(['id', ...allKeys.filter(k => k !== 'id' && k.endsWith('_id'))]);
    const STORAGE_KEY = `col_config_${entity}`;
    let colConfig = null;
    try { colConfig = JSON.parse(localStorage.getItem(STORAGE_KEY)); } catch(e) {}
    // colConfig = { visible: [...], order: [...] } | null
    // If no saved config, use all keys minus defaults hidden
    // For invoice/purchase/estimate: doc_number and date are pinned first
    const DOC_FIRST_ENTITIES = ['invoices', 'purchases', 'estimates'];
    let keys;
    if (colConfig && colConfig.order && colConfig.visible) {
        keys = colConfig.order.filter(k => colConfig.visible.includes(k));
    } else {
        const baseKeys = allKeys.filter(k => !DEFAULT_HIDDEN.has(k));
        if (DOC_FIRST_ENTITIES.includes(entity) && baseKeys.includes('doc_number')) {
            // Pin doc_number first, then date, then the rest
            const pinned = ['doc_number', 'date'].filter(k => baseKeys.includes(k));
            const rest = baseKeys.filter(k => !pinned.includes(k));
            keys = [...pinned, ...rest];
        } else {
            keys = baseKeys;
        }
    }
    // Column widths: { colKey: widthPx }
    let colWidths = (colConfig && colConfig.widths) ? colConfig.widths : {};
    function saveColConfig() {
        localStorage.setItem(STORAGE_KEY, JSON.stringify({ visible: keys, order: keys, allKeys: allKeys, widths: colWidths }));
    }

    // Holded direct-edit URLs per entity type
    const holdedUrl = (entity, id) => {
        const map = {
            'invoices':  `https://app.holded.com/sales#open:invoice-${id}`,
            'purchases': `https://app.holded.com/purchases#open:purchase-${id}`,
            'estimates': `https://app.holded.com/sales#open:estimate-${id}`,
            'contacts':  `https://app.holded.com/contacts/${id}`,
            'products':  `https://app.holded.com/inventory/products/${id}`,
        };
        return map[entity] || null;
    };

    // ── Header render with right-click column configurator + drag to reorder ──
    while (thead.firstChild) thead.removeChild(thead.firstChild);
    const headerRow = document.createElement('tr');
    let dragSrcKey = null;

    keys.forEach(function(k) {
        const th = document.createElement('th');
        th.className = currentSort.column === k ? `sort-${currentSort.direction}` : '';
        th.textContent = k.replace(/_/g, ' ').toUpperCase();
        th.style.cursor = 'grab';
        th.dataset.colKey = k;
        th.draggable = true;

        // Apply saved width if any
        if (colWidths[k]) th.style.width = colWidths[k] + 'px';

        // ── Column resizer handle ──
        const resizer = document.createElement('div');
        resizer.className = 'col-resizer';
        resizer.addEventListener('mousedown', function(e) {
            e.preventDefault();
            e.stopPropagation();
            // Disable drag while resizing
            th.draggable = false;
            resizer.classList.add('resizing');
            const startX = e.clientX;
            const startW = th.offsetWidth;
            function onMove(e) {
                const newW = Math.max(40, startW + (e.clientX - startX));
                th.style.width = newW + 'px';
            }
            function onUp() {
                resizer.classList.remove('resizing');
                th.draggable = true;
                colWidths[k] = th.offsetWidth;
                saveColConfig();
                document.removeEventListener('mousemove', onMove);
                document.removeEventListener('mouseup', onUp);
            }
            document.addEventListener('mousemove', onMove);
            document.addEventListener('mouseup', onUp);
        });
        th.appendChild(resizer);

        // ── Sort on click (only if not dragging) ──
        th.addEventListener('click', function(){ handleSort(entity, k); });

        // ── Right-click column configurator ──
        th.addEventListener('contextmenu', function(e){
            e.preventDefault();
            openColMenu(e, entity, allKeys, keys, DEFAULT_HIDDEN, function(newKeys){
                keys = newKeys;
                saveColConfig();
                renderEntityTable(entity);
            });
        });

        // ── Drag & drop handlers ──
        th.addEventListener('dragstart', function(e) {
            dragSrcKey = k;
            e.dataTransfer.effectAllowed = 'move';
            th.classList.add('col-dragging');
        });
        th.addEventListener('dragend', function() {
            th.classList.remove('col-dragging');
            headerRow.querySelectorAll('th').forEach(t => t.classList.remove('col-drag-over'));
        });
        th.addEventListener('dragover', function(e) {
            if (dragSrcKey && dragSrcKey !== k) {
                e.preventDefault();
                e.dataTransfer.dropEffect = 'move';
                headerRow.querySelectorAll('th').forEach(t => t.classList.remove('col-drag-over'));
                th.classList.add('col-drag-over');
            }
        });
        th.addEventListener('dragleave', function() {
            th.classList.remove('col-drag-over');
        });
        th.addEventListener('drop', function(e) {
            e.preventDefault();
            th.classList.remove('col-drag-over');
            if (!dragSrcKey || dragSrcKey === k) return;
            // Reorder keys array
            const fromIdx = keys.indexOf(dragSrcKey);
            const toIdx   = keys.indexOf(k);
            if (fromIdx === -1 || toIdx === -1) return;
            keys.splice(fromIdx, 1);
            keys.splice(toIdx, 0, dragSrcKey);
            dragSrcKey = null;
            saveColConfig();
            renderEntityTable(entity);
        });

        headerRow.appendChild(th);
    });
    if (showActions) {
        const thA = document.createElement('th');
        thA.textContent = 'ACTIONS';
        headerRow.appendChild(thA);
    }
    thead.appendChild(headerRow);

    // Filter Data
    let filteredData = currentEntityData.filter(row => {
        // Search filter
        const matchesSearch = keys.some(k => String(row[k]).toLowerCase().includes(searchTerm));
        if (!matchesSearch) return false;

        // Date filter (if applicable and set)
        if (startDate && endDate && row.date) {
            const rowDate = row.date; // already unix epoch (seconds)
            const startEpoch = Math.floor(startDate.getTime() / 1000);
            const endEpoch   = Math.floor(endDate.getTime()   / 1000);
            if (rowDate < startEpoch || rowDate > endEpoch) return false;
        }

        return true;
    });

    // Sort Data
    if (currentSort.column) {
        filteredData.sort((a, b) => {
            let valA = a[currentSort.column];
            let valB = b[currentSort.column];

            if (valA === valB) return 0;
            if (valA === null) return 1;
            if (valB === null) return -1;

            const direction = currentSort.direction === 'asc' ? 1 : -1;
            return valA > valB ? direction : -direction;
        });
    }

    tbody.innerHTML = '';
    filteredData.forEach(row => {
        const tr = document.createElement('tr');
        const interactive = ['contacts', 'products', 'invoices', 'estimates', 'purchases'].includes(entity);
        if (interactive) tr.style.cursor = 'pointer';

        keys.forEach(key => {
            const td = document.createElement('td');
            td.addEventListener('click', (e) => {
                if (!e.target.closest('.action-btn')) {
                    if (entity === 'contacts') openContactDetails(row.id, row.name);
                    else if (entity === 'products') openProductDetails(row.id, row.name);
                    else openDocumentDetails(entity, row.id);
                }
            });

            let val = row[key];
            const moneyKeys = ['amount', 'price', 'total', 'subtotal', 'tax', 'discount', 'balance', 'budget', 'stock'];
            const dateKeys = ['date', 'time', 'created', 'updated'];

            if (key.toLowerCase() === 'status' && (entity === 'invoices' || entity === 'estimates' || entity === 'purchases')) {
                td.innerHTML = getStatusBadge(entity, val);
            } else if (moneyKeys.includes(key.toLowerCase()) && val !== null) {
                const num = parseFloat(val);
                td.textContent = isNaN(num) ? val : formatter.format(num);
                td.style.fontWeight = '700';
            } else if (dateKeys.includes(key.toLowerCase()) && typeof val === 'number' && val > 0) {
                td.textContent = new Date(val * 1000).toLocaleDateString();
            } else {
                td.textContent = val !== null && val !== undefined ? val : '-';
            }
            tr.appendChild(td);
        });

        if (showActions) {
            const td = document.createElement('td');
            td.style.whiteSpace = 'nowrap';
            const url = holdedUrl(entity, row.id);
            const editBtn = url
                ? `<a class="action-btn" href="${url}" target="_blank" rel="noopener"
                      title="Editar en Holded" onclick="event.stopPropagation()"
                      style="text-decoration:none;display:inline-flex;align-items:center;gap:3px">
                      ✏️ Holded
                   </a>`
                : '';
            const pdfBtn = showPdf
                ? `<button class="action-btn btn-secondary" title="Ver PDF"
                      onclick="event.stopPropagation(); openPdfModal('${escapeHtml(entity)}', '${escapeHtml(row.id)}')">
                      👁️ PDF
                   </button>`
                : '';
            td.innerHTML = `<div style="display:flex;gap:4px;align-items:center">${pdfBtn}${editBtn}</div>`;
            tr.appendChild(td);
        }
        tbody.appendChild(tr);
    });
}

function handleSort(entity, column) {
    if (currentSort.column === column) {
        currentSort.direction = currentSort.direction === 'asc' ? 'desc' : 'asc';
    } else {
        currentSort.column = column;
        currentSort.direction = 'asc';
    }
    renderEntityTable(entity);
}

// Add event listeners for filters
document.getElementById('entitySearch').addEventListener('input', () => {
    const currentView = document.querySelector('.nav-item.active').getAttribute('data-view');
    if (currentView !== 'overview') renderEntityTable(currentView);
});

// ── Entity date picker ───────────────────────────────────────────────────────
var entityDateRange = null; // { start: Date, end: Date } | null
var entityDatePicker = null;
(function initEntityPicker() {
    if (typeof HDatePicker === 'undefined') {
        window.addEventListener('load', initEntityPicker);
        return;
    }
    entityDatePicker = new HDatePicker('entityDatePicker', function(range) {
        entityDateRange = range;
        var currentView = (document.querySelector('.nav-item.active') || {}).getAttribute &&
                          document.querySelector('.nav-item.active').getAttribute('data-view');
        if (currentView && currentView !== 'overview') renderEntityTable(currentView);
    });
})();

async function openDocumentDetails(type, id) {
    console.log(`Opening details for ${type} ID: ${id}`);
    const modal = document.getElementById('detailsModal');
    const thead = document.getElementById('modalThead');
    const tbody = document.getElementById('modalTbody');
    const title = document.getElementById('modalTitle');

    if (!modal) {
        console.error('detailsModal not found in DOM');
        return;
    }

    modal.classList.add('active');
    title.textContent = `Loading details for ${id}...`;
    tbody.innerHTML = '<tr><td colspan="100" style="text-align:center">Loading line items...</td></tr>';

    try {
        // Correct endpoint for purchases if needed
        const url = `/api/entities/${type}/${id}/items`;
        console.log(`Fetching: ${url}`);

        const response = await fetch(url);
        if (!response.ok) throw new Error(`Status ${response.status}`);

        const data = await response.json();
        console.log('Line items data:', data);

        title.textContent = `Details for ${type.slice(0, -1).toUpperCase()} #${id}`;

        if (!data || data.length === 0) {
            tbody.innerHTML = '<tr><td colspan="100" style="text-align:center">No line items found for this document.</td></tr>';
            return;
        }

        const keys = Object.keys(data[0]);
        const displayKeys = keys.filter(k => !k.includes('_id') && k !== 'id');

        thead.innerHTML = `<tr>${displayKeys.map(k => {
            const labels = {
                'name': 'CONCEPTO',
                'sku': 'SKU',
                'units': 'UDS',
                'price': 'PRECIO',
                'subtotal': 'SUBTOTAL',
                'discount': 'DTO %',
                'tax': 'IVA %',
                'retention': 'IRPF %',
                'account': 'CUENTA'
            };
            return `<th>${labels[k.toLowerCase()] || k.toUpperCase()}</th>`;
        }).join('')}</tr>`;

        tbody.innerHTML = '';
        data.forEach(item => {
            const tr = document.createElement('tr');
            displayKeys.forEach(key => {
                const td = document.createElement('td');
                let val = item[key];

                if (['price', 'subtotal', 'tax', 'discount', 'units', 'retention'].includes(key.toLowerCase())) {
                    if (val === null || val === undefined) {
                        td.textContent = '-';
                    } else if (['tax', 'retention', 'discount'].includes(key.toLowerCase())) {
                        td.textContent = `${val}%`;
                    } else {
                        td.textContent = formatter.format(val);
                    }
                    td.style.fontWeight = '700';
                } else {
                    td.textContent = val !== null ? val : '-';
                }
                tr.appendChild(td);
            });
            tbody.appendChild(tr);
        });
    } catch (e) {
        console.error('Error opening document details:', e);
        tbody.innerHTML = `<tr><td colspan="100" style="text-align:center; color: var(--danger)">
            Error loading details for ${type} #${id}: ${e.message}
        </td></tr>`;
    }
}

async function openProductDetails(id, name) {
    console.log(`Opening details for product: ${name} (${id})`);
    const modal = document.getElementById('detailsModal');
    const thead = document.getElementById('modalThead');
    const tbody = document.getElementById('modalTbody');
    const title = document.getElementById('modalTitle');

    modal.classList.add('active');
    title.textContent = `History for ${name}...`;
    tbody.innerHTML = '<tr><td colspan="100" style="text-align:center">Loading history...</td></tr>';

    try {
        const response = await fetch(`/api/entities/products/${id}/history`);
        const data = await response.json();

        title.textContent = `Product Sales & Purchase History: ${name}`;

        if (!data || data.length === 0) {
            tbody.innerHTML = '<tr><td colspan="100" style="text-align:center">No transactions found for this product.</td></tr>';
            return;
        }

        thead.innerHTML = `
            <tr>
                <th>TYPE</th>
                <th>DOC ID</th>
                <th>DATE</th>
                <th>UNITS</th>
                <th>PRICE</th>
                <th>TOTAL</th>
                <th>ACTIONS</th>
            </tr>
        `;

        tbody.innerHTML = '';
        data.forEach(item => {
            const tr = document.createElement('tr');
            const date = new Date(item.date * 1000).toLocaleDateString();
            const typeBadge = item.type === 'income' ? 'badge-income' : 'badge-expense';
            const docType = item.type === 'income' ? 'invoices' : 'purchases';

            tr.innerHTML = `
                <td><span class="badge ${typeBadge}">${escapeHtml(item.type).toUpperCase()}</span></td>
                <td>${escapeHtml(item.doc_id)}</td>
                <td>${escapeHtml(date)}</td>
                <td style="font-weight:700">${item.units}</td>
                <td>${formatter.format(item.price)}</td>
                <td style="font-weight:700">${formatter.format(item.subtotal)}</td>
                <td>
                    <div style="display:flex; gap:5px">
                        <button class="action-btn" title="View Details" onclick="openDocumentDetails('${escapeHtml(docType)}', '${escapeHtml(item.doc_id)}')">
                            📄
                        </button>
                    </div>
                </td>
            `;
            tbody.appendChild(tr);
        });
    } catch (e) {
        console.error('Error loading product history:', e);
        tbody.innerHTML = `<tr><td colspan="100" style="text-align:center; color: var(--danger)">
            Error loading history: ${escapeHtml(e.message)}
        </td></tr>`;
    }
}

async function openContactDetails(id, name) {
    console.log(`Opening details for contact: ${name} (${id})`);
    const modal = document.getElementById('detailsModal');
    const thead = document.getElementById('modalThead');
    const tbody = document.getElementById('modalTbody');
    const title = document.getElementById('modalTitle');

    modal.classList.add('active');
    title.textContent = `History for ${name}...`;
    tbody.innerHTML = '<tr><td colspan="100" style="text-align:center">Loading history...</td></tr>';

    try {
        const response = await fetch(`/api/entities/contacts/${id}/history`);
        const data = await response.json();

        title.textContent = `Transaction History: ${name}`;

        if (!data || data.length === 0) {
            tbody.innerHTML = '<tr><td colspan="100" style="text-align:center">No transactions found for this contact.</td></tr>';
            return;
        }

        thead.innerHTML = `
            <tr>
                <th>TYPE</th>
                <th>DOC ID</th>
                <th>DATE</th>
                <th>AMOUNT</th>
                <th>STATUS</th>
                <th>ACTIONS</th>
            </tr>
        `;

        tbody.innerHTML = '';
        data.forEach(item => {
            const tr = document.createElement('tr');
            const date = new Date(item.date * 1000).toLocaleDateString();
            const typeBadge = item.type === 'income' ? 'badge-income' : 'badge-expense';
            const docType = item.type === 'income' ? 'invoices' : 'purchases';

            tr.innerHTML = `
                <td><span class="badge ${typeBadge}">${escapeHtml(item.type).toUpperCase()}</span></td>
                <td>${escapeHtml(item.id)}</td>
                <td>${escapeHtml(date)}</td>
                <td style="font-weight:700">${formatter.format(item.amount)}</td>
                <td>${getStatusBadge(docType, item.status)}</td>
                <td>
                    <div style="display:flex; gap:5px">
                        <button class="action-btn" title="View Details" onclick="openDocumentDetails('${escapeHtml(docType)}', '${escapeHtml(item.id)}')">
                            📄
                        </button>
                        <button class="action-btn" title="View PDF" onclick="openPdfModal('${escapeHtml(docType)}', '${escapeHtml(item.id)}')">
                            👁️
                        </button>
                    </div>
                </td>
            `;
            tbody.appendChild(tr);
        });
    } catch (e) {
        console.error('Error loading contact history:', e);
        tbody.innerHTML = `<tr><td colspan="100" style="text-align:center; color: var(--danger)">
            Error loading history: ${escapeHtml(e.message)}
        </td></tr>`;
    }
}

function closeModal() {
    document.getElementById('detailsModal').classList.remove('active');
}

let currentPdfData = null;

function openPdfModal(type, id) {
    const modal = document.getElementById('pdfModal');
    const frame = document.getElementById('pdfFrame');
    const title = document.getElementById('pdfModalTitle');
    const shareBtn = document.getElementById('pdfShareBtn');

    currentPdfData = { type, id };

    // Set title based on doc type
    const labels = { 'invoices': 'Invoice', 'estimates': 'Estimate', 'purchases': 'Purchase' };
    title.textContent = `${labels[type] || 'Document'} Preview - ${id}`;

    // Set iframe source to our proxy endpoint
    frame.src = `/api/entities/${type}/${id}/pdf`;
    modal.classList.add('active');

    // Set up share button
    shareBtn.onclick = () => shareDocument(type, id);
}

function closePdfModal() {
    const modal = document.getElementById('pdfModal');
    const frame = document.getElementById('pdfFrame');
    modal.classList.remove('active');
    frame.src = 'about:blank'; // Stop loading
}

async function shareDocument(type, id) {
    const url = `${window.location.origin}/api/entities/${type}/${id}/pdf`;
    const title = `Document from Holded Local`;
    const text = `Take a look at this ${type.slice(0, -1)}: ${id}`;

    if (navigator.share) {
        try {
            await navigator.share({
                title: title,
                text: text,
                url: url
            });
            console.log('Document shared successfully');
        } catch (err) {
            console.error('Error sharing:', err);
        }
    } else {
        // Fallback: Copy to clipboard
        try {
            await navigator.clipboard.writeText(url);
            alert('Share link copied to clipboard!');
        } catch (err) {
            window.open(url, '_blank');
        }
    }
}

// AI Analyst Logic (Variables moved to top)
async function finishSetup() {
    const key = document.getElementById('setupApiKey').value.trim();
    const errorEl = document.getElementById('setupError1');
    if (!key) return;

    errorEl.textContent = 'Verifying key...';
    try {
        const response = await fetch('/api/config', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ apiKey: key })
        });
        const data = await response.json();

        if (data.status === 'success') {
            errorEl.textContent = '';
            showView('overview');
            init(); // Re-fetch all data
        } else {
            errorEl.textContent = data.message || 'Invalid Holded API Key';
        }
    } catch (e) {
        errorEl.textContent = 'Could not reach server.';
    }
}

document.getElementById('finishSetupBtn')?.addEventListener('click', finishSetup);

let distributionChartInstance = null;

async function renderDistributionChart() {
    try {
        const response = await fetch('/api/stats/top-contacts');
        const data = await response.json();

        const canvas = document.getElementById('distributionChart');
        if (!canvas) return;
        const ctx = canvas.getContext('2d');

        if (distributionChartInstance) {
            distributionChartInstance.destroy();
        }

        const labels = data.map(d => d.contact_name || 'Unknown');
        const values = data.map(d => d.total);
        const colors = ['#10b981', '#3b82f6', '#f59e0b', '#8b5cf6', '#f43f5e'];

        distributionChartInstance = new Chart(ctx, {
            type: 'doughnut',
            data: {
                labels: labels,
                datasets: [{
                    data: values,
                    backgroundColor: colors.slice(0, labels.length),
                    borderColor: '#0f172a',
                    borderWidth: 3
                }]
            },
            options: {
                responsive: true,
                maintainAspectRatio: false,
                plugins: {
                    legend: {
                        position: 'bottom',
                        labels: { color: '#94a3b8', font: { size: 11 }, padding: 12 }
                    },
                    tooltip: {
                        backgroundColor: '#1e293b',
                        titleColor: '#f8fafc',
                        bodyColor: '#94a3b8',
                        padding: 12,
                        borderRadius: 12,
                        callbacks: {
                            label: function(ctx) {
                                return `${ctx.label}: ${formatter.format(ctx.raw)}`;
                            }
                        }
                    }
                }
            }
        });
    } catch (error) {
        console.error('Error rendering distribution chart:', error);
    }
}

async function init() {
    // Wire up sidebar navigation
    document.querySelectorAll('.nav-item').forEach(item => {
        item.addEventListener('click', (e) => {
            if (e.target.closest('.nav-create-btn')) return; // let the link handle it
            showView(item.getAttribute('data-view'));
        });
    });

    // Wire up Excel export button
    document.getElementById('exportExcelBtn')?.addEventListener('click', () => {
        window.location.href = '/api/reports/excel';
    });

    // Check config and decide initial view
    try {
        const response = await fetch('/api/config');
        const config = await response.json();
        if (config.hasKey) {
            showView('overview');
            fetchStats();
            renderCharts();
            renderDistributionChart();
            fetchRecentActivity();
            loadAgingWidget();
        } else {
            showView('setup');
        }
    } catch (e) {
        console.error('Failed to load config:', e);
        showView('setup');
    }
}

init();

// ─── AI Chat ────────────────────────────────────────────────────────
// Note: innerHTML usage below is safe because all user/API content
// is first passed through escapeHtml() before rendering.

let chatOpen = false;
let chatConversationId = null;
let pendingStateId = null;
let chatSending = false;
let chatDrawerOpen = false;
let chatChartCounter = 0;

function toggleChat() {
    chatOpen = !chatOpen;
    document.getElementById('chatPanel').classList.toggle('open', chatOpen);
    if (chatOpen && !chatConversationId) {
        chatConversationId = crypto.randomUUID();
        checkAiConfig();
    }
    if (chatOpen) {
        document.getElementById('chatInput').focus();
    }
}

async function checkAiConfig() {
    try {
        const res = await fetch('/api/ai/config');
        const cfg = await res.json();
        if (!cfg.hasKey) {
            document.getElementById('aiSetupModal').style.display = 'flex';
        }
    } catch (e) {
        console.error('AI config check failed:', e);
    }
}

async function saveClaudeKey() {
    const key = document.getElementById('claudeApiKeyInput').value.trim();
    const errorEl = document.getElementById('aiSetupError');
    if (!key) { errorEl.textContent = 'Please enter a key.'; return; }
    try {
        const res = await fetch('/api/ai/config', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({ claudeApiKey: key })
        });
        const data = await res.json();
        if (data.status === 'success') {
            closeAiSetup();
        } else {
            errorEl.textContent = data.message || 'Error saving key.';
        }
    } catch (e) {
        errorEl.textContent = 'Connection error.';
    }
}

function closeAiSetup() {
    document.getElementById('aiSetupModal').style.display = 'none';
}

function sendSuggestion(text) {
    document.getElementById('chatInput').value = text;
    sendMessage();
}

// ─── Streaming Chat ─────────────────────────────────────────────────

async function sendMessage() {
    if (chatSending) return;
    const input = document.getElementById('chatInput');
    const message = input.value.trim();
    if (!message) return;

    input.value = '';
    autoResizeInput(input);
    hideWelcome();
    appendChatMessage('user', message);
    showThinking();
    chatSending = true;
    document.getElementById('chatSendBtn').disabled = true;

    try {
        const res = await fetch('/api/ai/chat/stream', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({ message, conversation_id: chatConversationId })
        });

        hideThinking();

        const reader = res.body.getReader();
        const decoder = new TextDecoder();
        let buffer = '';
        let streamDiv = null;
        let streamText = '';
        let toolsSummary = [];
        let chartsData = [];
        let currentEvent = null;

        while (true) {
            const { done, value } = await reader.read();
            if (done) break;

            buffer += decoder.decode(value, { stream: true });
            const lines = buffer.split('\n');
            buffer = lines.pop();

            for (const line of lines) {
                if (line.startsWith('event: ')) {
                    currentEvent = line.slice(7).trim();
                } else if (line.startsWith('data: ') && currentEvent) {
                    const data = JSON.parse(line.slice(6));

                    switch (currentEvent) {
                        case 'tool_start':
                            updateThinking('Using ' + data.tool.replace(/_/g, ' ') + '...');
                            break;

                        case 'tools_used':
                            toolsSummary = data;
                            break;

                        case 'charts':
                            chartsData = data;
                            break;

                        case 'text_delta':
                            if (!streamDiv) {
                                streamDiv = document.createElement('div');
                                streamDiv.className = 'chat-message chat-assistant';
                                document.getElementById('chatMessages').appendChild(streamDiv);
                            }
                            streamText += data.text;
                            streamDiv.innerHTML = renderChatMarkdown(escapeHtml(streamText));
                            document.getElementById('chatMessages').scrollTop = document.getElementById('chatMessages').scrollHeight;
                            break;

                        case 'confirmation_needed':
                            pendingStateId = data.pending_state_id;
                            chatConversationId = data.conversation_id || chatConversationId;
                            showConfirmation(data.action);
                            break;

                        case 'error':
                            if (data.content && data.content.includes('API key not configured')) {
                                document.getElementById('aiSetupModal').style.display = 'flex';
                            }
                            appendChatMessage('error', data.content);
                            break;

                        case 'done':
                            chatConversationId = data.conversation_id || chatConversationId;
                            break;
                    }
                    currentEvent = null;
                }
            }
        }

        // Add tool summary after streaming completes
        if (streamDiv && toolsSummary.length > 0) {
            const summary = document.createElement('div');
            summary.className = 'chat-tool-summary';
            summary.textContent = toolsSummary.map(t => t.tool.replace(/_/g, ' ')).join(' \u2192 ');
            streamDiv.appendChild(summary);
        }

        // Add favorite button to assistant messages
        if (streamDiv && streamText) {
            const favBtn = document.createElement('button');
            favBtn.className = 'chat-fav-btn';
            favBtn.title = 'Save this query as favorite';
            favBtn.textContent = '\u2B50';
            favBtn.onclick = function() { addFavorite(message); };
            streamDiv.appendChild(favBtn);

            // Check for download links
            const downloadMatch = streamText.match(/\/api\/reports\/download\/[^\s)]+/);
            if (downloadMatch) {
                const link = document.createElement('a');
                link.href = downloadMatch[0];
                link.className = 'chat-download-link';
                link.textContent = 'Download Report';
                link.target = '_blank';
                streamDiv.appendChild(link);
            }
        }

        // Render inline charts
        if (chartsData.length > 0) {
            chartsData.forEach(function(chart) { renderInlineChart(chart); });
        }

    } catch (e) {
        hideThinking();
        appendChatMessage('error', 'Connection error. Is the server running?');
    } finally {
        chatSending = false;
        document.getElementById('chatSendBtn').disabled = false;
    }
}

// ─── Inline Chart Rendering ─────────────────────────────────────────

function renderInlineChart(chartData) {
    const container = document.getElementById('chatMessages');
    const wrapper = document.createElement('div');
    wrapper.className = 'chat-message chat-assistant chat-chart-wrapper';

    const title = document.createElement('div');
    title.className = 'chat-chart-title';
    title.textContent = chartData.title;
    wrapper.appendChild(title);

    const canvasContainer = document.createElement('div');
    canvasContainer.className = 'chat-chart-container';
    const canvas = document.createElement('canvas');
    const chartId = 'chatChart_' + (chatChartCounter++);
    canvas.id = chartId;
    canvasContainer.appendChild(canvas);
    wrapper.appendChild(canvasContainer);
    container.appendChild(wrapper);
    container.scrollTop = container.scrollHeight;

    const colors = ['#10b981', '#3b82f6', '#f59e0b', '#8b5cf6', '#f43f5e', '#06b6d4', '#ec4899', '#84cc16'];
    const datasets = chartData.datasets.map(function(ds, i) {
        const color = colors[i % colors.length];
        const config = { label: ds.label, data: ds.data, borderColor: color, backgroundColor: color + '33', borderWidth: 2 };
        if (chartData.chart_type === 'doughnut' || chartData.chart_type === 'pie') {
            config.backgroundColor = chartData.labels.map(function(_, j) { return colors[j % colors.length] + 'cc'; });
            config.borderColor = '#0f172a';
            config.borderWidth = 3;
        } else if (chartData.chart_type === 'bar') {
            config.backgroundColor = color + '99';
        }
        return config;
    });

    new Chart(canvas.getContext('2d'), {
        type: chartData.chart_type,
        data: { labels: chartData.labels, datasets: datasets },
        options: {
            responsive: true,
            maintainAspectRatio: false,
            plugins: {
                legend: { position: 'bottom', labels: { color: '#94a3b8', font: { size: 11 } } },
                tooltip: { backgroundColor: '#1e293b', titleColor: '#f8fafc', bodyColor: '#94a3b8', padding: 10, borderRadius: 8 }
            },
            scales: (chartData.chart_type === 'doughnut' || chartData.chart_type === 'pie') ? {} : {
                y: { grid: { color: 'rgba(255,255,255,0.03)' }, ticks: { color: '#94a3b8' } },
                x: { grid: { display: false }, ticks: { color: '#94a3b8' } }
            }
        }
    });
}

// ─── History & Favorites Drawer ─────────────────────────────────────

function toggleHistoryDrawer() {
    chatDrawerOpen = !chatDrawerOpen;
    document.getElementById('chatDrawer').style.display = chatDrawerOpen ? 'block' : 'none';
    if (chatDrawerOpen) {
        loadConversations();
        loadFavorites();
    }
}

function switchDrawerTab(tab) {
    document.querySelectorAll('.drawer-tab').forEach(function(t) { t.classList.remove('active'); });
    if (tab === 'history') {
        document.querySelectorAll('.drawer-tab')[0].classList.add('active');
    } else {
        document.querySelectorAll('.drawer-tab')[1].classList.add('active');
    }
    document.getElementById('drawerHistory').style.display = tab === 'history' ? 'block' : 'none';
    document.getElementById('drawerFavorites').style.display = tab === 'favorites' ? 'block' : 'none';
}

async function loadConversations() {
    try {
        const res = await fetch('/api/ai/conversations');
        const convos = await res.json();
        const el = document.getElementById('drawerHistory');
        if (convos.length === 0) {
            el.textContent = '';
            const empty = document.createElement('div');
            empty.className = 'drawer-empty';
            empty.textContent = 'No conversations yet';
            el.appendChild(empty);
            return;
        }
        el.textContent = '';
        convos.forEach(function(c) {
            const item = document.createElement('div');
            item.className = 'drawer-item' + (c.conversation_id === chatConversationId ? ' active' : '');
            item.onclick = function() { loadConversation(c.conversation_id); };

            const titleDiv = document.createElement('div');
            titleDiv.className = 'drawer-item-title';
            titleDiv.textContent = (c.first_message || 'Untitled').substring(0, 40);
            item.appendChild(titleDiv);

            const metaDiv = document.createElement('div');
            metaDiv.className = 'drawer-item-meta';
            metaDiv.textContent = c.msg_count + ' msgs \u00B7 ' + (c.last_msg || '');
            item.appendChild(metaDiv);

            el.appendChild(item);
        });
    } catch (e) {
        console.error('Failed to load conversations:', e);
    }
}

async function loadConversation(convId) {
    chatConversationId = convId;
    document.getElementById('chatMessages').textContent = '';
    hideWelcome();
    toggleHistoryDrawer();

    try {
        const res = await fetch('/api/ai/history?conversation_id=' + encodeURIComponent(convId));
        const messages = await res.json();
        messages.forEach(function(msg) {
            appendChatMessage(msg.role, msg.content);
        });
    } catch (e) {
        appendChatMessage('error', 'Failed to load conversation.');
    }
}

async function loadFavorites() {
    try {
        const res = await fetch('/api/ai/favorites');
        const favs = await res.json();
        const el = document.getElementById('drawerFavorites');
        el.textContent = '';
        if (favs.length === 0) {
            const empty = document.createElement('div');
            empty.className = 'drawer-empty';
            empty.textContent = 'No favorites yet. Click \u2B50 on a response to save.';
            el.appendChild(empty);
            return;
        }
        favs.forEach(function(f) {
            const item = document.createElement('div');
            item.className = 'drawer-item';

            const titleDiv = document.createElement('div');
            titleDiv.className = 'drawer-item-title';
            titleDiv.style.cursor = 'pointer';
            titleDiv.style.flex = '1';
            titleDiv.textContent = '\u2B50 ' + (f.label || f.query.substring(0, 40));
            titleDiv.onclick = function() { sendSuggestion(f.query); };
            item.appendChild(titleDiv);

            const delBtn = document.createElement('button');
            delBtn.className = 'drawer-delete-btn';
            delBtn.title = 'Remove';
            delBtn.textContent = '\u00D7';
            delBtn.onclick = function(e) { e.stopPropagation(); removeFavorite(f.id); };
            item.appendChild(delBtn);

            el.appendChild(item);
        });
    } catch (e) {
        console.error('Failed to load favorites:', e);
    }
}

async function addFavorite(query) {
    try {
        await fetch('/api/ai/favorites', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({ query: query, label: query.substring(0, 50) })
        });
        const container = document.getElementById('chatMessages');
        const toast = document.createElement('div');
        toast.className = 'chat-toast';
        toast.textContent = '\u2B50 Saved to favorites';
        container.appendChild(toast);
        setTimeout(function() { toast.remove(); }, 2000);
    } catch (e) {
        console.error('Failed to add favorite:', e);
    }
}

async function removeFavorite(id) {
    try {
        await fetch('/api/ai/favorites/' + id, { method: 'DELETE' });
        loadFavorites();
    } catch (e) {
        console.error('Failed to remove favorite:', e);
    }
}

// ─── Chat UI Helpers ────────────────────────────────────────────────

function hideWelcome() {
    const w = document.getElementById('chatWelcome');
    if (w) w.style.display = 'none';
}

function appendChatMessage(role, content, toolCalls) {
    const container = document.getElementById('chatMessages');
    const div = document.createElement('div');
    div.className = 'chat-message chat-' + role;

    if (role === 'assistant') {
        // Content is escaped via escapeHtml before rendering as HTML
        div.innerHTML = renderChatMarkdown(escapeHtml(content));
        if (toolCalls && toolCalls.length > 0) {
            const summary = document.createElement('div');
            summary.className = 'chat-tool-summary';
            summary.textContent = toolCalls.map(function(t) { return t.tool.replace(/_/g, ' '); }).join(' \u2192 ');
            div.appendChild(summary);
        }
        const downloadMatch = content.match(/\/api\/reports\/download\/[^\s)]+/);
        if (downloadMatch) {
            const link = document.createElement('a');
            link.href = downloadMatch[0];
            link.className = 'chat-download-link';
            link.textContent = 'Download Report';
            link.target = '_blank';
            div.appendChild(link);
        }
    } else if (role === 'error') {
        div.textContent = content;
    } else {
        div.textContent = content;
    }

    container.appendChild(div);
    container.scrollTop = container.scrollHeight;
}

function renderChatMarkdown(text) {
    // Note: text is already HTML-escaped before this function is called
    return text
        .replace(/^### (.+)$/gm, '<strong style="font-size:1em">$1</strong>')
        .replace(/^## (.+)$/gm, '<strong style="font-size:1.05em">$1</strong>')
        .replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>')
        .replace(/\*(.+?)\*/g, '<em>$1</em>')
        .replace(/`(.+?)`/g, '<code>$1</code>')
        .replace(/^[-\u2022] (.+)$/gm, '<span style="display:block;padding-left:1em">\u2022 $1</span>')
        .replace(/^\d+\. (.+)$/gm, '<span style="display:block;padding-left:1em">$&</span>')
        .replace(/\n/g, '<br>');
}

function showThinking() {
    const container = document.getElementById('chatMessages');
    const div = document.createElement('div');
    div.className = 'chat-thinking';
    div.id = 'chatThinking';
    div.textContent = 'Analyzing ';
    const dots = document.createElement('span');
    dots.className = 'dots';
    for (let i = 0; i < 3; i++) {
        const dot = document.createElement('span');
        dot.textContent = '.';
        dots.appendChild(dot);
    }
    div.appendChild(dots);
    container.appendChild(div);
    container.scrollTop = container.scrollHeight;
}

function updateThinking(text) {
    let el = document.getElementById('chatThinking');
    if (!el) {
        showThinking();
        el = document.getElementById('chatThinking');
    }
    el.textContent = text + ' ';
    const dots = document.createElement('span');
    dots.className = 'dots';
    for (let i = 0; i < 3; i++) {
        const dot = document.createElement('span');
        dot.textContent = '.';
        dots.appendChild(dot);
    }
    el.appendChild(dots);
}

function hideThinking() {
    const el = document.getElementById('chatThinking');
    if (el) el.remove();
}

function showConfirmation(action) {
    const panel = document.getElementById('chatConfirmation');
    document.getElementById('confirmText').textContent = action.description;
    const details = action.details || {};
    let detailText = '';
    if (details.items) {
        detailText = details.items.map(function(i) {
            return i.name + ': ' + i.units + ' x ' + (i.price || 0).toFixed(2) + ' EUR';
        }).join('\n');
        const total = details.items.reduce(function(s, i) { return s + (i.units || 1) * (i.price || 0); }, 0);
        detailText += '\n\nTotal: ' + total.toFixed(2) + ' EUR';
    } else {
        detailText = JSON.stringify(details, null, 2);
    }
    document.getElementById('confirmDetails').textContent = detailText;
    panel.style.display = 'block';
}

async function handleConfirm(confirmed) {
    document.getElementById('chatConfirmation').style.display = 'none';
    if (!pendingStateId) return;

    showThinking();
    try {
        const res = await fetch('/api/ai/confirm', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({ pending_state_id: pendingStateId, confirmed: confirmed })
        });
        const data = await res.json();
        hideThinking();

        if (data.type === 'error') {
            appendChatMessage('error', data.content);
        } else {
            appendChatMessage('assistant', data.content, data.tool_calls_summary);
            if (data.charts && data.charts.length > 0) {
                data.charts.forEach(function(chart) { renderInlineChart(chart); });
            }
        }
    } catch (e) {
        hideThinking();
        appendChatMessage('error', 'Connection error.');
    }
    pendingStateId = null;
}

function clearChat() {
    chatConversationId = crypto.randomUUID();
    const container = document.getElementById('chatMessages');
    container.textContent = '';
    document.getElementById('chatConfirmation').style.display = 'none';
    pendingStateId = null;

    const welcome = document.createElement('div');
    welcome.className = 'chat-welcome';
    welcome.id = 'chatWelcome';

    const p = document.createElement('p');
    p.textContent = 'I can analyze your financial data, check prices, create estimates, send documents, and show visual charts.';
    welcome.appendChild(p);

    const suggestions = document.createElement('div');
    suggestions.className = 'chat-suggestions';
    var sugItems = [
        { text: 'Revenue this month', query: 'What is my revenue this month?' },
        { text: 'Top clients', query: 'Show me my top 5 clients' },
        { text: 'Income vs Expenses chart', query: 'Show me a chart of monthly income vs expenses' },
        { text: 'Overdue invoices', query: 'Do I have overdue invoices?' }
    ];
    sugItems.forEach(function(item) {
        const btn = document.createElement('button');
        btn.textContent = item.text;
        btn.onclick = function() { sendSuggestion(item.query); };
        suggestions.appendChild(btn);
    });
    welcome.appendChild(suggestions);
    container.appendChild(welcome);
}

document.addEventListener('DOMContentLoaded', function() {
    const input = document.getElementById('chatInput');
    if (input) {
        input.addEventListener('input', function() { autoResizeInput(input); });
    }
    // Restore saved theme on load
    const savedTheme = localStorage.getItem('theme');
    if (savedTheme === 'light') {
        document.body.classList.add('light-mode');
        const btn = document.getElementById('themeToggle');
        if (btn) btn.textContent = '☀️';
    }
});

// ── Dark / Light mode toggle ─────────────────────────────────────────────
function toggleTheme() {
    const isLight = document.body.classList.toggle('light-mode');
    const btn = document.getElementById('themeToggle');
    btn.textContent = isLight ? '☀️' : '🌙';
    localStorage.setItem('theme', isLight ? 'light' : 'dark');
}

function autoResizeInput(el) {
    el.style.height = 'auto';
    el.style.height = Math.min(el.scrollHeight, 120) + 'px';
}

// ─── File Management ────────────────────────────────────────────────────

// Handle file upload
document.getElementById('fileUploadInput')?.addEventListener('change', async (e) => {
    const file = e.target.files[0];
    if (!file) return;

    const formData = new FormData();
    formData.append('file', file);

    try {
        showThinking();
        const res = await fetch('/api/files/upload', {
            method: 'POST',
            body: formData
        });

        let data = {};
        try {
            data = await res.json();
        } catch (parseErr) {
            // JSON parse error - response might be error HTML
            data = { error: res.statusText || 'Unknown error' };
        }

        hideThinking();

        if (res.ok && data.success) {
            appendChatMessage('assistant', `✅ File uploaded: **${data.original_name}** (${(data.size / 1024).toFixed(1)} KB)`);
            // Auto-send message to AI to analyze
            const message = `I just uploaded "${data.filename}". Can you analyze it for me?`;
            document.getElementById('chatInput').value = message;
            sendMessage();
        } else {
            const errorMsg = data.detail || data.error || res.statusText || 'Upload failed';
            appendChatMessage('error', `Upload failed: ${errorMsg}`);
        }
    } catch (err) {
        hideThinking();
        appendChatMessage('error', `Upload error: ${err.message}`);
    }

    // Reset file input
    e.target.value = '';
});

async function openDirectoryConfig() {
    const modal = document.getElementById('directoryConfigModal');
    const errorEl = document.getElementById('directoryConfigError');
    const successEl = document.getElementById('directoryConfigSuccess');
    errorEl.textContent = '';
    successEl.textContent = '';

    // Load current config
    try {
        const res = await fetch('/api/files/config');
        const config = await res.json();
        document.getElementById('uploadsDir').value = config.uploads_dir || '';
        document.getElementById('reportsDir').value = config.reports_dir || '';
    } catch (e) {
        console.error('Failed to load config:', e);
    }

    modal.style.display = 'flex';
}

function closeDirectoryConfig() {
    document.getElementById('directoryConfigModal').style.display = 'none';
}

async function saveDirectoryConfig() {
    const uploadsDir = document.getElementById('uploadsDir').value.trim();
    const reportsDir = document.getElementById('reportsDir').value.trim();
    const errorEl = document.getElementById('directoryConfigError');
    const successEl = document.getElementById('directoryConfigSuccess');

    errorEl.textContent = '';
    successEl.style.display = 'none';

    if (!uploadsDir && !reportsDir) {
        errorEl.textContent = 'Please enter at least one path.';
        return;
    }

    try {
        const res = await fetch('/api/files/config', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({
                uploads_dir: uploadsDir || null,
                reports_dir: reportsDir || null
            })
        });
        const data = await res.json();

        if (data.uploads && data.uploads.error) {
            errorEl.textContent += `Uploads: ${data.uploads.error}\n`;
        }
        if (data.reports && data.reports.error) {
            errorEl.textContent += `Reports: ${data.reports.error}\n`;
        }

        if (!errorEl.textContent) {
            successEl.textContent = '✓ Configuration saved successfully';
            successEl.style.display = 'block';
            setTimeout(() => closeDirectoryConfig(), 2000);
        }
    } catch (e) {
        errorEl.textContent = `Connection error: ${e.message}`;
    }
}

// ============================================================
//  AMORTIZATIONS
// ============================================================

let _amortProducts = [];   // cached product list for select dropdown
let _amortChartInstance = null;  // Chart.js instance (destroy before re-render)
let _amortChartVisible = false;

// Fiscal type metadata (mirrors product_type_rules table)
const PRODUCT_TYPES = {
    alquiler: { label: '🔄 Alquiler',        color: '#3b82f6', irpf: 19, hint: 'Equipamiento cedido en uso. Retención IRPF 19% en facturas emitidas.' },
    venta:    { label: '🏷️ Venta',           color: '#10b981', irpf: 0,  hint: 'Venta directa. Sin IRPF. Cada compra debe correlacionarse con una venta.' },
    servicio: { label: '👤 Servicio / Fee',   color: '#f59e0b', irpf: 15, hint: 'Honorarios profesionales. Retención IRPF 15% en facturas emitidas.' },
    gasto:    { label: '📦 Gasto / Suplido',  color: '#94a3b8', irpf: 0,  hint: 'Gasto deducible o suplido. No genera ingreso directamente.' },
};

async function loadAmortizations() {
    try {
        const [dataRes, summaryRes] = await Promise.all([
            fetch('/api/amortizations'),
            fetch('/api/amortizations/summary')
        ]);
        const rows = await dataRes.json();
        const summary = await summaryRes.json();

        // Summary cards
        document.getElementById('amortTotalInvested').textContent = formatter.format(summary.total_invested || 0);
        document.getElementById('amortTotalRevenue').textContent = formatter.format(summary.total_revenue || 0);
        const profitEl = document.getElementById('amortTotalProfit');
        profitEl.textContent = formatter.format(summary.total_profit || 0);
        profitEl.style.color = (summary.total_profit >= 0) ? 'var(--primary)' : 'var(--danger)';
        const roiEl = document.getElementById('amortGlobalRoi');
        roiEl.textContent = `${summary.global_roi_pct || 0}%`;
        roiEl.style.color = (summary.global_roi_pct >= 0) ? 'var(--primary)' : 'var(--danger)';

        // Badges
        document.getElementById('amortBadges').innerHTML = `
            <span class="amort-badge badge-paid">✅ Amortizados: ${summary.amortized_count || 0}</span>
            <span class="amort-badge badge-pending">⏳ En curso: ${summary.in_progress_count || 0}</span>
            <span class="amort-badge badge-draft">📦 Total: ${summary.total_products || 0}</span>
        `;

        const tbody = document.getElementById('amortBody');
        if (!rows.length) {
            tbody.innerHTML = '<tr><td colspan="12" style="text-align:center;color:var(--text-gray);padding:2rem">Sin datos. Añade un producto para empezar.</td></tr>';
            document.getElementById('amortChartCard').style.display = 'none';
            return;
        }

        // Build rows — each product gets a main row + a hidden detail row for purchase links
        while (tbody.firstChild) tbody.removeChild(tbody.firstChild);
        rows.forEach(function(r) {
            const profitColor = r.profit >= 0 ? 'var(--primary)' : 'var(--danger)';
            const roiColor    = r.roi_pct >= 0 ? 'var(--primary)' : 'var(--danger)';
            const safeName    = r.product_name.replace(/'/g, "\\'");
            const safeType    = r.product_type || 'alquiler';
            const typeInfo    = PRODUCT_TYPES[safeType] || PRODUCT_TYPES.alquiler;
            const irpfBadge   = typeInfo.irpf > 0
                ? `<span style="font-size:0.78rem;color:var(--warning);font-weight:600">${typeInfo.irpf}%</span>`
                : `<span style="font-size:0.78rem;color:var(--text-gray)">—</span>`;
            const statusBadge = r.status === 'AMORTIZADO'
                ? '<span class="badge badge-paid">✅ AMORT.</span>'
                : '<span class="badge badge-pending">⏳ CURSO</span>';

            // Inline type selector
            const typeOpts = Object.entries(PRODUCT_TYPES).map(function([k, v]) {
                return '<option value="' + k + '"' + (k === safeType ? ' selected' : '') + '>' + v.label + '</option>';
            }).join('');
            const typeSelect = '<select onchange="updateAmortType(' + r.id + ', this.value)" style="font-size:0.78rem;padding:0.2rem 0.4rem;background:var(--glass-bg);border:1px solid var(--glass-border);border-radius:6px;color:var(--text-white)">' + typeOpts + '</select>';

            // Main row
            const mainTr = document.createElement('tr');
            mainTr.className = 'amort-main-row';
            mainTr.dataset.amortId = r.id;
            mainTr.style.cursor = 'pointer';
            mainTr.title = 'Clic para ver/editar fuentes de coste';
            mainTr.addEventListener('click', function(e) {
                if (e.target.tagName === 'SELECT' || e.target.tagName === 'BUTTON' || e.target.tagName === 'OPTION') return;
                toggleAmortDetail(r.id);
            });
            mainTr.innerHTML = `
                <td><span class="amort-expand-icon" id="amort-expand-${r.id}">▶</span> <strong>${escapeHtml(r.product_name)}</strong></td>
                <td>${typeSelect}</td>
                <td style="text-align:center">${irpfBadge}</td>
                <td id="amort-cost-${r.id}"><strong>${formatter.format(r.purchase_price)}</strong></td>
                <td>${escapeHtml(r.purchase_date)}</td>
                <td>${formatter.format(r.total_revenue)}</td>
                <td style="color:${profitColor};font-weight:600">${formatter.format(r.profit)}</td>
                <td style="color:${roiColor};font-weight:600">${r.roi_pct}%</td>
                <td>${statusBadge}</td>
                <td style="white-space:nowrap">
                    <button class="action-btn btn-secondary" style="padding:0.3rem 0.6rem;font-size:0.8rem"
                        onclick="openAmortHistory('${escapeHtml(r.product_id)}','${safeName}',${r.purchase_price},${r.total_revenue});event.stopPropagation()">
                        📋 Historial
                    </button>
                    <button class="action-btn btn-secondary" style="padding:0.3rem 0.6rem;font-size:0.8rem"
                        onclick="openAmortizationModal(${r.id},'${safeName}',${r.purchase_price},'${escapeHtml(r.purchase_date)}','${escapeHtml(r.notes || '')}','${safeType}');event.stopPropagation()">
                        ✎
                    </button>
                    <button class="action-btn" style="padding:0.3rem 0.6rem;font-size:0.8rem;background:var(--danger)"
                        onclick="deleteAmortization(${r.id},'${safeName}');event.stopPropagation()">
                        ✕
                    </button>
                </td>
            `;
            tbody.appendChild(mainTr);

            // Detail row (hidden by default)
            const detailTr = document.createElement('tr');
            detailTr.id = 'amort-detail-' + r.id;
            detailTr.className = 'amort-detail-row';
            detailTr.style.display = 'none';
            const detailTd = document.createElement('td');
            detailTd.colSpan = 10;
            detailTd.className = 'amort-detail-cell';
            detailTd.id = 'amort-detail-cell-' + r.id;
            detailTd.innerHTML = '<span style="color:var(--text-gray);font-size:.82rem">Cargando fuentes de coste…</span>';
            detailTr.appendChild(detailTd);
            tbody.appendChild(detailTr);
        });

        if (_amortChartVisible) renderAmortChart(rows);
        liveSearch('amortSearch', 'amortBody');

    } catch (e) {
        console.error('Error loading amortizations:', e);
        document.getElementById('amortBody').innerHTML =
            `<tr><td colspan="12" style="text-align:center;color:var(--danger);padding:2rem">Error: ${e.message}</td></tr>`;
    }
}

// Inline type change — no modal needed
async function updateAmortType(id, newType) {
    try {
        await fetch(`/api/amortizations/${id}`, {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ product_type: newType })
        });
        // Refresh IRPF badge without full reload
        loadAmortizations();
    } catch (e) {
        alert(`Error guardando tipo: ${e.message}`);
    }
}

async function openAmortizationModal(id = null, name = '', price = '', date = '', notes = '', productType = 'alquiler') {
    if (!_amortProducts.length) {
        try {
            const res = await fetch('/api/entities/products');
            _amortProducts = await res.json();
        } catch (e) { _amortProducts = []; }
    }

    const select = document.getElementById('amortProductSelect');
    select.innerHTML = _amortProducts.map(p =>
        `<option value="${escapeHtml(p.id)}" data-name="${escapeHtml(p.name)}">${escapeHtml(p.name)}</option>`
    ).join('');

    document.getElementById('amortEditId').value = id || '';
    document.getElementById('amortPurchasePrice').value = price || '';
    document.getElementById('amortPurchaseDate').value = date || '';
    document.getElementById('amortNotes').value = notes || '';
    document.getElementById('amortModalError').textContent = '';

    // Set product type selector + hint
    const typeEl = document.getElementById('amortProductType');
    typeEl.value = productType || 'alquiler';
    _updateAmortTypeHint(typeEl.value);
    typeEl.onchange = () => _updateAmortTypeHint(typeEl.value);

    if (id) {
        document.getElementById('amortModalTitle').textContent = `Editar: ${name}`;
        select.disabled = true;
        const opt = [...select.options].find(o => o.text === name);
        if (opt) opt.selected = true;
    } else {
        document.getElementById('amortModalTitle').textContent = 'Añadir producto a seguimiento';
        select.disabled = false;
    }

    document.getElementById('amortModal').style.display = 'flex';
}

function _updateAmortTypeHint(typeKey) {
    const hint = document.getElementById('amortTypeHint');
    if (hint) hint.textContent = PRODUCT_TYPES[typeKey]?.hint || '';
}

function closeAmortizationModal() {
    document.getElementById('amortModal').style.display = 'none';
    document.getElementById('amortProductSelect').disabled = false;
}

async function saveAmortization() {
    const id          = document.getElementById('amortEditId').value;
    const select      = document.getElementById('amortProductSelect');
    const productId   = select.value;
    const productName = select.options[select.selectedIndex]?.dataset?.name || select.options[select.selectedIndex]?.text || '';
    const price       = parseFloat(document.getElementById('amortPurchasePrice').value);
    const date        = document.getElementById('amortPurchaseDate').value;
    const notes       = document.getElementById('amortNotes').value.trim();
    const productType = document.getElementById('amortProductType').value;
    const errorEl     = document.getElementById('amortModalError');

    errorEl.textContent = '';
    if (!productId)            { errorEl.textContent = 'Selecciona un producto.'; return; }
    if (isNaN(price) || price <= 0) { errorEl.textContent = 'Precio de compra inválido.'; return; }
    if (!date)                 { errorEl.textContent = 'Fecha de compra requerida.'; return; }

    try {
        const body = id
            ? { purchase_price: price, purchase_date: date, notes, product_type: productType }
            : { product_id: productId, product_name: productName, purchase_price: price, purchase_date: date, notes, product_type: productType };
        const res  = await fetch(id ? `/api/amortizations/${id}` : '/api/amortizations', {
            method: id ? 'PUT' : 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body)
        });
        const data = await res.json();
        if (!res.ok) { errorEl.textContent = data.detail || 'Error al guardar'; return; }
        closeAmortizationModal();
        loadAmortizations();
    } catch (e) {
        errorEl.textContent = `Error de conexión: ${e.message}`;
    }
}

async function deleteAmortization(id, name) {
    if (!confirm(`¿Eliminar "${name}" del seguimiento de amortizaciones?`)) return;
    try {
        const res = await fetch(`/api/amortizations/${id}`, { method: 'DELETE' });
        if (res.ok) {
            loadAmortizations();
        } else {
            const data = await res.json();
            alert(data.detail || 'Error al eliminar');
        }
    } catch (e) {
        alert(`Error: ${e.message}`);
    }
}

// Close amortization modal on click outside
window.addEventListener('click', (e) => {
    if (e.target.id === 'amortModal') closeAmortizationModal();
    if (e.target.id === 'amortHistoryModal') closeAmortHistory();
});

// ── Amortization ROI Chart ──────────────────────────────────────────

function toggleAmortChart() {
    const card = document.getElementById('amortChartCard');
    _amortChartVisible = !_amortChartVisible;

    if (_amortChartVisible) {
        card.style.display = 'block';
        // Fetch current rows and render
        fetch('/api/amortizations')
            .then(r => r.json())
            .then(rows => renderAmortChart(rows))
            .catch(() => {});
    } else {
        card.style.display = 'none';
    }

    // Update button text
    document.querySelectorAll('[onclick="toggleAmortChart()"]').forEach(btn => {
        btn.textContent = _amortChartVisible ? '📈 Ocultar gráfico' : '📈 Ver gráfico';
    });
}

function renderAmortChart(rows) {
    if (!rows.length) return;
    const ctx = document.getElementById('amortChart').getContext('2d');

    if (_amortChartInstance) {
        _amortChartInstance.destroy();
        _amortChartInstance = null;
    }

    const labels = rows.map(r => r.product_name);
    const invested = rows.map(r => r.purchase_price);
    const revenue = rows.map(r => r.total_revenue);
    const profit = rows.map(r => r.profit);

    _amortChartInstance = new Chart(ctx, {
        type: 'bar',
        data: {
            labels,
            datasets: [
                {
                    label: 'Invertido',
                    data: invested,
                    backgroundColor: 'rgba(248, 113, 113, 0.7)',
                    borderColor: '#f87171',
                    borderWidth: 1,
                    borderRadius: 4,
                },
                {
                    label: 'Revenue',
                    data: revenue,
                    backgroundColor: 'rgba(16, 185, 129, 0.7)',
                    borderColor: '#10b981',
                    borderWidth: 1,
                    borderRadius: 4,
                },
                {
                    label: 'Profit',
                    data: profit,
                    backgroundColor: profit.map(p => p >= 0 ? 'rgba(59,130,246,0.7)' : 'rgba(244,63,94,0.5)'),
                    borderColor: profit.map(p => p >= 0 ? '#3b82f6' : '#f43f5e'),
                    borderWidth: 1,
                    borderRadius: 4,
                }
            ]
        },
        options: {
            responsive: true,
            maintainAspectRatio: false,
            plugins: {
                legend: { labels: { color: '#94a3b8' } },
                tooltip: {
                    callbacks: {
                        label: ctx => ` ${ctx.dataset.label}: ${new Intl.NumberFormat('es-ES',{style:'currency',currency:'EUR'}).format(ctx.parsed.y)}`
                    }
                }
            },
            scales: {
                x: { ticks: { color: '#94a3b8' }, grid: { color: 'rgba(255,255,255,0.05)' } },
                y: {
                    ticks: {
                        color: '#94a3b8',
                        callback: v => `${(v/1000).toFixed(0)}k €`
                    },
                    grid: { color: 'rgba(255,255,255,0.05)' }
                }
            }
        }
    });
}

// ── Rental History Modal ────────────────────────────────────────────

async function openAmortHistory(productId, productName, purchasePrice, totalRevenue) {
    document.getElementById('amortHistoryTitle').textContent = `📋 Alquileres: ${productName}`;
    document.getElementById('amortHistoryModal').style.display = 'flex';
    document.getElementById('amortHistoryBody').innerHTML =
        '<tr><td colspan="5" style="text-align:center;padding:2rem;color:var(--text-gray)">Cargando...</td></tr>';

    try {
        const res = await fetch(`/api/entities/products/${productId}/history`);
        const history = await res.json();

        // Only income transactions (invoice_items)
        const rentals = history.filter(h => h.type === 'income');

        // Summary bar
        const profit = totalRevenue - purchasePrice;
        const roi = purchasePrice > 0 ? ((profit / purchasePrice) * 100).toFixed(1) : 0;
        const profitColor = profit >= 0 ? 'var(--primary)' : 'var(--danger)';
        document.getElementById('amortHistorySummary').innerHTML = `
            <div style="display:flex;gap:2rem;flex-wrap:wrap;font-size:0.9rem">
                <span>💰 <strong>Invertido:</strong> ${formatter.format(purchasePrice)}</span>
                <span>📈 <strong>Revenue total:</strong> ${formatter.format(totalRevenue)}</span>
                <span style="color:${profitColor}">✨ <strong>Profit:</strong> ${formatter.format(profit)}</span>
                <span style="color:${profitColor}">📊 <strong>ROI:</strong> ${roi}%</span>
                <span style="color:var(--text-gray)">🧾 <strong>${rentals.length}</strong> alquiler(es)</span>
            </div>
        `;

        if (!rentals.length) {
            document.getElementById('amortHistoryBody').innerHTML =
                '<tr><td colspan="5" style="text-align:center;padding:2rem;color:var(--text-gray)">Sin alquileres registrados en facturas aún.</td></tr>';
            return;
        }

        document.getElementById('amortHistoryBody').innerHTML = rentals.map(h => {
            const date = h.date ? new Date(h.date * 1000).toLocaleDateString('es-ES') : '—';
            const label = escapeHtml(h.doc_desc || h.contact_name || h.doc_id);
            const linkCell = h.doc_id
                ? `<a href="#" onclick="closeAmortHistory();openDetails('${h.doc_id}','invoices');return false;"
                      style="color:var(--primary);text-decoration:none;font-size:0.82rem" title="${escapeHtml(h.contact_name || '')}">${label}</a>`
                : '—';
            return `<tr>
                <td>${linkCell}</td>
                <td>${date}</td>
                <td style="text-align:center">${h.units ?? '—'}</td>
                <td>${formatter.format(h.price ?? 0)}</td>
                <td style="font-weight:600;color:var(--primary)">${formatter.format(h.subtotal ?? ((h.units ?? 0) * (h.price ?? 0)))}</td>
            </tr>`;
        }).join('');
        liveSearch('amortHistorySearch', 'amortHistoryBody');

    } catch (e) {
        document.getElementById('amortHistoryBody').innerHTML =
            `<tr><td colspan="5" style="text-align:center;color:var(--danger);padding:1rem">Error: ${e.message}</td></tr>`;
    }
}

function closeAmortHistory() {
    document.getElementById('amortHistoryModal').style.display = 'none';
}

// ============================================================
//  INVOICE ANALYSIS
// ============================================================

let _analysisPolling = null;

let _analyzedPage = 0;
const _analyzedPageSize = 50;

async function loadAnalysisView() {
    try {
        const [statusRes, matchesRes] = await Promise.all([
            fetch('/api/analysis/status'),
            fetch('/api/analysis/matches')
        ]);
        const status = await statusRes.json();
        const matches = await matchesRes.json();

        _renderAnalysisStatus(status);
        _renderMatches(matches);
        _renderCategoryBreakdown(status.by_category || []);

        // Populate category filter from breakdown data
        _populateCategoryFilter(status.by_category || []);
        // Load categorized invoices table
        _analyzedPage = 0;
        loadAnalyzedInvoices();
    } catch (e) {
        console.error('Error loading analysis view:', e);
    }
}

function _populateCategoryFilter(categories) {
    const sel = document.getElementById('analysisCategoryFilter');
    if (!sel) return;
    const current = sel.value;
    sel.innerHTML = '<option value="">Todas las categorías</option>';
    categories.forEach(c => {
        const opt = document.createElement('option');
        opt.value = c.category;
        opt.textContent = `${c.category} (${c.count})`;
        if (c.category === current) opt.selected = true;
        sel.appendChild(opt);
    });
}

async function loadAnalyzedInvoices(page = 0) {
    _analyzedPage = page;
    const category = document.getElementById('analysisCategoryFilter')?.value || '';
    const q = document.getElementById('analysisTextSearch')?.value.trim() || '';
    const offset = page * _analyzedPageSize;
    let url = `/api/analysis/invoices?limit=${_analyzedPageSize}&offset=${offset}`;
    if (category) url += `&category=${encodeURIComponent(category)}`;
    if (q)        url += `&q=${encodeURIComponent(q)}`;

    const tbody = document.getElementById('analyzedInvoicesBody');
    tbody.innerHTML = '<tr><td colspan="8" style="text-align:center;color:var(--text-gray);padding:1rem">Cargando...</td></tr>';

    try {
        const rows = await fetch(url).then(r => r.json());
        if (!rows.length) {
            tbody.innerHTML = '<tr><td colspan="8" style="text-align:center;color:var(--text-gray);padding:2rem">Sin facturas categorizadas aún. Ejecuta el análisis.</td></tr>';
            document.getElementById('analyzedInvoicesPager').innerHTML = '';
            return;
        }

        const methodIcon = m => m === 'rules' ? '⚡' : '🤖';
        const confidenceClass = c => c === 'high' ? 'badge-paid' : c === 'medium' ? 'badge-pending' : '';
        const fmt = amt => amt != null ? `${parseFloat(amt).toFixed(2)} €` : '—';
        const fmtDate = d => {
            if (!d) return '—';
            // d may be a Unix timestamp (number) or an ISO string
            const dt = typeof d === 'number' ? new Date(d * 1000) : new Date(d);
            return isNaN(dt) ? String(d).substring(0, 10) : dt.toLocaleDateString('es-ES');
        };

        tbody.innerHTML = rows.map(r => `
            <tr>
                <td style="white-space:nowrap">${fmtDate(r.date)}</td>
                <td style="max-width:160px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="${r.contact_name || ''}">${r.contact_name || '—'}</td>
                <td style="max-width:200px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="${r.desc || r.reasoning || ''}">${r.desc || r.reasoning || '—'}</td>
                <td style="text-align:right;font-weight:600">${fmt(r.amount)}</td>
                <td><span class="badge ${confidenceClass(r.confidence)}" style="font-size:0.75rem">${r.category || '—'}</span></td>
                <td style="font-size:0.85rem;color:var(--text-gray)">${r.subcategory || '—'}</td>
                <td title="${r.method === 'rules' ? 'Regla automática' : 'Claude AI'}">${methodIcon(r.method)} ${r.method === 'rules' ? 'Reglas' : 'Claude'}</td>
                <td><span class="badge ${confidenceClass(r.confidence)}" style="font-size:0.7rem">${r.confidence || '—'}</span></td>
            </tr>`).join('');

        // Pager
        const pager = document.getElementById('analyzedInvoicesPager');
        const hasPrev = page > 0;
        const hasNext = rows.length === _analyzedPageSize;
        pager.innerHTML = `
            <span style="color:var(--text-gray);font-size:0.85rem">Página ${page + 1} · mostrando ${rows.length} facturas</span>
            ${hasPrev ? `<button class="action-btn btn-secondary" style="margin-left:0.5rem" onclick="loadAnalyzedInvoices(${page - 1})">← Anterior</button>` : ''}
            ${hasNext ? `<button class="action-btn btn-secondary" style="margin-left:0.5rem" onclick="loadAnalyzedInvoices(${page + 1})">Siguiente →</button>` : ''}
        `;
    } catch (e) {
        tbody.innerHTML = `<tr><td colspan="8" style="color:var(--danger);padding:1rem">Error: ${e.message}</td></tr>`;
    }
}

let _analysisSearchTimer = null;
function debounceAnalysisSearch() {
    clearTimeout(_analysisSearchTimer);
    // Reset to page 0 on new search; 350ms debounce avoids spamming the server
    _analysisSearchTimer = setTimeout(() => loadAnalyzedInvoices(0), 350);
}

function _renderAnalysisStatus(status) {
    document.getElementById('analysisTotalInvoices').textContent = status.total ?? '—';
    document.getElementById('analysisAnalyzed').textContent = status.analyzed ?? '—';
    document.getElementById('analysisPending').textContent = status.pending ?? '—';
    document.getElementById('analysisPct').textContent = `${status.pct ?? 0}%`;
    document.getElementById('analysisProgressBar').style.width = `${status.pct ?? 0}%`;

    const btn = document.getElementById('analysisRunBtn');
    const msgEl = document.getElementById('analysisStatusMsg');

    if (status.running) {
        btn.textContent = '⏳ Analizando...';
        btn.disabled = true;
        msgEl.textContent = 'Job en curso — la página se actualizará al terminar.';
        if (!_analysisPolling) {
            _analysisPolling = setInterval(async () => {
                const r = await fetch('/api/analysis/status');
                const s = await r.json();
                _renderAnalysisStatus(s);
                if (!s.running) {
                    clearInterval(_analysisPolling);
                    _analysisPolling = null;
                    loadAnalysisView(); // Full refresh when done
                }
            }, 3000);
        }
    } else {
        btn.textContent = '▶ Analizar';
        btn.disabled = (status.pending === 0);
        // SQLite returns "YYYY-MM-DD HH:MM:SS" (space separator); normalize to ISO for cross-browser parsing
        const lastRunStr = status.last_run ? status.last_run.replace(' ', 'T') : null;
        const lastRun = lastRunStr ? new Date(lastRunStr).toLocaleString('es-ES') : 'Nunca';
        const pendingMatches = status.pending_matches || 0;
        msgEl.innerHTML = `Último análisis: <strong>${lastRun}</strong>${pendingMatches ? ` · <span style="color:var(--warning)">⚠ ${pendingMatches} match(es) pendiente(s) de confirmar</span>` : ''}`;
    }

    // Matches badge
    const badge = document.getElementById('matchesBadge');
    const pm = status.pending_matches || 0;
    if (pm > 0) {
        badge.textContent = `${pm} pendiente${pm > 1 ? 's' : ''}`;
        badge.style.display = 'inline';
    } else {
        badge.style.display = 'none';
    }
}

function _renderMatches(matches) {
    const tbody = document.getElementById('matchesBody');
    if (!matches.length) {
        tbody.innerHTML = '<tr><td colspan="8" style="text-align:center;color:var(--text-gray);padding:2rem">No hay matches pendientes. Ejecuta el análisis para escanear tus facturas de compra.</td></tr>';
        return;
    }

    const methodLabel = m => {
        if (m === 'exact_id') return '<span class="badge badge-paid" style="font-size:0.75rem">✅ ID exacto</span>';
        if (m && m.startsWith('fuzzy')) return `<span class="badge badge-pending" style="font-size:0.75rem">🔍 ${m.replace('fuzzy_','').replace('pct','%')} similitud</span>`;
        return m || '—';
    };

    tbody.innerHTML = matches.map(m => {
        const safeName = (m.product_name || '').replace(/'/g, "\\'");
        return `
        <tr id="match-row-${m.id}">
            <td><strong>${escapeHtml(m.product_name)}</strong></td>
            <td style="color:var(--text-gray);font-size:0.85rem">${escapeHtml(m.item_name_in_invoice || m.product_name)}</td>
            <td>${escapeHtml(m.supplier || '—')}</td>
            <td style="font-weight:600;color:var(--primary)">${formatter.format(m.matched_price)}</td>
            <td>${escapeHtml(m.matched_date || '—')}</td>
            <td>${methodLabel(m.match_method)}</td>
            <td>
                <button class="action-btn btn-secondary" style="padding:0.3rem 0.6rem;font-size:0.8rem"
                    onclick="openMatchDetail(${m.id}, '${escapeHtml(m.purchase_id)}', '${safeName}', ${m.matched_price}, '${escapeHtml(m.matched_date || '')}', '${escapeHtml(m.supplier || '')}')">
                    🧾 Ver
                </button>
            </td>
            <td style="white-space:nowrap">
                <button class="action-btn" style="padding:0.3rem 0.7rem;font-size:0.8rem;margin-right:4px"
                    onclick="confirmMatch(${m.id}, true)">✅ Confirmar</button>
                <button class="action-btn" style="padding:0.3rem 0.7rem;font-size:0.8rem;background:var(--danger)"
                    onclick="confirmMatch(${m.id}, false)">✕ Rechazar</button>
            </td>
        </tr>`;
    }).join('');
    liveSearch('matchesSearch', 'matchesBody');
}

// ── Match Detail Modal ──────────────────────────────────────────────

let _currentMatchId = null;

async function openMatchDetail(matchId, purchaseId, productName, detectedPrice, date, supplier) {
    _currentMatchId = matchId;
    document.getElementById('matchDetailModal').style.display = 'flex';
    document.getElementById('matchDetailTitle').textContent = `🧾 Factura: ${productName}`;
    document.getElementById('matchDetailPrice').value = detectedPrice;
    document.getElementById('matchDetailError').textContent = '';
    document.getElementById('matchDetailItems').innerHTML =
        '<tr><td colspan="4" style="text-align:center;color:var(--text-gray);padding:1rem">Cargando items...</td></tr>';

    // Summary
    document.getElementById('matchDetailSummary').innerHTML = `
        <div style="display:flex;gap:2rem;flex-wrap:wrap">
            <span>📦 <strong>${escapeHtml(productName)}</strong></span>
            <span>🏢 ${escapeHtml(supplier || '—')}</span>
            <span>📅 ${escapeHtml(date || '—')}</span>
            <span style="color:var(--primary)">💰 Detectado: <strong>${formatter.format(detectedPrice)}</strong></span>
        </div>`;

    // Try to load PDF in a new tab first; simultaneously load line items
    const pdfBtn = `<button class="action-btn btn-secondary" style="margin-top:0.5rem;font-size:0.82rem"
        onclick="openPdfModal('purchases','${escapeHtml(purchaseId)}')">📄 Abrir PDF en visor</button>`;
    document.getElementById('matchDetailSummary').innerHTML += `<div style="margin-top:0.5rem">${pdfBtn}</div>`;

    // Load line items from Holded
    try {
        const res = await fetch(`/api/entities/purchases/${purchaseId}/items`);
        const items = await res.json();
        if (!items.length) {
            document.getElementById('matchDetailItems').innerHTML =
                '<tr><td colspan="4" style="text-align:center;color:var(--text-gray);padding:1rem">Sin items disponibles en esta factura.</td></tr>';
            return;
        }
        document.getElementById('matchDetailItems').innerHTML = items.map(it => `
            <tr>
                <td>${escapeHtml(it.name || '—')}</td>
                <td style="text-align:center">${it.units ?? '—'}</td>
                <td>${formatter.format(it.price ?? 0)}</td>
                <td style="font-weight:600;color:var(--primary)">${formatter.format(it.subtotal ?? 0)}</td>
            </tr>
        `).join('');
    } catch (e) {
        document.getElementById('matchDetailItems').innerHTML =
            `<tr><td colspan="4" style="text-align:center;color:var(--danger);padding:1rem">Error cargando items: ${e.message}</td></tr>`;
    }
}

function closeMatchDetail() {
    document.getElementById('matchDetailModal').style.display = 'none';
    _currentMatchId = null;
}

async function confirmMatchFromDetail(confirmed) {
    if (!_currentMatchId) return;
    const price = parseFloat(document.getElementById('matchDetailPrice').value);
    const errorEl = document.getElementById('matchDetailError');
    errorEl.textContent = '';

    if (confirmed && (isNaN(price) || price <= 0)) {
        errorEl.textContent = 'Introduce un importe válido.';
        return;
    }

    // If price was changed, update it on the match first
    const row = document.getElementById(`match-row-${_currentMatchId}`);
    closeMatchDetail();
    await confirmMatch(_currentMatchId, confirmed, confirmed ? price : null);
}

// confirmMatch (defined below) accepts optional customPrice from the detail modal

function _renderCategoryBreakdown(categories) {
    const el = document.getElementById('categoryBreakdown');
    if (!categories.length) {
        el.innerHTML = '<p style="color:var(--text-gray);text-align:center;padding:1rem">Sin datos aún. Ejecuta el análisis para ver el desglose.</p>';
        return;
    }
    const maxAmount = Math.max(...categories.map(c => c.total_amount || 0));
    el.innerHTML = `<div style="display:flex;flex-direction:column;gap:0.75rem">` +
        categories.map(c => {
            const pct = maxAmount > 0 ? Math.round((c.total_amount / maxAmount) * 100) : 0;
            return `
            <div>
                <div style="display:flex;justify-content:space-between;margin-bottom:0.3rem;font-size:0.9rem">
                    <span><strong>${escapeHtml(c.category)}</strong> <span style="color:var(--text-gray)">(${c.count} facturas)</span></span>
                    <span style="color:var(--primary);font-weight:600">${formatter.format(c.total_amount || 0)}</span>
                </div>
                <div style="background:rgba(255,255,255,0.05);border-radius:4px;height:6px">
                    <div style="width:${pct}%;height:100%;background:var(--primary);border-radius:4px;transition:width 0.5s"></div>
                </div>
            </div>`;
        }).join('') + `</div>`;
}

async function runAnalysisJob() {
    const btn = document.getElementById('analysisRunBtn');
    const batchSize = parseInt(document.getElementById('analysisBatchSize')?.value || '10');
    btn.disabled = true;
    btn.textContent = '⏳ Iniciando...';
    try {
        const res = await fetch(`/api/analysis/run?batch_size=${batchSize}`, { method: 'POST' });
        const data = await res.json();
        if (data.status === 'started' || data.status === 'already_running') {
            loadAnalysisView();
        }
    } catch (e) {
        btn.disabled = false;
        btn.textContent = '▶ Analizar';
        alert(`Error: ${e.message}`);
    }
}

async function confirmMatch(matchId, confirmed, customPrice = null) {
    const row = document.getElementById(`match-row-${matchId}`);
    if (row) {
        row.style.opacity = '0.5';
        row.querySelectorAll('button').forEach(b => b.disabled = true);
    }
    try {
        const body = { confirmed };
        if (customPrice !== null && !isNaN(customPrice)) body.custom_price = customPrice;

        const res = await fetch(`/api/analysis/matches/${matchId}/confirm`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body)
        });
        const data = await res.json();
        if (res.ok) {
            if (row) row.remove();
            if (confirmed && data.added_to_amortizations) {
                const usedPrice = customPrice ? ` con importe manual ${formatter.format(customPrice)}` : '';
                const msg = document.getElementById('analysisStatusMsg');
                const prev = msg.innerHTML;
                msg.innerHTML = `<span style="color:var(--primary)">✅ Añadido a Amortizaciones${usedPrice}</span>`;
                setTimeout(() => { msg.innerHTML = prev; }, 4000);
            }
            // Refresh status counts
            const r = await fetch('/api/analysis/status');
            const s = await r.json();
            _renderAnalysisStatus(s);
        } else {
            if (row) { row.style.opacity = '1'; row.querySelectorAll('button').forEach(b => b.disabled = false); }
            alert(data.detail || 'Error al confirmar');
        }
    } catch (e) {
        if (row) { row.style.opacity = '1'; row.querySelectorAll('button').forEach(b => b.disabled = false); }
        alert(`Error: ${e.message}`);
    }
}

// ── Column configurator context menu (Feature 2) ─────────────────────────────
var _colMenuEl = null;
function openColMenu(e, entity, allKeys, currentKeys, defaultHidden, onUpdate) {
    // Remove any existing menu
    if (_colMenuEl && _colMenuEl.parentNode) _colMenuEl.parentNode.removeChild(_colMenuEl);

    const menu = document.createElement('div');
    menu.className = 'col-config-menu';
    menu.style.cssText = 'position:fixed;z-index:99999;left:'+e.clientX+'px;top:'+e.clientY+'px';
    _colMenuEl = menu;

    // Title
    const title = document.createElement('div');
    title.className = 'col-config-title';
    title.textContent = 'Columnas visibles';
    menu.appendChild(title);

    // Checkboxes
    const visibleSet = new Set(currentKeys);
    allKeys.forEach(function(k) {
        const row = document.createElement('label');
        row.className = 'col-config-row';
        const cb = document.createElement('input');
        cb.type = 'checkbox';
        cb.checked = visibleSet.has(k);
        cb.dataset.key = k;
        const lbl = document.createElement('span');
        lbl.textContent = k.replace(/_/g, ' ');
        if (defaultHidden.has(k)) lbl.style.opacity = '.5';
        row.appendChild(cb); row.appendChild(lbl);
        menu.appendChild(row);
    });

    // Reset button
    const resetBtn = document.createElement('div');
    resetBtn.className = 'col-config-reset';
    resetBtn.textContent = 'Restablecer por defecto';
    resetBtn.addEventListener('click', function() {
        localStorage.removeItem('col_config_'+entity);
        closeMenu();
        onUpdate(allKeys.filter(function(k){ return !defaultHidden.has(k); }));
    });
    menu.appendChild(resetBtn);

    // Apply on checkbox change
    menu.addEventListener('change', function() {
        var newKeys = allKeys.filter(function(k){
            var cb = menu.querySelector('[data-key="'+k+'"]');
            return cb && cb.checked;
        });
        if (newKeys.length === 0) return; // prevent all hidden
        closeMenu();
        onUpdate(newKeys);
    });

    document.body.appendChild(menu);

    // Close on outside click
    function closeMenu() {
        if (menu.parentNode) menu.parentNode.removeChild(menu);
        document.removeEventListener('click', outsideClick);
    }
    function outsideClick(ev) {
        if (!menu.contains(ev.target)) closeMenu();
    }
    setTimeout(function(){ document.addEventListener('click', outsideClick); }, 10);
}


// ── Backup view ───────────────────────────────────────────────────────────────

async function loadBackupView() {
    const statusEl = document.getElementById('backupStatus');
    if (!statusEl) return;
    statusEl.innerHTML = '<span style="opacity:.6">Cargando información…</span>';
    try {
        const res = await fetch('/api/backup/status');
        const d = await res.json();

        // Build commit line
        const commitLine = d.last_commit
            ? `<strong>${d.last_commit.hash}</strong> · ${d.last_commit.message} · <span style="opacity:.7">${d.last_commit.date.slice(0,10)}</span>`
            : '<span style="opacity:.6">Sin commits encontrados</span>';

        // Build record table
        const counts = d.record_counts || {};
        const rows = Object.entries(counts).map(([tbl, n]) =>
            `<span style="color:var(--text-light)">${tbl}</span><span style="text-align:right">${n.toLocaleString()}</span>`
        ).join('');

        statusEl.innerHTML = `
            <div style="display:grid;grid-template-columns:1fr 1fr;gap:.4rem .75rem;align-items:center;margin-bottom:.75rem">
                <span>📁 Base de datos</span><span><strong>${d.db_size_mb} MB</strong></span>
                <span>📝 Último commit</span><span>${commitLine}</span>
            </div>
            <div style="border-top:1px solid var(--border-color);padding-top:.6rem;display:grid;grid-template-columns:repeat(auto-fill,minmax(180px,1fr));gap:.25rem .75rem">
                ${rows}
            </div>
        `;
    } catch (e) {
        statusEl.innerHTML = `<span style="color:#f87171">Error cargando estado: ${e.message}</span>`;
    }
}

function downloadBackup(type) {
    // type: 'db' | 'data' | 'code'
    const urls = {
        db:   '/api/backup/db',
        data: '/api/backup/data',
        code: '/api/backup/code',
    };
    const url = urls[type];
    if (!url) return;
    // Create a hidden <a> and click it — triggers browser Save dialog
    const a = document.createElement('a');
    a.href = url;
    a.style.display = 'none';
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
}

// ── Amortization purchase-link detail panel ───────────────────────────────────

var _amortDetailOpen = null;

async function toggleAmortDetail(amortId) {
    const detailRow  = document.getElementById('amort-detail-' + amortId);
    const expandIcon = document.getElementById('amort-expand-' + amortId);
    if (!detailRow) return;

    const isOpen = detailRow.style.display !== 'none';
    if (isOpen) {
        detailRow.style.display = 'none';
        if (expandIcon) expandIcon.textContent = '▶';
        _amortDetailOpen = null;
        return;
    }
    if (_amortDetailOpen && _amortDetailOpen !== amortId) {
        var prev = document.getElementById('amort-detail-' + _amortDetailOpen);
        var prevIcon = document.getElementById('amort-expand-' + _amortDetailOpen);
        if (prev) prev.style.display = 'none';
        if (prevIcon) prevIcon.textContent = '▶';
    }
    detailRow.style.display = '';
    if (expandIcon) expandIcon.textContent = '▼';
    _amortDetailOpen = amortId;
    await renderAmortDetail(amortId);
}

async function renderAmortDetail(amortId) {
    const cell = document.getElementById('amort-detail-cell-' + amortId);
    if (!cell) return;
    while (cell.firstChild) cell.removeChild(cell.firstChild);

    var loading = document.createElement('span');
    loading.style.cssText = 'color:var(--text-gray);font-size:.82rem';
    loading.textContent = 'Cargando…';
    cell.appendChild(loading);

    try {
        const res   = await fetch('/api/amortizations/' + amortId + '/purchases');
        const links = await res.json();
        while (cell.firstChild) cell.removeChild(cell.firstChild);

        var wrap = document.createElement('div');
        wrap.className = 'amort-detail-wrap';

        var titleEl = document.createElement('div');
        titleEl.className = 'amort-detail-title';
        titleEl.textContent = '💰 Fuentes de coste';
        wrap.appendChild(titleEl);

        if (links.length === 0) {
            var emptyEl = document.createElement('p');
            emptyEl.style.cssText = 'color:var(--text-gray);font-size:.82rem;margin:.5rem 0 1rem';
            emptyEl.textContent = 'Sin fuentes de coste registradas. Añade una abajo.';
            wrap.appendChild(emptyEl);
        } else {
            var tbl = document.createElement('table');
            tbl.className = 'amort-links-table';
            var thead = document.createElement('thead');
            thead.innerHTML = '<tr><th>Proveedor / Factura</th><th>Item detectado</th><th>Nota de asignación</th><th style="text-align:right">Coste asignado</th><th></th></tr>';
            tbl.appendChild(thead);
            var ltbody = document.createElement('tbody');

            links.forEach(function(lnk) {
                var tr = document.createElement('tr');
                var supplier = lnk.supplier || lnk.invoice_desc || '—';
                var docRef   = lnk.doc_number ? ' #' + lnk.doc_number : '';
                var dateStr  = lnk.invoice_date ? new Date(lnk.invoice_date * 1000).toLocaleDateString('es-ES') : '';
                var itemName = lnk.item_name || '—';
                var note     = lnk.allocation_note || '—';

                tr.innerHTML =
                    '<td style="font-size:.82rem"><strong>' + escapeHtml(supplier) + escapeHtml(docRef) + '</strong>' +
                        (dateStr ? '<br><span style="color:var(--text-gray)">' + escapeHtml(dateStr) + '</span>' : '') + '</td>' +
                    '<td style="font-size:.82rem;color:var(--text-gray)">' + escapeHtml(itemName) + '</td>' +
                    '<td style="font-size:.82rem">' + escapeHtml(note) + '</td>' +
                    '<td style="text-align:right;font-weight:600" id="amort-link-cost-' + lnk.id + '">' + formatter.format(lnk.cost_override) + '</td>' +
                    '<td style="white-space:nowrap">' +
                        '<button class="action-btn btn-secondary" style="padding:.2rem .5rem;font-size:.78rem" onclick="editAmortLink(' + lnk.id + ',' + amortId + ')">✎</button> ' +
                        '<button class="action-btn" style="padding:.2rem .5rem;font-size:.78rem;background:var(--danger)" onclick="deleteAmortLink(' + lnk.id + ',' + amortId + ')">✕</button>' +
                    '</td>';
                ltbody.appendChild(tr);
            });
            tbl.appendChild(ltbody);
            wrap.appendChild(tbl);
        }

        // Add-link form
        var addWrap = document.createElement('div');
        addWrap.className = 'amort-add-link';

        var addLabel = document.createElement('strong');
        addLabel.style.cssText = 'font-size:.82rem;color:var(--text-gray)';
        addLabel.textContent = 'Añadir fuente de coste:';

        // "+ Factura" button — opens purchase picker
        var pickBtn = document.createElement('button');
        pickBtn.className = 'btn-primary';
        pickBtn.style.cssText = 'padding:.3rem .7rem;font-size:.82rem;display:flex;align-items:center;gap:.3rem';
        pickBtn.innerHTML = '+ Factura';
        pickBtn.title = 'Buscar y vincular factura de compra';
        pickBtn.addEventListener('click', function(){ openPurchasePicker(amortId); });

        var costInput = document.createElement('input');
        costInput.id = 'amort-new-cost-' + amortId;
        costInput.type = 'number'; costInput.step = '0.01'; costInput.placeholder = 'Importe €';
        costInput.style.cssText = 'width:100px;padding:.3rem .5rem;background:var(--bg-card);border:1px solid var(--border-color);border-radius:6px;color:var(--text-light)';

        var noteInput = document.createElement('input');
        noteInput.id = 'amort-new-note-' + amortId;
        noteInput.type = 'text'; noteInput.placeholder = 'Nota (ej: 1/3 del pack)';
        noteInput.style.cssText = 'flex:1;padding:.3rem .5rem;background:var(--bg-card);border:1px solid var(--border-color);border-radius:6px;color:var(--text-light)';

        var addBtn = document.createElement('button');
        addBtn.className = 'action-btn btn-secondary';
        addBtn.style.cssText = 'padding:.3rem .8rem;font-size:.82rem';
        addBtn.textContent = 'Añadir manual';
        addBtn.addEventListener('click', function(){ addAmortLink(amortId); });

        addWrap.appendChild(addLabel);
        addWrap.appendChild(pickBtn);
        addWrap.appendChild(costInput);
        addWrap.appendChild(noteInput);
        addWrap.appendChild(addBtn);
        wrap.appendChild(addWrap);

        cell.appendChild(wrap);
    } catch (e) {
        while (cell.firstChild) cell.removeChild(cell.firstChild);
        var errEl = document.createElement('span');
        errEl.style.color = 'var(--danger)';
        errEl.textContent = 'Error: ' + e.message;
        cell.appendChild(errEl);
    }
}

async function addAmortLink(amortId) {
    const costEl = document.getElementById('amort-new-cost-' + amortId);
    const noteEl = document.getElementById('amort-new-note-' + amortId);
    const cost   = parseFloat(costEl ? costEl.value : 0);
    if (!cost || cost <= 0) { alert('Introduce un importe válido'); return; }

    const res = await fetch('/api/amortizations/' + amortId + '/purchases', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ cost_override: cost, allocation_note: noteEl ? noteEl.value : '' })
    });
    if (res.ok) {
        if (costEl) costEl.value = '';
        if (noteEl) noteEl.value = '';
        await refreshAmortRow(amortId);
        await renderAmortDetail(amortId);
    } else { alert('Error al añadir la fuente de coste'); }
}

async function editAmortLink(linkId, amortId) {
    const newCostStr = prompt('Nuevo importe para esta fuente de coste (€):');
    if (!newCostStr) return;
    const newCost = parseFloat(newCostStr);
    if (!newCost || newCost <= 0) { alert('Importe no válido'); return; }
    const res = await fetch('/api/amortizations/purchases/' + linkId, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ cost_override: newCost })
    });
    if (res.ok) {
        await refreshAmortRow(amortId);
        await renderAmortDetail(amortId);
    }
}

async function deleteAmortLink(linkId, amortId) {
    if (!confirm('¿Eliminar esta fuente de coste?')) return;
    const res = await fetch('/api/amortizations/purchases/' + linkId, { method: 'DELETE' });
    if (res.ok) {
        await refreshAmortRow(amortId);
        await renderAmortDetail(amortId);
    }
}

async function refreshAmortRow(amortId) {
    try {
        const res  = await fetch('/api/amortizations');
        const rows = await res.json();
        const r    = rows.find(function(x){ return x.id === amortId; });
        if (r) {
            const cc = document.getElementById('amort-cost-' + r.id);
            if (cc) cc.innerHTML = '<strong>' + formatter.format(r.purchase_price) + '</strong>';
        }
    } catch (e) { /* silently ignore */ }
}

// ============================================================
//  PURCHASE COST-SOURCE PICKER
// ============================================================

let _pickerAmortId = null;
let _pickerDebounceTimer = null;

function openPurchasePicker(amortId) {
    _pickerAmortId = amortId;
    document.getElementById('purchasePickerSearch').value = '';
    document.getElementById('purchasePickerResults').innerHTML =
        '<p style="color:var(--text-gray);font-size:.85rem;text-align:center;padding:2rem 0">Escribe para buscar…</p>';
    document.getElementById('purchasePickerModal').style.display = 'flex';
    document.getElementById('purchasePickerSearch').focus();
}

function closePurchasePicker() {
    document.getElementById('purchasePickerModal').style.display = 'none';
    _pickerAmortId = null;
}

function debouncePurchaseSearch(q) {
    clearTimeout(_pickerDebounceTimer);
    _pickerDebounceTimer = setTimeout(() => runPurchaseSearch(q), 280);
}

async function runPurchaseSearch(q) {
    if (!q || q.trim().length < 2) {
        document.getElementById('purchasePickerResults').innerHTML =
            '<p style="color:var(--text-gray);font-size:.85rem;text-align:center;padding:2rem 0">Escribe al menos 2 caracteres…</p>';
        return;
    }
    const container = document.getElementById('purchasePickerResults');
    container.innerHTML = '<p style="color:var(--text-gray);font-size:.85rem;text-align:center;padding:1rem 0">Buscando…</p>';
    try {
        const res  = await fetch('/api/purchases/search?q=' + encodeURIComponent(q) + '&limit=30');
        const invoices = await res.json();

        if (!invoices.length) {
            container.innerHTML = '<p style="color:var(--text-gray);font-size:.85rem;text-align:center;padding:2rem 0">Sin resultados</p>';
            return;
        }

        container.innerHTML = '';
        invoices.forEach(inv => {
            const dateStr = inv.date ? new Date(inv.date * 1000).toLocaleDateString('es-ES') : '—';

            // Invoice header card
            const card = document.createElement('div');
            card.className = 'picker-invoice-card';

            const header = document.createElement('div');
            header.className = 'picker-invoice-header';
            header.innerHTML =
                `<span style="font-weight:600">${escapeHtml(inv.supplier)}</span>` +
                `<span style="color:var(--text-gray);font-size:.8rem">${escapeHtml(inv.desc || '')}</span>` +
                `<span style="color:var(--text-gray);font-size:.8rem">${dateStr}</span>` +
                `<span style="font-weight:700">${formatter.format(inv.amount)}</span>`;

            // "Link whole invoice" button (no specific item)
            const wholeBtn = document.createElement('button');
            wholeBtn.className = 'action-btn btn-secondary';
            wholeBtn.style.cssText = 'font-size:.78rem;padding:.2rem .55rem;margin-left:auto';
            wholeBtn.textContent = '+ Total factura';
            wholeBtn.addEventListener('click', () => {
                selectPurchaseLink(inv.id, null, inv.amount, inv.supplier + (inv.desc ? ' — ' + inv.desc : ''));
            });
            header.appendChild(wholeBtn);
            card.appendChild(header);

            // Line items (if any)
            if (inv.items && inv.items.length) {
                const itemsWrap = document.createElement('div');
                itemsWrap.className = 'picker-items-list';
                inv.items.forEach(it => {
                    const row = document.createElement('div');
                    row.className = 'picker-item-row';
                    const itemTotal = (it.units || 1) * (it.price || 0);
                    row.innerHTML =
                        `<span style="flex:1;font-size:.82rem">${escapeHtml(it.name || '—')}</span>` +
                        `<span style="color:var(--text-gray);font-size:.78rem">${it.units ?? 1} × ${formatter.format(it.price ?? 0)}</span>` +
                        `<span style="font-weight:600;font-size:.82rem;min-width:70px;text-align:right">${formatter.format(itemTotal)}</span>`;
                    const itemBtn = document.createElement('button');
                    itemBtn.className = 'action-btn btn-secondary';
                    itemBtn.style.cssText = 'font-size:.75rem;padding:.15rem .45rem';
                    itemBtn.textContent = '+ Item';
                    itemBtn.addEventListener('click', () => {
                        selectPurchaseLink(inv.id, it.id, itemTotal, it.name);
                    });
                    row.appendChild(itemBtn);
                    itemsWrap.appendChild(row);
                });
                card.appendChild(itemsWrap);
            }

            container.appendChild(card);
        });
    } catch (e) {
        container.innerHTML = `<p style="color:var(--danger);font-size:.85rem;text-align:center;padding:1rem">Error: ${e.message}</p>`;
    }
}

async function selectPurchaseLink(purchaseId, itemId, suggestedCost, label) {
    if (!_pickerAmortId) return;
    const amortId = _pickerAmortId;

    // Pre-fill the cost input with the suggested value, let user confirm
    const costEl = document.getElementById('amort-new-cost-' + amortId);
    const noteEl = document.getElementById('amort-new-note-' + amortId);
    if (costEl) costEl.value = suggestedCost.toFixed(2);
    if (noteEl && !noteEl.value) noteEl.value = label || '';

    // Save directly with the purchase reference
    const res = await fetch('/api/amortizations/' + amortId + '/purchases', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
            cost_override: parseFloat(suggestedCost.toFixed(2)),
            allocation_note: label || '',
            purchase_id: purchaseId,
            purchase_item_id: itemId || null
        })
    });

    if (res.ok) {
        closePurchasePicker();
        await refreshAmortRow(amortId);
        await renderAmortDetail(amortId);
    } else {
        alert('Error al vincular la factura');
    }
}

// ============================================================
//  UNPAID INVOICES AGING
// ============================================================

// ── Unpaid invoices sub-view (inside Invoices) ───────────────────────────────

let _unpaidData = [];
let _activeInvoiceTab = 'all';

function switchInvoiceTab(tab) {
    _activeInvoiceTab = tab;
    document.getElementById('allInvoicesTable').style.display  = tab === 'all'    ? '' : 'none';
    document.getElementById('unpaidInvoicesTable').style.display = tab === 'unpaid' ? '' : 'none';
    document.getElementById('tabAllInvoices').className    = tab === 'all'    ? 'action-btn'           : 'action-btn btn-secondary';
    document.getElementById('tabUnpaidInvoices').className = tab === 'unpaid' ? 'action-btn'           : 'action-btn btn-secondary';
    if (tab === 'unpaid') loadUnpaidView();
}

async function loadUnpaidView() {
    const tbody = document.getElementById('unpaidBody');
    tbody.innerHTML = '<tr><td colspan="6" style="text-align:center;padding:2rem;color:var(--text-gray)">Cargando…</td></tr>';
    try {
        const data = await fetch('/api/invoices/unpaid').then(r => r.json());
        _unpaidData = data;
        renderUnpaidTable(data);
    } catch (e) {
        tbody.innerHTML = `<tr><td colspan="6" style="color:var(--danger);padding:1rem">Error: ${e.message}</td></tr>`;
    }
}

function filterUnpaidTable() {
    const q = (document.getElementById('unpaidSearch')?.value || '').toLowerCase();
    const filtered = q
        ? _unpaidData.filter(r =>
            (r.contact_name || '').toLowerCase().includes(q) ||
            (r.doc_number   || '').toLowerCase().includes(q) ||
            (r.contact_email || '').toLowerCase().includes(q))
        : _unpaidData;
    renderUnpaidTable(filtered);
}

function renderUnpaidTable(rows) {
    const tbody = document.getElementById('unpaidBody');
    const chips = document.getElementById('unpaidSummaryChips');
    const sumOf = arr => arr.reduce((s, r) => s + (r.payments_pending || 0), 0);

    // Summary chips
    const green  = rows.filter(r => (r.days_overdue || 0) <= 0);
    const yellow = rows.filter(r => (r.days_overdue || 0) > 0  && (r.days_overdue || 0) <= 30);
    const red    = rows.filter(r => (r.days_overdue || 0) > 30);
    const total  = sumOf(rows);
    const chip = (label, count, amt, color) => count
        ? `<div style="padding:.4rem .8rem;border-radius:8px;background:${color}22;border:1px solid ${color}44;font-size:.82rem;cursor:default">
               <span style="color:${color};font-weight:700">${label}:</span>
               ${count} facturas · ${formatter.format(amt)}
           </div>` : '';
    chips.innerHTML =
        chip('🟢 En plazo', green.length, sumOf(green), '#10b981') +
        chip('🟡 Atención (1–30d)', yellow.length, sumOf(yellow), '#f59e0b') +
        chip('🔴 Vencida (>30d)', red.length, sumOf(red), '#f43f5e') +
        `<div style="margin-left:auto;font-size:.82rem;font-weight:700;padding:.4rem .8rem">
             Total pendiente: ${formatter.format(total)}
         </div>`;

    if (!rows.length) {
        tbody.innerHTML = '<tr><td colspan="6" style="text-align:center;padding:2rem;color:var(--text-gray)">Sin facturas pendientes 🎉</td></tr>';
        return;
    }

    tbody.innerHTML = rows.map(r => {
        const d = r.days_overdue || 0;
        let color, bg;
        if (d <= 0)       { color = '#10b981'; bg = 'rgba(16,185,129,.05)'; }
        else if (d <= 30) { color = '#f59e0b'; bg = 'rgba(245,158,11,.05)'; }
        else              { color = '#f43f5e'; bg = 'rgba(244,63,94,.05)';  }

        const refTs  = r.due_date || r.date;
        const dateStr = refTs ? new Date(refTs * 1000).toLocaleDateString('es-ES') : '—';
        const label  = r.aging_label || (d <= 0 ? 'En plazo' : d <= 30 ? 'Atención' : 'Vencida');
        const overdueText = d > 0 ? ` · ${d}d` : '';

        // Action buttons
        const emailBtn = r.contact_email
            ? `<button class="action-btn btn-secondary" title="Copiar email: ${escapeHtml(r.contact_email)}"
                   style="font-size:.75rem;padding:.25rem .5rem"
                   onclick="copyUnpaidEmail('${escapeHtml(r.contact_email)}', this)">📧</button>`
            : `<button class="action-btn btn-secondary" title="Sin email registrado"
                   style="font-size:.75rem;padding:.25rem .5rem;opacity:.4" disabled>📧</button>`;

        const pdfBtn = `<button class="action-btn btn-secondary" title="Ver PDF"
                   style="font-size:.75rem;padding:.25rem .5rem"
                   onclick="openPdfModal('invoices','${r.id}')">📄</button>`;

        const holdedBtn = `<a href="https://app.holded.com/sales#open:invoice-${r.id}" target="_blank"
                   class="action-btn btn-secondary" title="Abrir en Holded"
                   style="font-size:.75rem;padding:.25rem .5rem;text-decoration:none">🔗</a>`;

        return `<tr style="background:${bg}">
            <td style="font-weight:600;white-space:nowrap">${escapeHtml(r.doc_number || '—')}</td>
            <td style="max-width:200px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap"
                title="${escapeHtml(r.contact_name || '')}">${escapeHtml(r.contact_name || '—')}</td>
            <td style="white-space:nowrap">${dateStr}</td>
            <td>
                <span style="font-weight:700;color:${color};background:${color}22;padding:.2rem .5rem;border-radius:20px;font-size:.78rem;white-space:nowrap">
                    ${label}${overdueText}
                </span>
            </td>
            <td style="text-align:right;font-weight:700;white-space:nowrap">${formatter.format(r.payments_pending || 0)}</td>
            <td style="white-space:nowrap">
                <div style="display:flex;gap:4px">${emailBtn}${pdfBtn}${holdedBtn}</div>
            </td>
        </tr>`;
    }).join('');
}

async function copyUnpaidEmail(email, btn) {
    try {
        await navigator.clipboard.writeText(email);
        const orig = btn.textContent;
        btn.textContent = '✓';
        btn.style.color = '#10b981';
        setTimeout(() => { btn.textContent = orig; btn.style.color = ''; }, 1500);
    } catch (e) {
        alert(email); // Fallback: show email if clipboard fails
    }
}

// ── Aging widget (dashboard overview) ────────────────────────────────────────

let _agingData = [];

async function loadAgingWidget() {
    try {
        const res = await fetch('/api/invoices/unpaid');
        _agingData = await res.json();

        if (!_agingData.length) {
            document.getElementById('agingWidget').style.display = 'none';
            return;
        }

        // Bucket counts & totals (based on days_overdue from due_date)
        const green  = _agingData.filter(r => (r.days_overdue || 0) <= 0);
        const yellow = _agingData.filter(r => (r.days_overdue || 0) > 0 && (r.days_overdue || 0) <= 30);
        const red    = _agingData.filter(r => (r.days_overdue || 0) > 30);

        const sumOf = arr => arr.reduce((s, r) => s + (r.amount || 0), 0);

        const buckets = document.getElementById('agingBuckets');
        buckets.innerHTML = '';
        [
            { label: 'Pendiente',  sublabel: '≤ 30 días',  items: green,  color: '#10b981', bg: 'rgba(16,185,129,.08)' },
            { label: 'Atención',   sublabel: '31 – 60 días',items: yellow, color: '#f59e0b', bg: 'rgba(245,158,11,.08)'  },
            { label: 'Vencida',    sublabel: '> 60 días',   items: red,    color: '#f43f5e', bg: 'rgba(244,63,94,.08)'   }
        ].forEach(b => {
            if (!b.items.length) return;
            const div = document.createElement('div');
            div.className = 'aging-bucket';
            div.style.cssText = `flex:1;padding:.6rem 1rem;background:${b.bg};border-right:1px solid var(--glass-border);cursor:pointer`;
            div.innerHTML =
                `<div style="font-size:.78rem;color:${b.color};font-weight:700">${b.label} <span style="font-weight:400;opacity:.7">${b.sublabel}</span></div>` +
                `<div style="font-size:1.15rem;font-weight:700;color:${b.color};margin:.1rem 0">${b.items.length} factura${b.items.length>1?'s':''}</div>` +
                `<div style="font-size:.82rem;color:var(--text-gray)">${formatter.format(sumOf(b.items))}</div>`;
            div.addEventListener('click', () => openAgingModal());
            buckets.appendChild(div);
        });

        document.getElementById('agingWidget').style.display = '';
    } catch (e) {
        console.error('Aging widget error:', e);
    }
}

function openAgingModal() {
    document.getElementById('agingModal').style.display = 'flex';
    renderAgingTable();
}

function closeAgingModal() {
    document.getElementById('agingModal').style.display = 'none';
}

function renderAgingTable() {
    const tbody = document.getElementById('agingBody');
    const summaryEl = document.getElementById('agingSummary');

    if (!_agingData.length) {
        tbody.innerHTML = '<tr><td colspan="6" style="text-align:center;padding:2rem;color:var(--text-gray)">No hay facturas pendientes de cobro.</td></tr>';
        return;
    }

    // Summary chips
    const green  = _agingData.filter(r => (r.days_overdue || 0) <= 0);
    const yellow = _agingData.filter(r => (r.days_overdue || 0) > 0 && (r.days_overdue || 0) <= 30);
    const red    = _agingData.filter(r => (r.days_overdue || 0) > 30);
    const total  = _agingData.reduce((s, r) => s + (r.amount || 0), 0);

    const chip = (label, count, amt, color) => count
        ? `<div style="padding:.4rem .8rem;border-radius:8px;background:${color}22;border:1px solid ${color}44;font-size:.82rem">
               <span style="color:${color};font-weight:700">${label}:</span>
               ${count} factura${count>1?'s':''} · ${formatter.format(amt)}
           </div>`
        : '';

    const sumOf = arr => arr.reduce((s, r) => s + (r.amount || 0), 0);
    summaryEl.innerHTML =
        chip('🟢 En plazo', green.length, sumOf(green), '#10b981') +
        chip('🟡 Atención (1–30d)', yellow.length, sumOf(yellow), '#f59e0b') +
        chip('🔴 Vencida (>30d)', red.length, sumOf(red), '#f43f5e') +
        `<div style="margin-left:auto;padding:.4rem .8rem;font-size:.82rem;font-weight:700">Total pendiente: ${formatter.format(total)}</div>`;

    // Table rows — color by days_overdue, label from aging_label
    tbody.innerHTML = _agingData.map(r => {
        // Show due_date as reference date; fall back to invoice date
        const refTs = r.due_date || r.date;
        const dateStr = refTs ? new Date(refTs * 1000).toLocaleDateString('es-ES') : '—';
        const d = r.days_overdue || 0;
        let color, bg;
        if (d <= 0)       { color = '#10b981'; bg = 'rgba(16,185,129,.06)'; }
        else if (d <= 30) { color = '#f59e0b'; bg = 'rgba(245,158,11,.06)'; }
        else              { color = '#f43f5e'; bg = 'rgba(244,63,94,.06)';  }

        // aging_label comes from server: 'Pendiente' / 'Atención' / 'Vencida'
        const agingLabel = r.aging_label || (d <= 0 ? 'Pendiente' : d <= 30 ? 'Atención' : 'Vencida');
        const overdueText = d > 0 ? ` · ${d}d` : '';

        return `<tr style="background:${bg}">
            <td style="font-weight:600">${escapeHtml(r.contact_name || '—')}</td>
            <td style="font-size:.82rem;color:var(--text-gray)">${escapeHtml(r.doc_number || r.desc || '—')}</td>
            <td>${dateStr}</td>
            <td>
                <span style="font-weight:700;color:${color};background:${color}22;padding:.2rem .55rem;border-radius:20px;font-size:.78rem;white-space:nowrap">
                    ${agingLabel}${overdueText}
                </span>
            </td>
            <td style="text-align:right;font-weight:700">${formatter.format(r.payments_pending || r.amount || 0)}</td>
            <td>
                <button class="action-btn btn-secondary" style="font-size:.75rem;padding:.2rem .5rem"
                    onclick="closeAgingModal();openDocumentDetails('invoices','${r.id}')">Ver</button>
            </td>
        </tr>`;
    }).join('');
    liveSearch('agingSearch', 'agingBody');
}
