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

    const targetViewId = (viewName === 'overview') ? 'view-overview' :
        (viewName === 'setup') ? 'view-setup' : 'view-entity';

    const targetView = document.getElementById(targetViewId);
    if (targetView) {
        targetView.classList.add('active');
        targetView.style.display = 'block'; // Force show

        // Handle navbar active state
        const navItem = document.querySelector(`.nav-item[data-view="${viewName}"]`);
        if (navItem) navItem.classList.add('active');

        if (viewName !== 'overview' && viewName !== 'setup') {
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
