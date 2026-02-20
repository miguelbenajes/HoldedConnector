/**
 * hdate.js - Selector de fechas estilo Holded
 * Uso: new HDatePicker('containerId', (range) => { range.start, range.end, range.preset })
 */
(function () {
    'use strict';

    const MONTHS_ES = ['Enero','Febrero','Marzo','Abril','Mayo','Junio',
                       'Julio','Agosto','Septiembre','Octubre','Noviembre','Diciembre'];
    const DAYS_ES = ['L','M','X','J','V','S','D'];

    const PRESETS = [
        { key: 'quarter',   label: 'Trimestre actual' },
        { key: 'year',      label: 'Año actual' },
        { key: 'prev_year', label: 'Año anterior' },
        { key: 'last_week', label: 'Última semana' },
        { key: 'last7',     label: 'Últimos 7 días' },
        { key: 'month',     label: 'Mes actual' },
        { key: 'prev_month',label: 'Mes anterior' },
        { key: 'custom',    label: 'Personalizado…' },
    ];

    function getPresetRange(key) {
        const now = new Date();
        const y = now.getFullYear(), m = now.getMonth();
        function sod(d) { d.setHours(0,0,0,0); return d; }
        function eod(d) { d.setHours(23,59,59,999); return d; }
        switch (key) {
            case 'quarter': {
                const q = Math.floor(m / 3);
                return { start: sod(new Date(y, q*3, 1)), end: eod(new Date(y, q*3+3, 0)) };
            }
            case 'year':      return { start: sod(new Date(y,0,1)), end: eod(new Date(y,11,31)) };
            case 'prev_year': return { start: sod(new Date(y-1,0,1)), end: eod(new Date(y-1,11,31)) };
            case 'last_week': {
                const d = new Date(now); d.setDate(d.getDate()-d.getDay()-7+1);
                const e = new Date(d);   e.setDate(d.getDate()+6);
                return { start: sod(d), end: eod(e) };
            }
            case 'last7':     return { start: sod(new Date(y,m,now.getDate()-6)), end: eod(new Date()) };
            case 'month':     return { start: sod(new Date(y,m,1)), end: eod(new Date(y,m+1,0)) };
            case 'prev_month':return { start: sod(new Date(y,m-1,1)), end: eod(new Date(y,m,0)) };
            default:          return null;
        }
    }

    function fmt(d)      { return d ? d.toLocaleDateString('es-ES',{day:'2-digit',month:'short',year:'numeric'}) : ''; }
    function fmtShort(d) { return d ? d.toLocaleDateString('es-ES',{day:'2-digit',month:'short'}) : ''; }

    function buildCalendar(container, opts) {
        while (container.firstChild) container.removeChild(container.firstChild);

        // Header
        const header = document.createElement('div');
        header.className = 'hcal-header';

        const prevBtn = document.createElement('button');
        prevBtn.className = 'hcal-nav';
        prevBtn.textContent = '‹';
        prevBtn.addEventListener('click', function(e){ e.stopPropagation(); opts.onNavMonth(-1); });

        const titleEl = document.createElement('span');
        titleEl.className = 'hcal-title';
        titleEl.textContent = MONTHS_ES[opts.month] + ' ' + opts.year;

        const nextBtn = document.createElement('button');
        nextBtn.className = 'hcal-nav';
        nextBtn.textContent = '›';
        nextBtn.addEventListener('click', function(e){ e.stopPropagation(); opts.onNavMonth(1); });

        header.appendChild(prevBtn);
        header.appendChild(titleEl);
        header.appendChild(nextBtn);
        container.appendChild(header);

        // Grid
        const grid = document.createElement('div');
        grid.className = 'hcal-grid';

        // Day labels
        DAYS_ES.forEach(function(d) {
            const lbl = document.createElement('div');
            lbl.className = 'hcal-daylabel';
            lbl.textContent = d;
            grid.appendChild(lbl);
        });

        // Offset blanks
        const firstDay = new Date(opts.year, opts.month, 1).getDay();
        const offset   = firstDay === 0 ? 6 : firstDay - 1;
        for (var i = 0; i < offset; i++) {
            const blank = document.createElement('div');
            blank.className = 'hcal-cell hcal-empty';
            grid.appendChild(blank);
        }

        // Days
        const daysInMonth = new Date(opts.year, opts.month+1, 0).getDate();
        const rs = opts.rangeStart, re = opts.rangeEnd;

        for (var d = 1; d <= daysInMonth; d++) {
            const date = new Date(opts.year, opts.month, d);
            const cell = document.createElement('div');
            cell.className = 'hcal-cell';
            cell.textContent = d;

            if (rs && date.toDateString() === rs.toDateString()) cell.classList.add('hcal-range-start');
            if (re && date.toDateString() === re.toDateString()) cell.classList.add('hcal-range-end');
            if (rs && re && date > rs && date < re) cell.classList.add('hcal-in-range');

            (function(dt){
                cell.addEventListener('mouseenter', function(){ if (opts.onHover) opts.onHover(dt); });
                cell.addEventListener('click', function(e){ e.stopPropagation(); opts.onSelect(dt); });
            })(date);
            grid.appendChild(cell);
        }
        container.appendChild(grid);
    }

    // ─── Constructor ──────────────────────────────────────────────────────────
    function HDatePicker(containerId, onChange) {
        const root = document.getElementById(containerId);
        if (!root) return;

        var activePreset = 'quarter';
        var currentStart, currentEnd;
        var customStart = null, customEnd = null;
        var hoverDate = null, pickStep = 0;
        var cal1Year, cal1Month, cal2Year, cal2Month;

        // Init with quarter preset
        var ini = getPresetRange('quarter');
        currentStart = ini.start;
        currentEnd   = ini.end;

        // ── Build static DOM ──────────────────────────────────────────────────
        root.className = 'hdate-picker';

        var trigger = document.createElement('div');
        trigger.className = 'hdate-trigger';

        var iconEl  = document.createElement('span'); iconEl.className  = 'hdate-icon'; iconEl.textContent = '📅';
        var labelEl = document.createElement('span'); labelEl.className = 'hdate-label';
        var caretEl = document.createElement('span'); caretEl.className = 'hdate-caret'; caretEl.textContent = '▾';
        trigger.appendChild(iconEl); trigger.appendChild(labelEl); trigger.appendChild(caretEl);

        var dd = document.createElement('div');
        dd.className = 'hdate-dropdown';
        dd.style.display = 'none';

        // Presets column
        var presetsEl = document.createElement('div');
        presetsEl.className = 'hdate-presets';
        PRESETS.forEach(function(p) {
            var item = document.createElement('div');
            item.className = 'hdate-preset-item';
            item.textContent = p.label;
            item.dataset.key = p.key;
            item.addEventListener('click', function(){ selectPreset(p.key); });
            presetsEl.appendChild(item);
        });

        // Custom panel
        var customPan = document.createElement('div');
        customPan.className = 'hdate-custom-panel';
        customPan.style.display = 'none';

        var calsEl = document.createElement('div');
        calsEl.className = 'hdate-cals';
        var cal1El = document.createElement('div'); cal1El.className = 'hdate-cal';
        var cal2El = document.createElement('div'); cal2El.className = 'hdate-cal';
        calsEl.appendChild(cal1El); calsEl.appendChild(cal2El);

        var footer = document.createElement('div');
        footer.className = 'hdate-custom-footer';

        var rangeLabel = document.createElement('span');
        rangeLabel.className = 'hdate-range-label';

        var applyBtn = document.createElement('button');
        applyBtn.className = 'btn-primary hdate-apply';
        applyBtn.textContent = 'Aplicar';
        applyBtn.addEventListener('click', applyCustom);

        footer.appendChild(rangeLabel); footer.appendChild(applyBtn);
        customPan.appendChild(calsEl); customPan.appendChild(footer);
        dd.appendChild(presetsEl); dd.appendChild(customPan);

        root.appendChild(trigger);
        root.appendChild(dd);

        // ── Calendar nav ──────────────────────────────────────────────────────
        function initCalMonths() {
            var now = new Date();
            cal2Year  = now.getFullYear(); cal2Month = now.getMonth();
            cal1Month = cal2Month - 1; cal1Year = cal2Year;
            if (cal1Month < 0) { cal1Month = 11; cal1Year--; }
        }
        initCalMonths();

        function navCal(which, dir) {
            if (which === 1) {
                cal1Month += dir;
                if (cal1Month > 11){ cal1Month = 0;  cal1Year++; }
                if (cal1Month < 0) { cal1Month = 11; cal1Year--; }
            } else {
                cal2Month += dir;
                if (cal2Month > 11){ cal2Month = 0;  cal2Year++; }
                if (cal2Month < 0) { cal2Month = 11; cal2Year--; }
            }
            renderCals();
        }

        function renderCals() {
            var rs = customStart;
            var re = customEnd || (pickStep === 2 && hoverDate ? hoverDate : null);
            buildCalendar(cal1El, { year:cal1Year, month:cal1Month, rangeStart:rs, rangeEnd:re,
                onSelect:onCellClick, onHover:onHover, onNavMonth:function(d){ navCal(1,d); } });
            buildCalendar(cal2El, { year:cal2Year, month:cal2Month, rangeStart:rs, rangeEnd:re,
                onSelect:onCellClick, onHover:onHover, onNavMonth:function(d){ navCal(2,d); } });
        }

        function onHover(date) {
            hoverDate = date;
            if (pickStep === 2) renderCals();
            updateRangeLabel();
        }
        function onCellClick(date) {
            if (pickStep === 0 || pickStep === 0) {
                customStart = new Date(date); customStart.setHours(0,0,0,0);
                customEnd = null; hoverDate = null; pickStep = 2;
            } else {
                var end = new Date(date); end.setHours(23,59,59,999);
                if (end < customStart) {
                    customEnd = new Date(customStart); customEnd.setHours(23,59,59,999);
                    customStart = new Date(date); customStart.setHours(0,0,0,0);
                } else { customEnd = end; }
                pickStep = 0;
            }
            renderCals(); updateRangeLabel();
        }
        function updateRangeLabel() {
            var s = customStart, e = customEnd || (pickStep===2&&hoverDate?hoverDate:null);
            if (s && e)  rangeLabel.textContent = fmt(s) + ' → ' + fmt(e);
            else if (s)  rangeLabel.textContent = fmt(s) + ' → …';
            else         rangeLabel.textContent = 'Selecciona el inicio del rango';
        }
        function applyCustom() {
            if (!customStart || !customEnd) return;
            currentStart = customStart; currentEnd = customEnd; activePreset = 'custom';
            updateLabel(); highlightPreset(); closeDropdown(); fireChange();
        }

        // ── Preset selection ──────────────────────────────────────────────────
        function selectPreset(key) {
            if (key === 'custom') {
                customPan.style.display = 'block';
                pickStep = 0; customStart = null; customEnd = null;
                initCalMonths(); renderCals(); updateRangeLabel();
                return;
            }
            var r = getPresetRange(key);
            currentStart = r.start; currentEnd = r.end; activePreset = key;
            updateLabel(); highlightPreset(); closeDropdown(); fireChange();
        }
        function highlightPreset() {
            presetsEl.querySelectorAll('.hdate-preset-item').forEach(function(el){
                el.classList.toggle('hdate-preset-active', el.dataset.key === activePreset);
            });
        }
        function updateLabel() {
            if (activePreset !== 'custom') {
                var p = PRESETS.find(function(x){ return x.key === activePreset; });
                labelEl.textContent = p ? p.label : '';
            } else {
                labelEl.textContent = fmtShort(currentStart) + ' – ' + fmtShort(currentEnd);
            }
        }

        // ── Toggle ────────────────────────────────────────────────────────────
        function openDropdown() {
            customPan.style.display = 'none';
            dd.style.display = 'flex';
            highlightPreset();
            var rect = root.getBoundingClientRect();
            if (window.innerWidth - rect.right < 500) {
                dd.style.right = '0'; dd.style.left = 'auto';
            } else {
                dd.style.left = '0'; dd.style.right = 'auto';
            }
        }
        function closeDropdown() { dd.style.display = 'none'; }

        trigger.addEventListener('click', function(e){
            e.stopPropagation();
            dd.style.display === 'none' ? openDropdown() : closeDropdown();
        });
        document.addEventListener('click', function(e){
            if (!root.contains(e.target)) closeDropdown();
        });

        // ── Init ──────────────────────────────────────────────────────────────
        function fireChange() {
            if (onChange) onChange({ start: currentStart, end: currentEnd, preset: activePreset });
        }
        updateLabel(); highlightPreset();
        setTimeout(function(){ fireChange(); }, 50);

        // Public
        this.getRange  = function(){ return { start: currentStart, end: currentEnd }; };
        this.setPreset = function(key){ selectPreset(key); };
    }

    window.HDatePicker = HDatePicker;
})();
