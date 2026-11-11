(function () {
    var wrap = document.getElementById('notif-wrap');
    if (!wrap) return;

    var API_URL = wrap.dataset.url;
    var KEY     = 'smartpark_notif_seen_' + (wrap.dataset.user || '');
    var btn     = document.getElementById('notif-btn');
    var dot     = document.getElementById('notif-dot');
    var count   = document.getElementById('notif-count');
    var list    = document.getElementById('notif-list');
    var readAll = document.getElementById('notif-read-all');
    var items = [], timer = null;

    function loadSeen() { try { return JSON.parse(localStorage.getItem(KEY)) || []; } catch (e) { return []; } }
    function markSeen(ids) {
        var seen = loadSeen();
        ids.forEach(function (id) { if (seen.indexOf(id) === -1) seen.push(id); });
        try { localStorage.setItem(KEY, JSON.stringify(seen.slice(-200))); } catch (e) {}
    }

    function render() {
        var seen = loadSeen();
        var unread = items.filter(function (n) { return seen.indexOf(n.id) === -1; }).length;
        dot.hidden = count.hidden = unread === 0;
        count.textContent = unread + ' new';
        readAll.disabled = unread === 0;
        list.innerHTML = '';

        if (!items.length) {
            list.innerHTML = '<li class="notif-empty"><i class="bi bi-bell-slash"></i><span>You\'re all caught up.</span></li>';
            return;
        }
        items.forEach(function (n) {
            var li = document.createElement('li');
            li.className = 'notif-item ' + n.level + (seen.indexOf(n.id) === -1 ? ' unread' : '') + (n.url ? ' clickable' : '');
            li.innerHTML = '<span class="notif-ico"><i class="bi ' + n.icon + '"></i></span>' +
                           '<div class="notif-body"><b></b><p></p><small></small></div>';
            li.querySelector('b').textContent = n.title;
            li.querySelector('p').textContent = n.text;
            li.querySelector('small').textContent = n.ago;
            li.addEventListener('click', function () {
                markSeen([n.id]);
                if (n.url) window.location.href = n.url; else render();
            });
            list.appendChild(li);
        });
    }

    function refresh() {
        fetch(API_URL, { credentials: 'same-origin', headers: { 'X-Requested-With': 'XMLHttpRequest' } })
            .then(function (r) { if (!r.ok) throw 0; return r.json(); })
            .then(function (d) { items = d.items || []; render(); })
            .catch(function () {});
    }
    function start() { stop(); refresh(); timer = setInterval(refresh, 15000); }
    function stop()  { if (timer) { clearInterval(timer); timer = null; } }
    document.addEventListener('visibilitychange', function () { document.hidden ? stop() : start(); });

    function toggle(show) {
        if (show === undefined) show = !wrap.classList.contains('open');
        wrap.classList.toggle('open', show);
        btn.setAttribute('aria-expanded', show);
    }
    btn.addEventListener('click', function (e) { e.stopPropagation(); toggle(); });
    document.addEventListener('click', function (e) { if (!wrap.contains(e.target)) toggle(false); });
    document.addEventListener('keydown', function (e) { if (e.key === 'Escape') toggle(false); });
    readAll.addEventListener('click', function () {
        markSeen(items.map(function (n) { return n.id; }));
        render();
    });

    start();
})();