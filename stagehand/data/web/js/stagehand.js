// Stagehand web client: API access, background job polling, notifications,
// and small UI helpers.  No dependencies.
'use strict';

const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];

/** Runs fn once the DOM is ready (immediately if it already is). */
function ready(fn) {
    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', fn, { once: true });
    } else {
        fn();
    }
}

/** Escapes text for safe inclusion in HTML. */
function escapeHTML(text) {
    const div = document.createElement('div');
    div.textContent = text ?? '';
    return div.innerHTML;
}

/** Returns the text content of an HTML fragment (e.g. provider overviews). */
function stripTags(html) {
    return new DOMParser().parseFromString(html ?? '', 'text/html').body.textContent;
}

class APIError extends Error {
    constructor(message, status = 0) {
        super(message);
        this.status = status;
    }
}

/**
 * Client for the Stagehand API.
 *
 * Long-running API calls respond with a job id; the result is delivered
 * later via /api/jobs, which this client polls.  Polling also delivers
 * server-side notifications (alerts, download progress), which are
 * dispatched to handlers registered with on().
 */
class Stagehand {
    constructor(root) {
        this.root = root;
        this.jobs = new Map();  // job id -> {resolve, reject}
        this.handlers = {};
        this.minInterval = 5000;
        this.maxInterval = 10000;
        this.interval = null;
        this.timer = null;
        this.inflight = false;
        this.requested = null;
        this.poll(this.minInterval);
    }

    /** Registers a handler for notifications of the given type. */
    on(ntype, handler) {
        (this.handlers[ntype] ??= []).push(handler);
    }

    /**
     * Calls an API endpoint and resolves with its result.
     *
     * @param {string} url - path relative to the application root
     * @param {object} [options]
     * @param {object} [options.data] - query parameters (GET) or form body
     * @param {string} [options.method='GET']
     * @param {function} [options.onPending] - called if the result will
     *        arrive asynchronously (i.e. the job is still running)
     */
    async api(url, { data = null, method = 'GET', onPending = null } = {}) {
        method = method.toUpperCase();
        let target = this.root + url;
        const init = { method, headers: { Accept: 'application/json' } };
        if (data && method === 'GET') {
            target += (target.includes('?') ? '&' : '?') + new URLSearchParams(data);
        } else if (data) {
            init.body = new URLSearchParams(data);
        }

        let response;
        try {
            response = await fetch(target, init);
        } catch (e) {
            throw new APIError(`Network error: ${e.message}`);
        }
        if (!response.ok) {
            throw new APIError(`HTTP ${response.status}: ${response.statusText}`, response.status);
        }
        const body = await response.json();
        if (body.jobid == null) {
            return body;
        }

        const result = new Promise((resolve, reject) => this.jobs.set(body.jobid, { resolve, reject }));
        if (body.pending) {
            onPending?.(body.jobid);
            this.poll(body.interval);
        }
        this.handleResponse(body);
        return result;
    }

    /** Settles finished jobs and dispatches notifications. */
    handleResponse({ jobs = [], notifications = [] }) {
        for (const job of jobs) {
            const pending = this.jobs.get(job.id);
            if (!pending) {
                continue;
            }
            this.jobs.delete(job.id);
            if (job.error) {
                pending.reject(new APIError(job.error.message));
            } else {
                pending.resolve(job.result);
            }
        }
        for (const n of notifications) {
            for (const handler of this.handlers[n._ntype] ?? []) {
                handler(n);
            }
            if (n._ntype === 'alert') {
                // Alerts are plain text unless the server explicitly sends HTML.
                toast({
                    title: n.title,
                    text: n.text,
                    html: n.html?.replaceAll('{{root}}', this.root),
                    type: n.type ?? 'notice',
                    delay: n.delay ?? 8000,
                });
            }
        }
    }

