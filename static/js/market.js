document.addEventListener('DOMContentLoaded', () => {
    const syncButton = document.getElementById('market-sync-button');
    const progressContainer = document.getElementById('market-progress-container');
    const progressBar = document.getElementById('market-progress-bar');
    const progressStatus = document.getElementById('market-progress-status');
    const filterButtons = document.querySelectorAll('[data-market-filter]');
    const sortSelect = document.getElementById('market-sort');

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
});