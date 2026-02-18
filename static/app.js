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
            0: { label: 'BORRADOR', class: 'badge-draft' },
            1: { label: 'EMITIDA', class: 'badge-pending' },
            2: { label: 'COBRADA PARCIAL', class: 'badge-partial' },
            3: { label: 'COBRADA', class: 'badge-paid' },
            4: { label: 'VENCIDA', class: 'badge-overdue' },
            5: { label: 'ANULADA', class: 'badge-canceled' }
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
    } catch (error) {
        console.error('Error fetching recent activity:', error);
    }
}

document.getElementById('filterBtn').addEventListener('click', () => {
    const startStr = document.getElementById('startDate').value;
    const endStr = document.getElementById('endDate').value;
    if (startStr && endStr) {
        const start = Math.floor(new Date(startStr).getTime() / 1000);
        const end = Math.floor(new Date(endStr).getTime() / 1000);
        fetchStats(start, end);
        renderCharts(start, end);
        fetchRecentActivity(start, end);
    }
});

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

    const specialViews = { 'overview': 'view-overview', 'setup': 'view-setup', 'amortizations': 'view-amortizations', 'analysis': 'view-analysis' };
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
        } else if (!specialViews[viewName]) {
            loadEntityData(viewName);
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

    const dateFilters = document.getElementById('entityDateFilters');
    const isFinancial = ['invoices', 'purchases', 'estimates'].includes(entity);
    dateFilters.classList.toggle('active', isFinancial);

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
    const startDate = document.getElementById('entityStartDate').value;
    const endDate = document.getElementById('entityEndDate').value;

    if (!currentEntityData || currentEntityData.length === 0) {
        tbody.innerHTML = '<tr><td colspan="100" style="text-align:center">No records found.</td></tr>';
        return;
    }

    const keys = Object.keys(currentEntityData[0]);
    const showPdf = ['invoices', 'estimates', 'purchases'].includes(entity);

    // Initial Header Render or Update
    thead.innerHTML = `<tr>${keys.map(k => {
        const isCurrent = currentSort.column === k;
        const sortClass = isCurrent ? `sort-${currentSort.direction}` : '';
        return `<th class="${sortClass}" onclick="handleSort('${entity}', '${k}')">${k.replace(/_/g, ' ').toUpperCase()}</th>`;
    }).join('')}${showPdf ? '<th>ACTIONS</th>' : ''}</tr>`;

    // Filter Data
    let filteredData = currentEntityData.filter(row => {
        // Search filter
        const matchesSearch = keys.some(k => String(row[k]).toLowerCase().includes(searchTerm));
        if (!matchesSearch) return false;

        // Date filter (if applicable and set)
        if (startDate && endDate && row.date) {
            const rowDate = row.date; // already unix epoch
            const startEpoch = new Date(startDate).getTime() / 1000;
            const endEpoch = new Date(endDate).getTime() / 1000 + 86399; // End of day
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

        if (showPdf) {
            const td = document.createElement('td');
            td.innerHTML = `<button class="action-btn" title="View PDF" onclick="event.stopPropagation(); openPdfModal('${escapeHtml(entity)}', '${escapeHtml(row.id)}')">👁️</button>`;
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

document.getElementById('entityStartDate').addEventListener('change', () => {
    const currentView = document.querySelector('.nav-item.active').getAttribute('data-view');
    renderEntityTable(currentView);
});

document.getElementById('entityEndDate').addEventListener('change', () => {
    const currentView = document.querySelector('.nav-item.active').getAttribute('data-view');
    renderEntityTable(currentView);
});

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
        item.addEventListener('click', () => {
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
});

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

async function loadAmortizations() {
    try {
        const [dataRes, summaryRes] = await Promise.all([
            fetch('/api/amortizations'),
            fetch('/api/amortizations/summary')
        ]);
        const rows = await dataRes.json();
        const summary = await summaryRes.json();

        // Update summary cards
        document.getElementById('amortTotalInvested').textContent = formatter.format(summary.total_invested || 0);
        document.getElementById('amortTotalRevenue').textContent = formatter.format(summary.total_revenue || 0);
        const profitEl = document.getElementById('amortTotalProfit');
        profitEl.textContent = formatter.format(summary.total_profit || 0);
        profitEl.style.color = (summary.total_profit >= 0) ? 'var(--primary)' : 'var(--danger)';

        const roiEl = document.getElementById('amortGlobalRoi');
        roiEl.textContent = `${summary.global_roi_pct || 0}%`;
        roiEl.style.color = (summary.global_roi_pct >= 0) ? 'var(--primary)' : 'var(--danger)';

        // Badges
        const badgesEl = document.getElementById('amortBadges');
        badgesEl.innerHTML = `
            <span class="amort-badge badge-paid">✅ Amortizados: ${summary.amortized_count || 0}</span>
            <span class="amort-badge badge-pending">⏳ En curso: ${summary.in_progress_count || 0}</span>
            <span class="amort-badge badge-draft">📦 Total productos: ${summary.total_products || 0}</span>
        `;

        // Render table
        const tbody = document.getElementById('amortBody');
        if (!rows.length) {
            tbody.innerHTML = '<tr><td colspan="10" style="text-align:center;color:var(--text-gray);padding:2rem">Sin datos. Añade un producto para empezar a hacer seguimiento.</td></tr>';
            document.getElementById('amortChartCard').style.display = 'none';
            return;
        }

        tbody.innerHTML = rows.map(r => {
            const isAmort = r.status === 'AMORTIZADO';
            const profitColor = r.profit >= 0 ? 'var(--primary)' : 'var(--danger)';
            const roiColor = r.roi_pct >= 0 ? 'var(--primary)' : 'var(--danger)';
            const statusBadge = isAmort
                ? '<span class="badge badge-paid">✅ AMORTIZADO</span>'
                : '<span class="badge badge-pending">⏳ EN CURSO</span>';
            // Safely encode name for onclick attribute
            const safeName = r.product_name.replace(/'/g, "\\'");

            return `<tr>
                <td><strong>${escapeHtml(r.product_name)}</strong></td>
                <td>${formatter.format(r.purchase_price)}</td>
                <td>${escapeHtml(r.purchase_date)}</td>
                <td>${formatter.format(r.total_revenue)}</td>
                <td style="color:${profitColor};font-weight:600">${formatter.format(r.profit)}</td>
                <td style="color:${roiColor};font-weight:600">${r.roi_pct}%</td>
                <td>${statusBadge}</td>
                <td style="color:var(--text-gray);font-size:0.85rem">${escapeHtml(r.notes || '—')}</td>
                <td>
                    <button class="action-btn btn-secondary" style="padding:0.3rem 0.6rem;font-size:0.8rem"
                        onclick="openAmortHistory('${escapeHtml(r.product_id)}', '${safeName}', ${r.purchase_price}, ${r.total_revenue})">
                        📋 Ver
                    </button>
                </td>
                <td>
                    <button class="action-btn btn-secondary" style="padding:0.3rem 0.6rem;font-size:0.8rem"
                        onclick="openAmortizationModal(${r.id}, '${safeName}', ${r.purchase_price}, '${escapeHtml(r.purchase_date)}', '${escapeHtml(r.notes || '')}')">
                        Editar
                    </button>
                    <button class="action-btn" style="padding:0.3rem 0.6rem;font-size:0.8rem;background:var(--danger);margin-left:4px"
                        onclick="deleteAmortization(${r.id}, '${safeName}')">
                        Eliminar
                    </button>
                </td>
            </tr>`;
        }).join('');

        // Re-render chart if it was visible
        if (_amortChartVisible) renderAmortChart(rows);

    } catch (e) {
        console.error('Error loading amortizations:', e);
        document.getElementById('amortBody').innerHTML =
            `<tr><td colspan="9" style="text-align:center;color:var(--danger);padding:2rem">Error cargando datos: ${e.message}</td></tr>`;
    }
}

async function openAmortizationModal(id = null, name = '', price = '', date = '', notes = '') {
    // Load products into select if not cached
    if (!_amortProducts.length) {
        try {
            const res = await fetch('/api/entities/products');
            _amortProducts = await res.json();
        } catch (e) {
            _amortProducts = [];
        }
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

    // In edit mode, select the right product (disabled — can't change product in edit)
    if (id) {
        document.getElementById('amortModalTitle').textContent = `Editar: ${name}`;
        select.disabled = true;
        // select the matching option
        const opt = [...select.options].find(o => o.text === name);
        if (opt) opt.selected = true;
    } else {
        document.getElementById('amortModalTitle').textContent = 'Añadir producto a seguimiento';
        select.disabled = false;
    }

    document.getElementById('amortModal').style.display = 'flex';
}

function closeAmortizationModal() {
    document.getElementById('amortModal').style.display = 'none';
    document.getElementById('amortProductSelect').disabled = false;
}

async function saveAmortization() {
    const id = document.getElementById('amortEditId').value;
    const select = document.getElementById('amortProductSelect');
    const productId = select.value;
    const productName = select.options[select.selectedIndex]?.dataset?.name || select.options[select.selectedIndex]?.text || '';
    const price = parseFloat(document.getElementById('amortPurchasePrice').value);
    const date = document.getElementById('amortPurchaseDate').value;
    const notes = document.getElementById('amortNotes').value.trim();
    const errorEl = document.getElementById('amortModalError');

    errorEl.textContent = '';

    if (!productId) { errorEl.textContent = 'Selecciona un producto.'; return; }
    if (isNaN(price) || price <= 0) { errorEl.textContent = 'Precio de compra inválido.'; return; }
    if (!date) { errorEl.textContent = 'Fecha de compra requerida.'; return; }

    try {
        let res, data;
        if (id) {
            // Edit mode
            res = await fetch(`/api/amortizations/${id}`, {
                method: 'PUT',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ purchase_price: price, purchase_date: date, notes })
            });
        } else {
            // Create mode
            res = await fetch('/api/amortizations', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ product_id: productId, product_name: productName, purchase_price: price, purchase_date: date, notes })
            });
        }
        data = await res.json();
        if (!res.ok) {
            errorEl.textContent = data.detail || 'Error al guardar';
            return;
        }
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
        '<tr><td colspan="4" style="text-align:center;padding:2rem;color:var(--text-gray)">Cargando...</td></tr>';

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
                '<tr><td colspan="4" style="text-align:center;padding:2rem;color:var(--text-gray)">Sin alquileres registrados en facturas aún.</td></tr>';
            return;
        }

        document.getElementById('amortHistoryBody').innerHTML = rentals.map(h => {
            const date = h.date ? new Date(h.date * 1000).toLocaleDateString('es-ES') : '—';
            return `<tr>
                <td>${date}</td>
                <td style="text-align:center">${h.units ?? '—'}</td>
                <td>${formatter.format(h.price ?? 0)}</td>
                <td style="font-weight:600;color:var(--primary)">${formatter.format(h.subtotal ?? 0)}</td>
            </tr>`;
        }).join('');

    } catch (e) {
        document.getElementById('amortHistoryBody').innerHTML =
            `<tr><td colspan="4" style="text-align:center;color:var(--danger);padding:1rem">Error: ${e.message}</td></tr>`;
    }
}

function closeAmortHistory() {
    document.getElementById('amortHistoryModal').style.display = 'none';
}

// ============================================================
//  INVOICE ANALYSIS
// ============================================================

let _analysisPolling = null;

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
    } catch (e) {
        console.error('Error loading analysis view:', e);
    }
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
        btn.textContent = `▶ Analizar ${status.pending > 0 ? Math.min(status.pending, 10) : 10} facturas`;
        btn.disabled = (status.pending === 0);
        const lastRun = status.last_run ? new Date(status.last_run).toLocaleString('es-ES') : 'Nunca';
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
    btn.disabled = true;
    btn.textContent = '⏳ Iniciando...';
    try {
        const res = await fetch('/api/analysis/run', { method: 'POST' });
        const data = await res.json();
        if (data.status === 'started' || data.status === 'already_running') {
            // Start polling
            loadAnalysisView();
        }
    } catch (e) {
        btn.disabled = false;
        btn.textContent = '▶ Analizar facturas';
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