    /**
     * Polls /api/jobs every interval ms.  If already polling, the interval
     * only changes when the new one is shorter (or force is true).
     */
    poll(interval, force = false) {
        interval = Math.min(interval, this.maxInterval);
        if (this.inflight) {
            // A request is underway; it will schedule the next one.
            this.requested = Math.min(this.requested ?? interval, interval);
            return;
        }
        if (this.timer && !force && interval >= this.interval) {
            return;
        }
        clearTimeout(this.timer);
        this.interval = interval;
        this.timer = setTimeout(() => this._tick(), interval);
    }

    async _tick() {
        this.timer = null;
        this.inflight = true;
        this.requested = null;
        let next = this.maxInterval;
        try {
            const params = this.jobs.size ? '?' + new URLSearchParams({ jobs: [...this.jobs.keys()].join(',') }) : '';
            const response = await fetch(`${this.root}/api/jobs${params}`, {
                signal: AbortSignal.timeout(Math.max(this.interval, 5000)),
            });
            if (response.ok) {
                const body = await response.json();
                this.handleResponse(body);
                // Back off while idle, speed up while things are happening.
                next = (this.jobs.size === 0 && body.notifications.length === 0)
                    ? this.interval * 2
                    : Math.min(this.interval, 1000);
            }
        } catch {
            // Server unreachable; keep polling slowly.
        } finally {
            this.inflight = false;
        }
        this.poll(Math.min(next, this.requested ?? Infinity), true);
    }
}

// ---------------------------------------------------------------------------
// Notifications
// ---------------------------------------------------------------------------

/**
 * Shows a notification in the top-right corner.
 *
 * Returns a handle with update(options) and close() methods.  If sticky or
 * busy is true, the notification stays until updated or closed.
 */
function toast(options) {
    let container = $('#toasts');
    if (!container) {
        container = document.createElement('div');
        container.id = 'toasts';
        container.setAttribute('aria-live', 'polite');
        document.body.append(container);
    }
    const el = document.createElement('div');
    el.className = 'toast';
    el.setAttribute('role', 'status');
    el.innerHTML = '<button type="button" class="toast-close" aria-label="Close">×</button>' +
                   '<div class="toast-title"></div><div class="toast-body"></div>';
    container.append(el);

    let timer = null;
    const handle = {
        el,
        close() {
            clearTimeout(timer);
            el.classList.add('closing');
            setTimeout(() => el.remove(), 200);
        },
        update(opts) {
            options = { ...options, ...opts };
            const { title = '', text, html, type = 'notice', sticky = false, busy = false, delay = 8000 } = options;
            el.dataset.type = type;
            el.classList.toggle('busy', busy);
            $('.toast-title', el).textContent = title;
            const body = $('.toast-body', el);
            if (html !== undefined) {
                body.innerHTML = html;
            } else {
                body.textContent = text ?? '';
            }
            $('.toast-close', el).hidden = busy;
            clearTimeout(timer);
            if (!sticky && !busy) {
                timer = setTimeout(() => handle.close(), delay);
            }
            return handle;
        },
    };
    $('.toast-close', el).addEventListener('click', () => handle.close());
    return handle.update({});
}

// ---------------------------------------------------------------------------
// Tooltips
// ---------------------------------------------------------------------------

let tipElement = null;
let tipOwner = null;

/**
 * Shows a tooltip next to the target element.
 *
 * @param {Element} target
 * @param {string} html - tooltip contents
 * @param {string} [placement='right'] - 'right' or 'below'
 */
function showTip(target, html, placement = 'right') {
    if (!tipElement) {
        tipElement = document.createElement('div');
        tipElement.className = 'tip';
        tipElement.setAttribute('role', 'tooltip');
        document.body.append(tipElement);
    }
    tipOwner = target;
    tipElement.innerHTML = html;
    tipElement.dataset.placement = placement;
    tipElement.hidden = false;
    const rect = target.getBoundingClientRect();
    const tip = tipElement.getBoundingClientRect();
    let left, top;
    if (placement === 'below') {
        left = rect.left + rect.width / 2 - tip.width / 2;
        top = rect.bottom + 8;
    } else {
        left = rect.right + 10;
        top = rect.top + rect.height / 2 - tip.height / 2;
    }
    left = Math.max(4, Math.min(left, document.documentElement.clientWidth - tip.width - 4));
    tipElement.style.left = `${left + window.scrollX}px`;
    tipElement.style.top = `${top + window.scrollY}px`;
}

