document.addEventListener('DOMContentLoaded', () => {
    const syncButton = document.getElementById('market-sync-button');
    const progressContainer = document.getElementById('market-progress-container');
    const progressBar = document.getElementById('market-progress-bar');
    const progressStatus = document.getElementById('market-progress-status');
    const filterButtons = document.querySelectorAll('[data-market-filter]');
    const sortSelect = document.getElementById('market-sort');
    const deferredSectionsContainer = document.getElementById('market-deferred-sections');

    function setQueryParam(key, value) {
        const url = new URL(window.location.href);

        if (value && value !== 'all') {
            url.searchParams.set(key, value);
        } else {
            url.searchParams.delete(key);
        }

        window.location.href = url.toString();
    }

    function markActiveFilterFromUrl() {
        const params = new URLSearchParams(window.location.search);
        const activeFilter = params.get('market_filter') || 'all';

        filterButtons.forEach(button => {
            button.classList.toggle(
                'is-active',
                button.dataset.marketFilter === activeFilter
            );
        });
    }

    function markActiveSortFromUrl() {
        if (!sortSelect) {
            return;
        }

        const params = new URLSearchParams(window.location.search);
        const activeSort = params.get('market_sort');

        if (activeSort) {
            sortSelect.value = activeSort;
        }
    }

    function setSyncStatus(status, message) {
        if (!progressStatus) {
            return;
        }

        progressStatus.dataset.status = status;
        progressStatus.innerText = message;
    }

    function startPriceUpdate() {
        if (!syncButton || !progressContainer || !progressBar || !progressStatus) {
            return;
        }

        const syncUrl = syncButton.dataset.syncUrl;

        if (!syncUrl) {
            setSyncStatus('error', 'Missing sync endpoint.');
            return;
        }

        syncButton.disabled = true;
        progressContainer.hidden = false;
        progressBar.value = 0;

        setSyncStatus('active', 'Connecting to market server...');

        const source = new EventSource(syncUrl);

        source.onmessage = event => {
            let data;

            try {
                data = JSON.parse(event.data);
            } catch (error) {
                console.error('Invalid market sync payload:', error);
                setSyncStatus('error', 'Received an invalid sync response.');
                syncButton.disabled = false;
                source.close();
                return;
            }

            progressBar.value = data.progress ?? 0;
            setSyncStatus('active', data.status || 'Updating market prices...');

            if (data.progress === 100) {
                source.close();
                setSyncStatus('success', 'Sync complete. Refreshing page...');

                setTimeout(() => {
                    window.location.reload();
                }, 1000);
            }
        };

        source.onerror = error => {
            console.error('EventSource failed:', error);
            setSyncStatus('error', 'An error occurred during the update.');
            syncButton.disabled = false;
            source.close();
        };
    }

    async function loadDeferredSections() {
        if (!deferredSectionsContainer) {
            return;
        }

        const deferredUrl = deferredSectionsContainer.dataset.deferredUrl;

        if (!deferredUrl) {
            return;
        }

        try {
            const requestUrl = new URL(deferredUrl, window.location.origin);
            const currentParams = new URLSearchParams(window.location.search);

            currentParams.delete('defer_sections');
            requestUrl.search = currentParams.toString();

            const response = await fetch(requestUrl.toString(), {
                headers: {
                    'X-Requested-With': 'XMLHttpRequest',
                },
                credentials: 'same-origin',
            });

            if (!response.ok) {
                throw new Error(`Failed to load deferred sections (${response.status}).`);
            }

            deferredSectionsContainer.innerHTML = await response.text();
        } catch (error) {
            console.error('Deferred market sections failed to load:', error);
            deferredSectionsContainer.innerHTML = `
                <section class="site-card site-card--left-accent site-accent-red site-market-section">
                    <div class="site-empty-state">
                        <strong>Could not load additional sections.</strong>
                        <span>Refresh the page or open the full dashboard with defer_sections=0.</span>
                    </div>
                </section>
            `;
        }
    }

    syncButton?.addEventListener('click', startPriceUpdate);

    filterButtons.forEach(button => {
        button.addEventListener('click', () => {
            setQueryParam('market_filter', button.dataset.marketFilter);
        });
    });

    sortSelect?.addEventListener('change', () => {
        setQueryParam('market_sort', sortSelect.value);
    });

    markActiveFilterFromUrl();
    markActiveSortFromUrl();
    loadDeferredSections();
});