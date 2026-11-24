/* =========================================================
   SmartPark notifications (bell)
   Save as: static/js/notifications.js

   Load this on EVERY page that has the bell, and do NOT keep
   any other notification code in dashboard.js.

   Required markup (same on every page):

   <div class="notif-wrap" id="notif-wrap"
        data-url="{% url 'notifications_data' %}"
        data-user="{{ request.user.pk }}">
       <button id="notif-btn"> ... <span id="notif-dot" hidden></span></button>
       <div class="notif-panel" id="notif-panel">
           <span id="notif-count" hidden></span>
           <button id="notif-read-all">Mark all as read</button>
           <ul id="notif-list"></ul>
       </div>
   </div>
   ========================================================= */
(function () {
    'use strict';

    // Guard: never initialise twice, even if the script is included twice.
    if (window.__smartparkNotifInit) return;
    window.__smartparkNotifInit = true;

    var wrap = document.getElementById('notif-wrap');
    if (!wrap) return;   // this page has no bell

    var btn     = document.getElementById('notif-btn');
    var dot     = document.getElementById('notif-dot');
    var count   = document.getElementById('notif-count');
    var list    = document.getElementById('notif-list');
    var readAll = document.getElementById('notif-read-all');

    if (!btn || !dot || !count || !list || !readAll) {
        console.error('SmartPark: notification HTML elements are missing.');
        return;
    }

    // Works on every page: wrapper first, <body> as a fallback.
    var API_URL = wrap.dataset.url  || document.body.dataset.notifUrl  || '';
    var USER_ID = wrap.dataset.user || document.body.dataset.userId    || 'anon';

    var STORE_KEY  = 'smartpark_notif_seen_' + USER_ID;
    var REFRESH_MS = 15000;

    var items = [];
    var timer = null;
    var loaded = false;

    /* ---------- helpers ---------- */
    function el(tag, className, text) {
        var node = document.createElement(tag);
        if (className) node.className = className;
        if (text !== undefined) node.textContent = text;
        return node;
    }

    function loadSeen() {
        try {
            var data = JSON.parse(localStorage.getItem(STORE_KEY));
            return Array.isArray(data) ? data : [];
        } catch (e) {
            return [];
        }
    }

    function saveSeen(ids) {
        try {
            localStorage.setItem(STORE_KEY, JSON.stringify(ids.slice(-200)));
        } catch (e) { /* storage unavailable */ }
    }

    function markSeen(ids) {
        var seen = loadSeen();
        ids.forEach(function (id) {
            if (seen.indexOf(id) === -1) seen.push(id);
        });
        saveSeen(seen);
    }

    function isUnread(n, seen) {
        return seen.indexOf(n.id) === -1;
    }

    function showMessage(iconClass, message) {
        list.innerHTML = '';
        var li = el('li', 'notif-empty');
        li.appendChild(el('i', 'bi ' + iconClass));
        li.appendChild(el('span', '', message));
        list.appendChild(li);
    }

    /* ---------- render ---------- */
    function render() {
        var seen = loadSeen();
        var unread = items.filter(function (n) { return isUnread(n, seen); }).length;

        dot.hidden = unread === 0;
        count.hidden = unread === 0;
        count.textContent = unread + ' new';
        readAll.disabled = unread === 0;
        btn.setAttribute(
            'aria-label',
            unread ? 'Notifications (' + unread + ' new)' : 'Notifications'
        );

        if (!items.length) {
            showMessage('bi-bell-slash', "You're all caught up.");
            return;
        }

        list.innerHTML = '';

        items.forEach(function (n) {
            var li = el(
                'li',
                'notif-item ' + (n.level || 'info') +
                (isUnread(n, seen) ? ' unread' : '') +
                (n.url ? ' clickable' : '')
            );

            if (n.url) {
                li.tabIndex = 0;
                li.setAttribute('role', 'link');
            }

            var ico = el('span', 'notif-ico');
            ico.appendChild(el('i', 'bi ' + (n.icon || 'bi-bell')));

            var body = el('div', 'notif-body');
            body.appendChild(el('b', '', n.title || 'Notification'));
            body.appendChild(el('p', '', n.text || ''));
            body.appendChild(el('small', '', n.ago || ''));

            li.appendChild(ico);
            li.appendChild(body);

            function openItem() {
                markSeen([n.id]);
                if (n.url) {
                    window.location.href = n.url;
                } else {
                    render();
                }
            }

            li.addEventListener('click', openItem);
            li.addEventListener('keydown', function (e) {
                if (e.key === 'Enter' || e.key === ' ') {
                    e.preventDefault();
                    openItem();
                }
            });

            list.appendChild(li);
        });
    }

    /* ---------- fetching ---------- */
    function refresh() {
        if (!API_URL) {
            console.error('SmartPark: notification URL is missing (data-url on #notif-wrap).');
            showMessage('bi-wifi-off', 'Could not load notifications.');
            return;
        }

        fetch(API_URL, {
            method: 'GET',
            credentials: 'same-origin',
            headers: { 'X-Requested-With': 'XMLHttpRequest' }
        })
            .then(function (res) {
                if (!res.ok) throw new Error('HTTP ' + res.status);
                return res.json();
            })
            .then(function (data) {
                if (!data || !Array.isArray(data.items)) {
                    throw new Error('Expected {"items": [...]}');
                }
                items = data.items;
                loaded = true;
                render();
            })
            .catch(function (err) {
                console.error('SmartPark notifications error:', err);
                // Keep showing the last good data if we have it.
                if (!loaded) showMessage('bi-wifi-off', 'Could not load notifications.');
            });
    }

    function stop() {
        if (timer) {
            clearInterval(timer);
            timer = null;
        }
    }

    function start() {
        stop();
        refresh();
        timer = setInterval(refresh, REFRESH_MS);
    }

    document.addEventListener('visibilitychange', function () {
        if (document.hidden) stop(); else start();
    });

    /* ---------- open / close panel ---------- */
    function toggle(force) {
        var show = (force === undefined) ? !wrap.classList.contains('open') : force;
        wrap.classList.toggle('open', show);
        btn.setAttribute('aria-expanded', show ? 'true' : 'false');
    }

    btn.addEventListener('click', function (e) {
        e.stopPropagation();
        toggle();
    });

    // Clicks inside the panel must not close it.
    document.addEventListener('click', function (e) {
        if (!wrap.contains(e.target)) toggle(false);
    });

    document.addEventListener('keydown', function (e) {
        if (e.key === 'Escape' && wrap.classList.contains('open')) {
            toggle(false);
            btn.focus();
        }
    });

    readAll.addEventListener('click', function () {
        markSeen(items.map(function (n) { return n.id; }));
        render();
    });

    start();
})();