function hideTip(target = null) {
    if (tipElement && (target === null || target === tipOwner)) {
        tipElement.hidden = true;
        tipOwner = null;
    }
}

/**
 * Attaches a tooltip to each element, shown on hover (or focus).
 *
 * @param {Element[]} elements
 * @param {function(Element): string} content - returns tooltip HTML
 */
function tooltip(elements, content, { placement = 'right', on = 'hover', delay = 150 } = {}) {
    for (const el of elements) {
        let timer = null;
        const show = () => {
            clearTimeout(timer);
            timer = setTimeout(() => showTip(el, content(el), placement), delay);
        };
        const hide = () => {
            clearTimeout(timer);
            hideTip(el);
        };
        if (on === 'focus') {
            el.addEventListener('focus', show);
            el.addEventListener('blur', hide);
        } else {
            el.addEventListener('mouseenter', show);
            el.addEventListener('mouseleave', hide);
        }
    }
}

// ---------------------------------------------------------------------------
// Misc UI helpers
// ---------------------------------------------------------------------------

/**
 * Asks the user to confirm an action with a modal dialog.  Resolves true if
 * confirmed.
 */
function confirmDialog({ title, text, confirm = 'OK', cancel = 'Cancel' }) {
    const dialog = document.createElement('dialog');
    dialog.className = 'confirm';
    dialog.innerHTML = '<h2></h2><p></p><form method="dialog"><button value="cancel"></button>' +
                       '<button value="confirm" class="danger"></button></form>';
    $('h2', dialog).textContent = title;
    $('p', dialog).textContent = text;
    const [cancelButton, confirmButton] = $$('button', dialog);
    cancelButton.textContent = cancel;
    confirmButton.textContent = confirm;
    document.body.append(dialog);
    return new Promise(resolve => {
        dialog.addEventListener('close', () => {
            resolve(dialog.returnValue === 'confirm');
            dialog.remove();
        });
        dialog.showModal();
        cancelButton.focus();
    });
}

/** Enables or disables all form controls within an element. */
function setDisabled(section, disabled) {
    section.classList.toggle('disabled', disabled);
    for (const control of $$('input, select, button, textarea', section)) {
        control.disabled = disabled;
    }
}

/**
 * Makes a table sortable by clicking its column headings.  Rows are compared
 * by their text, numerically when both values are numbers.
 */
function sortableTable(table) {
    const headings = $$('thead th', table);
    headings.forEach((th, column) => {
        th.classList.add('sortable');
        th.tabIndex = 0;
        const sort = () => {
            const ascending = th.getAttribute('aria-sort') !== 'ascending';
            headings.forEach(h => h.removeAttribute('aria-sort'));
            th.setAttribute('aria-sort', ascending ? 'ascending' : 'descending');
            const tbody = table.tBodies[0];
            const key = row => row.cells[column]?.textContent.trim() ?? '';
            const rows = [...tbody.rows].sort((a, b) => {
                const [x, y] = [key(a), key(b)];
                const cmp = (isNaN(x) || isNaN(y) || x === '' || y === '')
                    ? x.localeCompare(y, undefined, { sensitivity: 'base' })
                    : Number(x) - Number(y);
                return ascending ? cmp : -cmp;
            });
            tbody.append(...rows);
        };
        th.addEventListener('click', sort);
        th.addEventListener('keydown', e => (e.key === 'Enter' || e.key === ' ') && (e.preventDefault(), sort()));
    });
}

/** Makes every row with a data-href attribute navigate there when clicked. */
function linkRows(root = document) {
    for (const row of $$('tr[data-href]', root)) {
        row.addEventListener('click', event => {
            if (!event.target.closest('input, a, button')) {
                window.location = row.dataset.href;
            }
        });
    }
}
