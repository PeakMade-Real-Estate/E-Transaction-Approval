/**
 * E-Transaction Approval Dashboard — Prototype JS
 * =================================================
 * Client-side behavior for intake form, dashboard, and detail view.
 * No real API calls or data persistence — prototype only.
 */

'use strict';

// ── Request Type Helper Text ──────────────────────────────────
const REQUEST_TYPE_HELP = {
    'Wire':                'Same-day transaction if released prior to the bank cutoff time.',
    'ACH':                 'Approximately two-day transaction if released prior to the bank cutoff time.',
    'ACH Pull':            'Originates from the receiving bank. Use only if ACH/Wire permissions are not set up in the originating bank.',
    'Intra Bank Transfer': 'Used for internal bank transfers. For SD True Ups, accountants may move funds directly.',
    'EFT':                 'Canada-specific. EFT is the Canadian equivalent of ACH.',
};

// ── Approval Tier Logic ───────────────────────────────────────
function getApprovalTier(amount) {
    if (amount > 1_000_000) return { tier: 'CFO (Additional Approval Required)', level: 'cfo',        icon: 'fa-star',       color: 'danger'  };
    if (amount > 500_000)   return { tier: 'Vice President',                     level: 'vp',         icon: 'fa-user-tie',   color: 'warning' };
    if (amount > 250_000)   return { tier: 'Controller',                         level: 'controller', icon: 'fa-user-check', color: 'primary' };
    return { tier: 'Senior Accounting Manager / Assistant Controller',           level: 'sam',        icon: 'fa-user',       color: 'success' };
}

function formatCurrency(amount) {
    return '$' + amount.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}

// ── Intake Form: Request Type Change ─────────────────────────
function onRequestTypeChange() {
    const select = document.getElementById('request_type');
    const helpEl = document.getElementById('request-type-help');
    if (!select || !helpEl) return;
    const val = select.value;
    if (val && REQUEST_TYPE_HELP[val]) {
        helpEl.textContent = REQUEST_TYPE_HELP[val];
        helpEl.style.display = 'block';
    } else {
        helpEl.style.display = 'none';
    }
}

// ── Intake Form: Urgent Toggle ────────────────────────────────
function onUrgentChange() {
    const radios = document.querySelectorAll('input[name="urgent"]');
    const urgencyRow = document.getElementById('urgency-reason-row');
    const urgencyInput = document.getElementById('urgency_reason');
    if (!urgencyRow) return;
    let val = '';
    radios.forEach(r => { if (r.checked) val = r.value; });
    if (val === 'yes') {
        urgencyRow.style.display = 'block';
        if (urgencyInput) urgencyInput.required = true;
    } else {
        urgencyRow.style.display = 'none';
        if (urgencyInput) urgencyInput.required = false;
    }
}

// ── Intake Form: Amount → Tier Card Update ────────────────────
function onAmountChange() {
    const amountInput = document.getElementById('amount');
    if (!amountInput) return;
    const amount = parseFloat(amountInput.value) || 0;
    const info = getApprovalTier(amount);

    // ── Inline tier card (visible on all screen sizes) ──
    const inlineTierCard = document.getElementById('approval-tier-card');
    if (inlineTierCard) {
        inlineTierCard.style.display = amount > 0 ? 'block' : 'none';
        inlineTierCard.className = `card tier-card-${info.level} mb-0`;
        const tierLabel = document.getElementById('tier-label');
        const tierIcon  = document.getElementById('tier-icon');
        const tierBadge = document.getElementById('tier-badge');
        if (tierLabel) tierLabel.textContent = info.tier;
        if (tierIcon)  tierIcon.className = `fas ${info.icon} me-2`;
        if (tierBadge) tierBadge.className = `badge tier-badge-${info.level} fs-6 px-3 py-2`;
    }

    // ── Sidebar tier card ──
    const sidebarNoAmount = document.getElementById('sidebar-no-amount');
    const sidebarDisplay  = document.getElementById('sidebar-tier-display');
    if (sidebarNoAmount && sidebarDisplay) {
        if (amount > 0) {
            sidebarNoAmount.style.display = 'none';
            sidebarDisplay.style.display  = 'block';
        } else {
            sidebarNoAmount.style.display = 'block';
            sidebarDisplay.style.display  = 'none';
        }
        const sidebarAmount = document.getElementById('sidebar-amount-display');
        const sidebarBadge  = document.getElementById('sidebar-tier-badge');
        const sidebarCFO    = document.getElementById('sidebar-cfo-warning');
        if (sidebarAmount) sidebarAmount.textContent = formatCurrency(amount);
        if (sidebarBadge) {
            sidebarBadge.textContent = info.tier;
            sidebarBadge.className   = `badge tier-badge-${info.level} fs-6 px-3 py-2 mb-2 d-block`;
        }
        if (sidebarCFO) sidebarCFO.style.display = amount > 1_000_000 ? 'block' : 'none';
    }

    // ── Warning alerts ──
    const cfoWarning    = document.getElementById('cfo-warning');
    const vpNotice      = document.getElementById('vp-release-notice');
    const execNotice    = document.getElementById('exec-approval-notice');
    if (cfoWarning)  cfoWarning.style.display  = amount > 1_000_000 ? 'block' : 'none';
    if (vpNotice)    vpNotice.style.display    = amount > 500_000   ? 'block' : 'none';
    if (execNotice)  execNotice.style.display  = amount > 1_000_000 ? 'inline' : 'none';

    // ── Tier guidance table row highlight ──
    ['sam', 'controller', 'vp', 'cfo'].forEach(level => {
        const row = document.getElementById('tier-row-' + level);
        if (row) {
            row.classList.remove('table-active', 'fw-bold', 'table-warning');
        }
    });
    if (amount > 0) {
        const activeRow = document.getElementById('tier-row-' + info.level);
        if (activeRow) activeRow.classList.add('table-active', 'fw-bold');
    }
}

