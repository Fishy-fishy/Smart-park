
/* SmartPark — payment page behaviour
   Handles:
   - Mobile sidebar
   - Logout confirmation modal
   - Live parking fee calculation
   - GCash QR code display
   - Cash Counter payment display

   IMPORTANT:
   The server must calculate and validate the actual payment amount.
*/

(function () {
    'use strict';

    var $ = function (id) {
        return document.getElementById(id);
    };

    /* ---------- sidebar (mobile) ---------- */

    var sidebar = $('sidebar');
    var backdrop = $('backdrop');
    var menuBtn = $('menu-btn');

    function setMenu(open) {
        if (!sidebar || !backdrop || !menuBtn) return;

        sidebar.classList.toggle('open', open);
        backdrop.classList.toggle('show', open);
        menuBtn.setAttribute('aria-expanded', open ? 'true' : 'false');
    }

    if (menuBtn) {
        menuBtn.addEventListener('click', function () {
            setMenu(!sidebar.classList.contains('open'));
        });
    }

    if (backdrop) {
        backdrop.addEventListener('click', function () {
            setMenu(false);
        });
    }

    /* ---------- logout modal ---------- */

    var logoutModal = $('logout-modal');

    if (logoutModal) {

        var logoutOpen = $('logout-open');
        var logoutCancel = $('logout-cancel');
        var logoutConfirm = $('logout-confirm');
        var logoutForm = $('logout-form');

        if (logoutOpen) {
            logoutOpen.addEventListener('click', function () {
                logoutModal.classList.add('show');
            });
        }

        if (logoutCancel) {
            logoutCancel.addEventListener('click', function () {
                logoutModal.classList.remove('show');
            });
        }

        if (logoutConfirm && logoutForm) {
            logoutConfirm.addEventListener('click', function () {
                logoutForm.submit();
            });
        }

        logoutModal.addEventListener('click', function (e) {
            if (e.target === logoutModal) {
                logoutModal.classList.remove('show');
            }
        });

        document.addEventListener('keydown', function (e) {
            if (e.key === 'Escape') {
                logoutModal.classList.remove('show');
            }
        });
    }

    /* ---------- payment amount ---------- */

    var card = $('session-card');
    var progress = $('free-progress');

    var amount = 0;

    function peso(value) {
        return '\u20B1' + value.toFixed(2);
    }

    function pad(value) {
        return String(value).padStart(2, '0');
    }

    function fmtHMS(seconds) {
        seconds = Math.max(0, Math.floor(seconds));

        var hours = Math.floor(seconds / 3600);
        var minutes = Math.floor((seconds % 3600) / 60);
        var remaining = seconds % 60;

        return pad(hours) + ':' + pad(minutes) + ':' + pad(remaining);
    }

    function fmtMin(minutes) {
        var hours = Math.floor(minutes / 60);
        var remaining = minutes % 60;

        return hours
            ? hours + ' hr ' + remaining + ' min'
            : remaining + ' min';
    }

    /* ---------- payment button ---------- */

    function updateButton() {

        var button = $('pay-btn');
        var buttonText = $('pay-btn-text');

        if (!button || !buttonText) return;

        var selectedMethod = document.querySelector(
            'input[name="method"]:checked'
        );

        button.disabled = amount <= 0;

        if (amount <= 0) {
            buttonText.textContent = 'Nothing to pay yet';
        }
        else if (selectedMethod && selectedMethod.value === 'cash') {
            buttonText.textContent =
                'Submit Cash Payment \u2022 ' + peso(amount);
        }
        else {
            buttonText.textContent = 'Submit GCash Payment \u2022 ' + peso(amount);
        }
    }

    /* ---------- live parking fee calculation ---------- */

    function tick() {

        if (!card || !progress) return;

        var freeMinutes =
            parseInt(card.dataset.freeMinutes, 10) || 60;

        var hourlyRate =
            parseFloat(card.dataset.rate) || 0;

        var startTime =
            new Date(progress.dataset.since).getTime();

        if (isNaN(startTime)) return;

        var elapsedSeconds =
            (Date.now() - startTime) / 1000;

        var freeSeconds = freeMinutes * 60;

        var remainingSeconds =
            freeSeconds - elapsedSeconds;

        var elapsedMinutes =
            Math.floor(elapsedSeconds / 60);

        var billableMinutes =
            Math.max(
                0,
                Math.ceil(elapsedSeconds / 60) - freeMinutes
            );

        var billableHours =
            Math.ceil(billableMinutes / 60);

        amount = billableHours * hourlyRate;

        var percentage =
            Math.min(100, (elapsedSeconds / freeSeconds) * 100);

        var bar = $('free-bar');
        var chip = $('free-status');
        var left = $('free-left');
        var barWrap = $('free-bar-wrap');

        if (bar) {
            bar.style.width = percentage + '%';

            var state = 'free';

            if (remainingSeconds <= 0) {
                state = 'paid';
            }
            else if (remainingSeconds <= 600) {
                state = 'warn';
            }

            bar.className =
                'bar-fill ' + (state === 'free' ? '' : state);

            if (chip) {
                chip.className = 'free-chip ' + state;

                chip.textContent =
                    state === 'paid'
                        ? 'Payment started'
                        : state === 'warn'
                            ? 'Free time ending soon'
                            : 'Free time left';
            }

            if (left) {
                left.textContent =
                    state === 'paid'
                        ? '+' + fmtHMS(-remainingSeconds) + ' over'
                        : fmtHMS(remainingSeconds);
            }

            if (barWrap) {
                barWrap.setAttribute(
                    'aria-valuenow',
                    Math.round(percentage)
                );
            }
        }

        var elapsedElement = $('bill-elapsed');
        var billableElement = $('bill-billable');
        var hoursElement = $('bill-hours');
        var totalElement = $('bill-total');

        if (elapsedElement) {
            elapsedElement.textContent = fmtMin(elapsedMinutes);
        }

        if (billableElement) {
            billableElement.textContent = billableMinutes + ' min';
        }

        if (hoursElement) {
            hoursElement.textContent = billableHours + ' hr';
        }

        if (totalElement) {
            totalElement.textContent = peso(amount);
        }

        var payTotal = $('pay-total');
var payAmount = $('pay-amount');
var qrAmount = $('qr-amount');

if (payTotal) {
    payTotal.textContent = peso(amount);
}

if (payAmount) {
    payAmount.value = amount.toFixed(2);
}

if (qrAmount) {
    qrAmount.textContent = peso(amount);
}

updateButton();
    }

    if (progress) {
        tick();
        setInterval(tick, 1000);
    }

    /* ---------- GCash QR / Cash Counter switching ---------- */

    var radios = document.querySelectorAll(
        'input[name="method"]'
    );

    function showPaymentMethod() {

        var selected = document.querySelector(
            'input[name="method"]:checked'
        );

        var method = selected ? selected.value : '';

        var gcashPanel = $('fields-ewallet');
        var cashPanel = $('fields-cash');

        if (gcashPanel) {
            gcashPanel.hidden = method !== 'gcash';
        }

        if (cashPanel) {
            cashPanel.hidden = method !== 'cash';
        }

        updateButton();
    }

    if (radios.length) {

        radios.forEach(function (radio) {
            radio.addEventListener('change', showPaymentMethod);
        });

        showPaymentMethod();
    }

    /* ---------- auto-hide messages ---------- */

    var toasts = document.querySelectorAll('.toast');

    if (toasts.length) {

        setTimeout(function () {

            toasts.forEach(function (toast) {

                toast.classList.add('hide');

                setTimeout(function () {
                    toast.remove();
                }, 450);

            });

        }, 5000);
    }

})();
