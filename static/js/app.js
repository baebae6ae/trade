/* ── 공통 유틸리티 ────────────────────────────────────── */

function formatCurrency(value) {
    if (value === null || value === undefined) return '₩0';
    const abs = Math.abs(value);
    let str;
    if (abs >= 1e8) {
        str = (value / 1e8).toFixed(2) + '억';
    } else if (abs >= 1e4) {
        str = (value / 1e4).toFixed(1) + '만';
    } else {
        str = value.toLocaleString('ko-KR', { maximumFractionDigits: 0 });
    }
    return (value >= 0 ? '₩' : '-₩') + str.replace('-', '');
}

function eventIcon(type) {
    const icons = {
        entry: '🔵',
        exit: '🔴',
        trail_armed: '🟢',
        breakeven: '🟣',
        scale_in: '🟡',
        blocked: '⛔',
    };
    return icons[type] || '⚪';
}

async function fetchJson(url, data) {
    try {
        const res = await fetch(url, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(data),
        });
        return await res.json();
    } catch (err) {
        return { error: err.message };
    }
}

function showToast(title, message, type = 'info') {
    const toastEl = document.getElementById('toastAlert');
    const toastTitle = document.getElementById('toastTitle');
    const toastBody = document.getElementById('toastBody');
    const toastIcon = document.getElementById('toastIcon');

    toastTitle.textContent = title;
    toastBody.textContent = message;

    const iconMap = {
        success: 'bi-check-circle text-success',
        danger: 'bi-x-circle text-danger',
        warning: 'bi-exclamation-triangle text-warning',
        info: 'bi-info-circle text-info',
    };
    toastIcon.className = `bi ${iconMap[type] || iconMap.info} me-2`;

    const toast = bootstrap.Toast.getOrCreateInstance(toastEl, { delay: 4000 });
    toast.show();
}