// ── Intake Form: Last Used Date ───────────────────────────────
function onLastUsedDateChange() {
    const input  = document.getElementById('last_used_date');
    const notice = document.getElementById('new-instructions-notice');
    if (!input || !notice) return;
    notice.style.display = (!input.value || input.value.trim() === '') ? 'block' : 'none';
}

// ── Masked Account Toggle (Request Detail) ────────────────────
function toggleMask(fieldId) {
    const masked   = document.getElementById(fieldId + '-masked');
    const unmasked = document.getElementById(fieldId + '-unmasked');
    const btn      = document.getElementById(fieldId + '-toggle');
    if (!masked || !unmasked) return;

    const isCurrentlyMasked = masked.style.display !== 'none';
    if (isCurrentlyMasked) {
        masked.style.display   = 'none';
        unmasked.style.display = 'inline';
        if (btn) btn.innerHTML = '<i class="fas fa-eye-slash me-1"></i>Hide';
    } else {
        masked.style.display   = 'inline';
        unmasked.style.display = 'none';
        if (btn) btn.innerHTML = '<i class="fas fa-eye me-1"></i>Reveal';
    }
}

// ── Action Button Confirmation ────────────────────────────────
function confirmAction(label, requestId) {
    return confirm(
        `Confirm action: "${label}" for request ${requestId}?\n\n` +
        'This is a prototype — no real workflow will be triggered.'
    );
}

// ── Dashboard: Clear All Filters ─────────────────────────────
function clearFilters() {
    const form = document.getElementById('filter-form');
    if (!form) return;
    form.querySelectorAll('input, select').forEach(el => {
        if (el.type === 'checkbox') el.checked = false;
        else el.value = '';
    });
    form.submit();
}

// ── Initialize ────────────────────────────────────────────────
document.addEventListener('DOMContentLoaded', function () {

    // Intake form wiring
    const reqType = document.getElementById('request_type');
    if (reqType) {
        reqType.addEventListener('change', onRequestTypeChange);
        onRequestTypeChange();
    }

    document.querySelectorAll('input[name="urgent"]').forEach(r => {
        r.addEventListener('change', onUrgentChange);
    });
    onUrgentChange();

    const amountEl = document.getElementById('amount');
    if (amountEl) {
        amountEl.addEventListener('input', onAmountChange);
        onAmountChange();
    }

    const lastUsedEl = document.getElementById('last_used_date');
    if (lastUsedEl) {
        lastUsedEl.addEventListener('change', onLastUsedDateChange);
        lastUsedEl.addEventListener('blur', onLastUsedDateChange);
        onLastUsedDateChange();
    }

    // Bootstrap tooltips
    document.querySelectorAll('[data-bs-toggle="tooltip"]').forEach(el => {
        new bootstrap.Tooltip(el);
    });

    // Auto-dismiss flash alerts after 7 seconds
    document.querySelectorAll('.alert.alert-dismissible.auto-dismiss').forEach(alertEl => {
        setTimeout(() => {
            const bsAlert = bootstrap.Alert.getOrCreateInstance(alertEl);
            if (bsAlert) bsAlert.close();
        }, 7000);
    });
});
