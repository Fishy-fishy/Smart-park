/* =========================================================
   SmartPark dashboard
   Save as: static/js/dashboard.js
   Django values arrive through data-* attributes on <body>.

   NOTE: notifications (the bell) live in notifications.js.
   ========================================================= */
(function () {
    'use strict';

    var body = document.body;
    var CFG = {
        isAdmin:   body.getAttribute('data-is-admin') === '1',
        userId:    body.getAttribute('data-user-id'),
        mapUrl:    body.getAttribute('data-map-url'),
        reserveUrl: body.getAttribute('data-reserve-url'),
        mySlotId:  body.getAttribute('data-my-slot-id') || ''
    };

    /* ---------- tiny helpers ---------- */
    function $(id) { return document.getElementById(id); }
    function el(tag, className, text) {
        var node = document.createElement(tag);
        if (className) node.className = className;
        if (text !== undefined) node.textContent = text;
        return node;
    }
    function setText(id, value) {
        var node = $(id);
        if (node) node.textContent = value;
    }
    function pad(n) { return n < 10 ? '0' + n : '' + n; }

    /* =========================================================
       MODAL MANAGER (one Escape handler, focus trap, focus restore)
       ========================================================= */
    var Modals = (function () {
        var stack = [];
        var FOCUSABLE = 'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea, [tabindex]:not([tabindex="-1"])';

        function visible(node) { return node.offsetParent !== null || node === document.activeElement; }

        function open(modal, focusEl) {
            if (!modal || modal.classList.contains('show')) return;
            modal._returnFocus = document.activeElement;
            modal.classList.add('show');
            stack.push(modal);
            var app = document.querySelector('.app');
            if (app) app.setAttribute('aria-hidden', 'true');
            var target = focusEl || modal.querySelector('.btn-cancel') || modal.querySelector(FOCUSABLE);
            if (target) target.focus();
        }

        function close(modal) {
            if (!modal || !modal.classList.contains('show')) return;
            modal.classList.remove('show');
            var i = stack.indexOf(modal);
            if (i !== -1) stack.splice(i, 1);
            if (!stack.length) {
                var app = document.querySelector('.app');
                if (app) app.removeAttribute('aria-hidden');
            }
            if (modal._returnFocus && modal._returnFocus.focus) {
                try { modal._returnFocus.focus(); } catch (e) { /* element gone */ }
            }
        }

        function top() { return stack[stack.length - 1]; }

        document.addEventListener('keydown', function (e) {
            var t = top();
            if (!t) return;
            if (e.key === 'Escape') { e.preventDefault(); close(t); return; }
            if (e.key === 'Tab') {
                var items = Array.prototype.filter.call(t.querySelectorAll(FOCUSABLE), visible);
                if (!items.length) { e.preventDefault(); return; }
                var first = items[0], last = items[items.length - 1];
                if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
                else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
                else if (!t.contains(document.activeElement)) { e.preventDefault(); first.focus(); }
            }
        });

        // Click on the dark overlay (not the box) closes the modal
        Array.prototype.forEach.call(document.querySelectorAll('.modal-overlay'), function (m) {
            m.addEventListener('click', function (e) { if (e.target === m) close(m); });
        });

        return { open: open, close: close, isOpen: function () { return stack.length > 0; } };
    })();

    /* =========================================================
       MOBILE SIDEBAR (hamburger + backdrop + close on link tap)
       ========================================================= */
    (function () {
        var sidebar  = $('sidebar');
        var backdrop = $('backdrop');
        var btn      = $('menu-btn');
        if (!sidebar || !btn) return;

        function setOpen(show) {
            sidebar.classList.toggle('open', show);
            if (backdrop) backdrop.classList.toggle('show', show);
            btn.setAttribute('aria-expanded', show ? 'true' : 'false');
        }
        btn.addEventListener('click', function () { setOpen(!sidebar.classList.contains('open')); });
        if (backdrop) backdrop.addEventListener('click', function () { setOpen(false); });
        sidebar.addEventListener('click', function (e) { if (e.target.closest('a.nav-link')) setOpen(false); });
        document.addEventListener('keydown', function (e) {
            if (e.key === 'Escape' && sidebar.classList.contains('open') && !Modals.isOpen()) setOpen(false);
        });
    })();

    /* =========================================================
       LOGOUT CONFIRMATION
       ========================================================= */
    (function () {
        var modal = $('logout-modal');
        var form  = $('logout-form');
        $('logout-open').addEventListener('click', function () { Modals.open(modal, $('logout-cancel')); });
        $('logout-cancel').addEventListener('click', function () { Modals.close(modal); });
        $('logout-confirm').addEventListener('click', function () { form.submit(); });
    })();

    /* =========================================================
       RELEASE CONFIRMATION (replaces window.confirm)
       ========================================================= */
    (function () {
        var modal   = $('release-modal');
        var desc    = $('release-desc');
        var pending = null;

        document.addEventListener('submit', function (e) {
            var f = e.target;
            if (!f.classList || !f.classList.contains('release-form')) return;
            e.preventDefault();
            pending = f;
            desc.textContent = 'Release slot ' + (f.getAttribute('data-slot') || '') +
                               ' for ' + (f.getAttribute('data-plate') || 'this vehicle') + '?';
            Modals.open(modal, $('release-cancel'));
        });

        $('release-cancel').addEventListener('click', function () { pending = null; Modals.close(modal); });
        $('release-confirm').addEventListener('click', function () {
            var f = pending;
            pending = null;
            if (!f) return;
            var btn = $('release-confirm');
            btn.disabled = true;
            f.submit();   // native submit() does not re-trigger the submit event
        });
    })();

    /* =========================================================
       LIVE ELAPSED TIME + ESTIMATED FEE
       ========================================================= */
    (function () {
        function fmtElapsed(ms) {
            if (ms < 0) ms = 0;
            var s = Math.floor(ms / 1000);
            var h = Math.floor(s / 3600);
            var m = Math.floor((s % 3600) / 60);
            var sec = s % 60;
            if (h > 0) return h + 'h ' + pad(m) + 'm';
            return m + 'm ' + pad(sec) + 's';
        }

        function tick() {
            var now = Date.now();

            Array.prototype.forEach.call(document.querySelectorAll('.js-elapsed'), function (node) {
                var since = Date.parse(node.getAttribute('data-since'));
                if (!isNaN(since)) node.textContent = fmtElapsed(now - since);
            });

            Array.prototype.forEach.call(document.querySelectorAll('.js-fee'), function (node) {
                var since = Date.parse(node.getAttribute('data-since'));
                var rate  = parseFloat(node.getAttribute('data-rate')) || 0;
                if (isNaN(since) || !rate) { node.textContent = '\u2014'; return; }
                var hours = Math.max(1, Math.ceil((now - since) / 3600000));
                node.textContent = '\u20B1' + (hours * rate).toFixed(2);
            });
        }

        tick();
        setInterval(tick, 1000);
    })();

    /* =========================================================
       QUICK ALLOCATE FORM
       ========================================================= */
    var Alloc = (function () {
        var form      = $('allocate-form');
        var vtype     = $('vtype');
        var plate     = $('plate');
        var vSelect   = $('vehicle-select');      // only for drivers with saved vehicles
        var otherWrap = $('plate-other-wrap');
        var other     = $('plate-other');
        var onChange  = function () {};

       function syncVehicle() {
            if (!vSelect) return;
            if (vSelect.value === '__other__') {
                otherWrap.hidden = false;
                other.required = true;
                plate.value = other.value.trim();
                vtype.disabled = false;          // new plate: driver chooses the type
            } else {
                otherWrap.hidden = true;
                other.required = false;
                plate.value = vSelect.value;
                var t = vSelect.options[vSelect.selectedIndex].getAttribute('data-type');
                if (t && vtype.querySelector('option[value="' + t + '"]')) {
                    vtype.value = t;
                }
                vtype.disabled = true;           // saved vehicle: type is locked
            }
            onChange();
        }

        if (vSelect) {
            vSelect.addEventListener('change', syncVehicle);
            other.addEventListener('input', function () { plate.value = other.value.trim(); });
            syncVehicle();
        }
        if (vtype) vtype.addEventListener('change', function () { onChange(); });

        if (form) {
            form.addEventListener('submit', function (e) {
                if (vSelect && vSelect.value === '__other__' && !other.value.trim()) {
                    e.preventDefault();
                    other.focus();
                    return;
                }
                vtype.disabled = false;         // disabled fields are not posted
                var btn = form.querySelector('button[type="submit"]');
                if (btn) {
                    btn.disabled = true;
                    btn.innerHTML = '<i class="bi bi-hourglass-split"></i> Allocating...';
                }
            });
        }

        var findLink = $('find-slot-link');
        if (findLink) {
            findLink.addEventListener('click', function (e) {
                e.preventDefault();
                form.scrollIntoView({ behavior: 'smooth', block: 'center' });
                (vSelect || plate).focus({ preventScroll: true });
            });
        }

        return {
            vehicleType: function () { return vtype ? vtype.value : ''; },
            onChange: function (fn) { onChange = fn; }
        };
    })();

    /* =========================================================
       LIVE SLOT MAP
       ========================================================= */
    (function () {
        // Polling backs off after repeated failures: 5s -> 15s -> 30s
        var DELAYS = [5000, 15000, 30000];

        // Icons for slots that are not occupied
        var ICONS = {
            available:   'bi-p-lg',
            occupied:    'bi-car-front-fill',
            reserved:    'bi-bookmark-fill',
            maintenance: 'bi-cone-striped'
        };

        // Icons for the vehicle parked in an occupied slot.
        // Requires Bootstrap Icons 1.10+ (you load 1.11.3).
        var VEHICLE_ICONS = {
            car:        'bi-car-front-fill',
            motorcycle: 'bi-scooter',
            truck:      'bi-truck-front-fill',
            pwd:        'bi-person-wheelchair'
        };

        // Accepts "car", "Car", "Motorcycle", "PWD", "Person with Disability", etc.
        function vehicleIcon(type) {
            var t = String(type || '').toLowerCase();
            if (!t) return VEHICLE_ICONS.car;
            if (t.indexOf('motor') !== -1 || t.indexOf('scooter') !== -1 || t.indexOf('bike') !== -1) return VEHICLE_ICONS.motorcycle;
            if (t.indexOf('truck') !== -1 || t.indexOf('van') !== -1) return VEHICLE_ICONS.truck;
            if (t.indexOf('pwd') !== -1 || t.indexOf('disab') !== -1 || t.indexOf('wheelchair') !== -1) return VEHICLE_ICONS.pwd;
            return VEHICLE_ICONS.car;
        }

        // One place that decides which icon a slot gets
        function iconFor(slot) {
            if (slot.status === 'occupied') return vehicleIcon(slot.vehicle_type);
            return ICONS[slot.status] || ICONS.available;
        }

        var mapEl      = $('slot-map');
        var liveStatus = $('live-status');
        var liveText   = $('live-text');
        var legend     = $('legend-filter');
        var searchEl   = $('slot-search');

        var state = {
            data: null,
            filter: null,
            query: '',
            selectedId: null,
            failures: 0,
            timer: null
        };

        /* ---------- helpers ---------- */
        function findSlot(id) {
            if (!state.data) return null;
            for (var z = 0; z < state.data.zones.length; z++) {
                var zone = state.data.zones[z];
                for (var s = 0; s < zone.slots.length; s++) {
                    if (String(zone.slots[s].id) === String(id)) {
                        return { slot: zone.slots[s], zone: zone };
                    }
                }
            }
            return null;
        }
        function isMine(slot) {
            return slot.mine === true || (CFG.mySlotId && String(slot.id) === String(CFG.mySlotId));
        }
        function matchesQuery(slot) {
            if (!state.query) return true;
            var q = state.query.toLowerCase();
            if ((slot.number || '').toLowerCase().indexOf(q) !== -1) return true;
            // The server only sends a plate to administrators and to the vehicle's owner
            return (slot.plate || '').toLowerCase().indexOf(q) !== -1;
        }
        // Mirrors required_slot_type() in views.py: a vehicle type with its own slot type
        // needs that type, anything else needs regular "car" slots.
        function compatible(slot) {
            var t = slot.slot_type;
            var known = (state.data && state.data.slot_types) || [];
            if (!t || !known.length) return true;
            var v = Alloc.vehicleType();
            var needed = known.indexOf(v) !== -1 ? v : 'car';
            return t === needed;
        }
        function slotLabel(slot) {
            var label = slot.number + ', ' + slot.status_label;
            // Plate is only present for administrators and the vehicle's owner
            if (slot.status === 'occupied' && slot.plate) label += ' (' + slot.plate + ')';
            if (isMine(slot)) label += ' (your slot)';
            return label;
        }
        function slotNode(slot, dimmed, selected) {
            var cls = 'slot ' + slot.status;
            if (dimmed) cls += ' dim';
            if (selected) cls += ' selected';
            if (isMine(slot)) cls += ' mine';

            var node = el('div', cls);
            node.setAttribute('data-id', slot.id);
            node.setAttribute('tabindex', '0');
            node.setAttribute('role', 'button');
            node.setAttribute('aria-label', slotLabel(slot));
            node.title = slotLabel(slot);
            node.appendChild(el('span', 'slot-no', slot.number));
            node.appendChild(el('i', 'bi ' + iconFor(slot)));
            return node;
        }

        /* ---------- render ---------- */
        function clamp(n) { n = Number(n) || 0; return Math.max(0, Math.min(100, n)); }

        function renderStats(d) {
            setText('stat-total', d.total);
            setText('stat-available', d.available);
            setText('stat-occupied', d.occupied);
            setText('stat-reserved', d.reserved);
            setText('leg-available', d.available);
            setText('leg-occupied', d.occupied);
            setText('leg-reserved', d.reserved);
            var pct = clamp(d.occupancy_rate);
            setText('donut-pct', pct);
            var donut = $('donut');
            if (donut) {
                donut.style.setProperty('--occ', pct);
                donut.setAttribute('aria-label', 'Occupancy rate ' + pct + ' percent');
            }
        }

        function renderMap(d) {
            mapEl.innerHTML = '';

            if (!d.zones.length) {
                var empty = el('div', 'empty');
                empty.appendChild(el('i', 'bi bi-inbox'));
                empty.appendChild(el('p', '', 'No parking zones yet.'));
                mapEl.appendChild(empty);
                return;
            }

            d.zones.forEach(function (zone) {
                var wrap = el('div', 'zone');

                var head = el('div', 'zone-head');
                var h4 = el('h4');
                h4.appendChild(el('i', 'bi bi-geo-alt-fill'));
                h4.appendChild(document.createTextNode(' ' + zone.name));
                head.appendChild(h4);
                head.appendChild(el('span', 'muted', zone.available + ' free'));
                wrap.appendChild(head);

                var grid = el('div', 'slot-grid');
                if (!zone.slots.length) grid.appendChild(el('p', 'muted', 'No slots in this zone.'));

                zone.slots.forEach(function (slot) {
                    var dimmed = (state.filter && slot.status !== state.filter) ||
                                 !matchesQuery(slot) ||
                                 (slot.status === 'available' && !compatible(slot));
                    grid.appendChild(slotNode(slot, dimmed, String(slot.id) === String(state.selectedId)));
                });
                wrap.appendChild(grid);
                mapEl.appendChild(wrap);
            });
        }

        function renderAll() {
            if (!state.data) return;
            renderStats(state.data);
            renderMap(state.data);
            renderAllModal();
        }
        Alloc.onChange(renderAll);

        /* ---------- fetching with back-off ---------- */
        function setOnline(ok) {
            liveStatus.classList.toggle('offline', !ok);
            liveText.textContent = ok
                ? 'Live \u00b7 updated ' + new Date().toLocaleTimeString()
                : 'Connection lost \u00b7 retrying...';
        }

        function schedule() {
            stopPolling();
            var delay = DELAYS[Math.min(state.failures, DELAYS.length - 1)];
            state.timer = setTimeout(refresh, delay);
        }

        function refresh() {
            fetch(CFG.mapUrl, { headers: { 'X-Requested-With': 'XMLHttpRequest' }, credentials: 'same-origin' })
                .then(function (res) {
                    if (!res.ok) throw new Error('HTTP ' + res.status);
                    return res.json();
                })
                .then(function (data) {
                    state.failures = 0;
                    state.data = data;
                    renderAll();
                    if (state.selectedId) {
                        var found = findSlot(state.selectedId);
                        if (!found || found.slot.status !== 'available') clearSelection();
                    }
                    setOnline(true);
                })
                .catch(function () {
                    state.failures += 1;
                    setOnline(false);
                })
                .then(function () { if (!document.hidden) schedule(); });
        }

        function startPolling() { stopPolling(); refresh(); }
        function stopPolling()  { if (state.timer) { clearTimeout(state.timer); state.timer = null; } }
        document.addEventListener('visibilitychange', function () {
            if (document.hidden) stopPolling(); else startPolling();
        });

        /* ---------- legend filter ---------- */
        legend.addEventListener('click', function (e) {
            var item = e.target.closest('[data-filter]');
            if (!item) return;
            var f = item.getAttribute('data-filter');
            state.filter = (state.filter === f) ? null : f;
            Array.prototype.forEach.call(legend.children, function (c) {
                c.classList.toggle('active', c.getAttribute('data-filter') === state.filter);
            });
            renderAll();
        });

        /* ---------- search ---------- */
        if (searchEl) {
            searchEl.addEventListener('input', function () {
                state.query = searchEl.value.trim();
                renderAll();
            });
        }

        /* ---------- slot details modal ---------- */
        var slotModal    = $('slot-modal');
        var slotTitle    = $('slot-modal-title');
        var slotSub      = $('slot-modal-sub');
        var slotDetails  = $('slot-modal-details');
        var slotIcon     = $('slot-modal-icon');
        var slotIconI    = $('slot-modal-icon-i');
        var slotAllocate = $('slot-modal-allocate');
        var slotReserve  = $('slot-modal-reserve');   // drivers only
        var modalSlotId  = null;

        function addDetail(label, value) {
            var li = el('li');
            li.appendChild(el('span', '', label));
            li.appendChild(el('b', '', value));
            slotDetails.appendChild(li);
        }

        function openSlotModal(id) {
            var found = findSlot(id);
            if (!found) return;
            var slot = found.slot, zone = found.zone;
            modalSlotId = slot.id;

            slotTitle.textContent = 'Slot ' + slot.number;
            slotSub.textContent = zone.name;
            slotIcon.className = 'modal-icon ' + slot.status;
            slotIconI.className = 'bi ' + iconFor(slot);

            slotDetails.innerHTML = '';
            addDetail('Status', slot.status_label);
            addDetail('Zone', zone.name);
            if (isMine(slot)) addDetail('Note', 'This is your slot');

            // The server only includes these for administrators and the owner
            if (slot.status === 'occupied') {
                if (slot.plate) addDetail('Vehicle', slot.plate);
                if (slot.vehicle_type) addDetail('Type', slot.vehicle_type_label || slot.vehicle_type);
                if (slot.since) addDetail('Parked since', slot.since);
            }

            var free = slot.status === 'available';
            slotAllocate.style.display = free ? '' : 'none';
            if (slotReserve) {
                slotReserve.style.display = free ? '' : 'none';
                slotReserve.setAttribute('href', CFG.reserveUrl + '?slot=' + encodeURIComponent(slot.id));
            }
            Modals.open(slotModal, $('slot-modal-close'));
        }
        function closeSlotModal() { Modals.close(slotModal); modalSlotId = null; }
        $('slot-modal-close').addEventListener('click', closeSlotModal);

        function onSlotActivate(e, root) {
            var node = e.target.closest('.slot');
            if (!node || !root.contains(node)) return false;
            return node;
        }
        mapEl.addEventListener('click', function (e) {
            var node = onSlotActivate(e, mapEl);
            if (node) openSlotModal(node.getAttribute('data-id'));
        });
        mapEl.addEventListener('keydown', function (e) {
            if ((e.key === 'Enter' || e.key === ' ') && e.target.classList.contains('slot')) {
                e.preventDefault();
                openSlotModal(e.target.getAttribute('data-id'));
            }
        });

        /* ---------- "View all" modal ---------- */
        var allModal  = $('all-modal');
        var allBody   = $('all-modal-body');
        var allSub    = $('all-modal-sub');
        var allFilter = $('all-filter');
        var allStatus = '';

        function renderAllModal() {
            if (!state.data || !allModal.classList.contains('show')) return;
            var d = state.data;

            allSub.textContent = d.total + ' slots \u00b7 ' + d.available + ' available \u00b7 ' +
                                 d.occupied + ' occupied \u00b7 ' + d.reserved + ' reserved \u00b7 ' +
                                 d.maintenance + ' maintenance';

            Array.prototype.forEach.call(allFilter.children, function (c) {
                c.classList.toggle('active', c.getAttribute('data-filter') === allStatus);
            });

            allBody.innerHTML = '';
            d.zones.forEach(function (zone) {
                var slots = zone.slots.filter(function (s) { return !allStatus || s.status === allStatus; });
                if (!slots.length) return;

                var wrap = el('div', 'zone');
                var head = el('div', 'zone-head');
                var h4 = el('h4');
                h4.appendChild(el('i', 'bi bi-geo-alt-fill'));
                h4.appendChild(document.createTextNode(' ' + zone.name));
                head.appendChild(h4);
                head.appendChild(el('span', 'muted', slots.length + ' slot' + (slots.length === 1 ? '' : 's')));
                wrap.appendChild(head);

                var grid = el('div', 'slot-grid');
                slots.forEach(function (slot) { grid.appendChild(slotNode(slot, false, false)); });
                wrap.appendChild(grid);
                allBody.appendChild(wrap);
            });

            if (!allBody.children.length) {
                allBody.appendChild(el('p', 'muted', 'No slots match this filter.'));
            }
        }

        function openAllModal() { allStatus = ''; Modals.open(allModal, $('all-modal-close')); renderAllModal(); }
        $('view-all-btn').addEventListener('click', openAllModal);
        $('all-modal-close').addEventListener('click', function () { Modals.close(allModal); });
        allFilter.addEventListener('click', function (e) {
            var item = e.target.closest('[data-filter]');
            if (!item) return;
            allStatus = item.getAttribute('data-filter');
            renderAllModal();
        });

        function pickFromAll(node) {
            if (!node) return;
            var id = node.getAttribute('data-id');
            Modals.close(allModal);
            openSlotModal(id);
        }
        allBody.addEventListener('click', function (e) { pickFromAll(e.target.closest('.slot')); });
        allBody.addEventListener('keydown', function (e) {
            if ((e.key === 'Enter' || e.key === ' ') && e.target.classList.contains('slot')) {
                e.preventDefault();
                pickFromAll(e.target);
            }
        });

        /* ---------- pick a slot for Quick Allocate ---------- */
        var slotIdInput  = $('slot_id');
        var selectedBox  = $('selected-slot');
        var selectedName = $('selected-slot-name');
        var zoneSelect   = $('zone');
        var reserveLink  = $('reserve-link');

        function selectSlot(id) {
            var found = findSlot(id);
            if (!found) return;
            state.selectedId = found.slot.id;
            slotIdInput.value = found.slot.id;
            selectedName.textContent = found.slot.number + ' (' + found.zone.name + ')';
            selectedBox.classList.add('show');
            if (zoneSelect) zoneSelect.value = found.zone.id;
            if (reserveLink) reserveLink.setAttribute('href', CFG.reserveUrl + '?slot=' + encodeURIComponent(found.slot.id));
            renderAll();
        }
        function clearSelection() {
            state.selectedId = null;
            slotIdInput.value = '';
            selectedBox.classList.remove('show');
            if (reserveLink) reserveLink.setAttribute('href', CFG.reserveUrl);
            renderAll();
        }

        slotAllocate.addEventListener('click', function () {
            var id = modalSlotId;
            closeSlotModal();
            selectSlot(id);
            $('allocate-form').scrollIntoView({ behavior: 'smooth', block: 'center' });
            var firstField = $('vehicle-select') || $('plate');
            if (firstField && firstField.type !== 'hidden') firstField.focus({ preventScroll: true });
        });
        $('selected-slot-clear').addEventListener('click', clearSelection);

        /* ---------- go ---------- */
        startPolling();
    })();

    /* =========================================================
       FLASH MESSAGES: auto-hide after 6 seconds
       ========================================================= */
    Array.prototype.forEach.call(document.querySelectorAll('.toast'), function (t) {
        setTimeout(function () {
            t.classList.add('hide');
            setTimeout(function () { t.remove(); }, 450);
        }, 6000);
    });
})();