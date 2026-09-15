# MTGSite dev bug review

Reviewed base: `c710838fe79fd22e1834c035eb220e008034a393` (`dev`). Working fixes: `review/dev-bug-fixes`.

22 grouped findings have fixes in the accompanying patch. The most urgent are trade integrity, deck-upload path handling, owner-data permissions, and database integrity. This was a source review plus isolated SQLite/Flask regression testing, not a production penetration test or complete browser audit.

## Implemented fixes

P1: data integrity, authorization, arbitrary file writes, or unusable database paths. P2: functional failures or incorrect results. Source links point to the reviewed commit, before the fixes.

### B01 · P1 · Trade acceptance can run repeatedly or “succeed” for a nonexistent trade.

Source: [routes/admin.py:566](https://github.com/NicholasBradford/MTGSite/blob/c710838fe79fd22e1834c035eb220e008034a393/routes/admin.py#L566).

**Reproduction / cause:** Accept the same Pending trade twice, or accept an unknown ID. The original route never reads its status.

**Implemented fix:** Require an existing Pending trade and use BEGIN IMMEDIATE so concurrent resolutions serialize; return 404/409 appropriately.

### B02 · P1 · Invalid quantities and insufficient outgoing stock can corrupt trade inventory.

Source: [routes/admin.py:566](https://github.com/NicholasBradford/MTGSite/blob/c710838fe79fd22e1834c035eb220e008034a393/routes/admin.py#L566).

**Reproduction / cause:** A negative LIMIT can delete every matching copy; requesting three copies when only two exist still completes the trade. Duplicate lines can over-request the same stock.

**Implemented fix:** Validate positive integer quantities on submission and acceptance. Aggregate duplicate lines, check all stock before changes, and exclude deck-assigned copies. A failed check leaves the entire trade untouched.

### B03 · P1 · Incoming-card lookups can commit a half-written trade.

Source: [routes/trade_binder.py:142](https://github.com/NicholasBradford/MTGSite/blob/c710838fe79fd22e1834c035eb220e008034a393/routes/trade_binder.py#L142).

**Reproduction / cause:** The route inserts the trade before calling ScryfallFetcher, which commits internally. An unsuccessful fetch is ignored.

**Implemented fix:** Verify incoming printing IDs before inserting any trade rows; use cached metadata when present; persist the trade and its items together.

### B04 · P1 · Deck upload filenames can overwrite arbitrary writable files.

Source: [routes/edh.py:173](https://github.com/NicholasBradford/MTGSite/blob/c710838fe79fd22e1834c035eb220e008034a393/routes/edh.py#L173).

**Reproduction / cause:** Upload an absolute filename or a ../ path. os.path.join accepts it, and file.save writes there. Failed imports also leave uploads behind.

**Implemented fix:** Save to a fixed filename inside a unique TemporaryDirectory; always clean up. Regression test proves an existing file remains unchanged.

### B05 · P1 · Failed deck imports can leave partial decks behind.

Source: [routes/edh.py:96](https://github.com/NicholasBradford/MTGSite/blob/c710838fe79fd22e1834c035eb220e008034a393/routes/edh.py#L96).

**Reproduction / cause:** A missing-card fetch commits earlier deck writes; unresolved cards are silently omitted.

**Implemented fix:** Resolve and validate all cards first, then write the deck and card rows. Abort an unresolved deck without saving partial deck records. Successfully fetched catalog metadata may remain cached.

### B06 · P1 · Ordinary registered users can edit the owner’s wishlist and assign inventory to decks.

Source: [routes/edh.py:509](https://github.com/NicholasBradford/MTGSite/blob/c710838fe79fd22e1834c035eb220e008034a393/routes/edh.py#L509).

**Reproduction / cause:** POST /edh/assign_card/... and /api/wishlist/update/... require login but lack the admin checks used by the other owner-data mutations.

**Implemented fix:** Require admin on both endpoints and align shared card controls with that policy. Deck assignment also clears tradeability.

### B07 · P1 · Foreign keys are disabled on ordinary database connections.

Source: [db/db_manager.py:16](https://github.com/NicholasBradford/MTGSite/blob/c710838fe79fd22e1834c035eb220e008034a393/db/db_manager.py#L16).

**Reproduction / cause:** Only create_tables enables PRAGMA foreign_keys, but request-scoped connections do not call create_tables. Invalid references are accepted.

**Implemented fix:** Enable foreign keys on every connection. Defer checks while renumbering a location and its inventory rows; validate edited locations and permit explicit unassignment.

### B08 · P1 · Schema creation does not upgrade older databases; fresh planeswalker pages also fail.

Source: [db/db_manager.py:296](https://github.com/NicholasBradford/MTGSite/blob/c710838fe79fd22e1834c035eb220e008034a393/db/db_manager.py#L296).

**Reproduction / cause:** Legacy wishlist tables lack non_specific. Fresh planeswalker_tracker lacks sort_index even though /collection/planeswalkers orders by it.

**Implemented fix:** Add idempotent column migrations for both fields. Test repeated initialization and preservation of an existing wishlist row.

### B09 · P2 · Imports and accepted trades default to a nonexistent location.

Source: [services/card_fetcher.py:26](https://github.com/NicholasBradford/MTGSite/blob/c710838fe79fd22e1834c035eb220e008034a393/services/card_fetcher.py#L26).

**Reproduction / cause:** The schema seeds location 1, but imports and accepted trades use 5. This creates invisible/orphaned copies, or fails once foreign keys are enforced.

**Implemented fix:** Use initialized location 1 for defaults and accepted incoming cards; preserve explicitly selected import locations.

### B10 · P2 · Valid wishlist and location searches raise SQL errors.

Source: [services/search.py:4](https://github.com/NicholasBradford/MTGSite/blob/c710838fe79fd22e1834c035eb220e008034a393/services/search.py#L4).

**Reproduction / cause:** /wishlist?q=usd>1, sort:usd, sort:added, or sort:location reference absent aliases i or l. Inventory sort:location also references missing l.

**Implemented fix:** Give search an explicit wishlist source; use wishlist finish/added columns and a correlated location lookup instead of nonexistent joins.

### B11 · P2 · Search negation, quoted phrases, and malformed numeric filters behave incorrectly.

Source: [services/search.py:234](https://github.com/NicholasBradford/MTGSite/blob/c710838fe79fd22e1834c035eb220e008034a393/services/search.py#L234).

**Reproduction / cause:** -usd>1 applies the positive comparison. Quoted card names include literal quotes in LIKE. usd:.. raises ValueError and qty><2 generates invalid SQL.

**Implemented fix:** Strip phrase delimiters, invert numeric operators for negation, support validated quantity comparisons, and ignore malformed numeric filters safely. Etched/rainbow prices use the foil bucket in these searches.

### B12 · P2 · Sorting ignores request parameters and partially sorts identity by card color.

Source: [services/search.py:73](https://github.com/NicholasBradford/MTGSite/blob/c710838fe79fd22e1834c035eb220e008034a393/services/search.py#L73).

**Reproduction / cause:** Inventory calculates active_sort but never uses it. The identity SQL replacement misses IN/NOT LIKE/REPLACE references to color.

**Implemented fix:** Read the sort request parameter in the shared parser, retain explicit sort: overrides, and substitute the complete color identifier when building identity sorting.

### B13 · P2 · Copies assigned to decks disappear from the instance editor.

Source: [routes/inventory.py:200](https://github.com/NicholasBradford/MTGSite/blob/c710838fe79fd22e1834c035eb220e008034a393/routes/inventory.py#L200).

**Reproduction / cause:** Deck assignment sets location_id to NULL. get_instances uses an inner join to locations and omits those copies.

**Implemented fix:** Use a left join, expose an Unassigned option, and recognize the current grid tile when tracking the edited card.

### B14 · P2 · Bulk CSV imports ignore tradeability and per-row locations.

Source: [services/card_fetcher.py:870](https://github.com/NicholasBradford/MTGSite/blob/c710838fe79fd22e1834c035eb220e008034a393/services/card_fetcher.py#L870).

**Reproduction / cause:** The downloadable template includes tradeable and location, but the live importer never reads either field.

**Implemented fix:** Read truthy tradeability values and explicit numeric/existing named locations. Unrecognized location names continue to use the chosen default; legacy Master/Bulk routing is not reintroduced.

### B15 · P2 · One malformed CSV quantity aborts the file; zero-copy imports can appear successful.

Source: [services/card_fetcher.py:918](https://github.com/NicholasBradford/MTGSite/blob/c710838fe79fd22e1834c035eb220e008034a393/services/card_fetcher.py#L918).

**Reproduction / cause:** All rows are parsed before importing, without per-row error handling. range(0) or range(-1) writes nothing but counts as a successful import.

**Implemented fix:** Collect invalid-row failures and continue valid rows; reject nonpositive quantities before writing; roll back failed owned-card writes.

### B16 · P2 · Import failures are only printed to the server console.

Source: [routes/card_adder.py:30](https://github.com/NicholasBradford/MTGSite/blob/c710838fe79fd22e1834c035eb220e008034a393/routes/card_adder.py#L30).

**Reproduction / cause:** The single and bulk routes redirect after errors without telling the person using the form. UTF-8 BOM headers also fail recognition.

**Implemented fix:** Show flash error/result messages, accept UTF-8 BOM CSVs, and tolerate invalid pagination input on the adder.

### B17 · P2 · 404 and 500 handlers return HTTP 200, and both show a 404 page.

Source: [app.py:115](https://github.com/NicholasBradford/MTGSite/blob/c710838fe79fd22e1834c035eb220e008034a393/app.py#L115).

**Reproduction / cause:** Request an unknown path or invoke the 500 handler; render_template alone defaults to status 200.

**Implemented fix:** Return actual 404/500 statuses and add a distinct server-error page.

### B18 · P2 · Price refresh cannot find its fetch script with the default configuration.

Source: [services/tcgcsv_prices.py:34](https://github.com/NicholasBradford/MTGSite/blob/c710838fe79fd22e1834c035eb220e008034a393/services/tcgcsv_prices.py#L34).

**Reproduction / cause:** The default is _get_tcgcsv.py in the working directory, but the tracked script is services/_get_tcgcsv.py. .env.example masks this only when its override is configured.

**Implemented fix:** Resolve the default relative to the service module; retain the environment override.

### B19 · P2 · Structurally invalid settings can break every request.

Source: [services/app_settings.py:69](https://github.com/NicholasBradford/MTGSite/blob/c710838fe79fd22e1834c035eb220e008034a393/services/app_settings.py#L69).

**Reproduction / cause:** Valid JSON such as [] or {"features": null} bypasses the JSONDecodeError catch and breaks merging/access; string "false" is truthy.

**Implemented fix:** Validate known settings sections and value types; keep the last valid configuration on invalid input/read failures.

### B20 · P2 · Market price-quality checks misclassify the supported etched finish.

Source: [routes/markets.py:38](https://github.com/NicholasBradford/MTGSite/blob/c710838fe79fd22e1834c035eb220e008034a393/routes/markets.py#L38).

**Reproduction / cause:** The market predicate recognizes etched foil but omits etched, which the importer and trade routes accept.

**Implemented fix:** Include etched in the market foil-like predicate. This does not redesign the shared pricing buckets for different premium finishes.

### B21 · P2 · Card images and public image clicks are broken in some views.

Source: [templates/trade_page.html:53](https://github.com/NicholasBradford/MTGSite/blob/c710838fe79fd22e1834c035eb220e008034a393/templates/trade_page.html#L53).

**Reproduction / cause:** The trade page builds browser URLs from IMAGE_PATH, which may be an absolute server path. Public cards call showPopup, which is not defined in the repository.

**Implemented fix:** Use the Flask image-serving URL, preserve remote image URLs, provide a consistent default image directory, and open public card art through a defined browser action.

### B22 · P2 · Deck detail and name resolution have avoidable failure cases.

Source: [routes/edh.py:21](https://github.com/NicholasBradford/MTGSite/blob/c710838fe79fd22e1834c035eb220e008034a393/routes/edh.py#L21).

**Reproduction / cause:** An unrecognized card type selects Other, but that category is absent. Name lookup embeds names directly in a query string and has no timeout.

**Implemented fix:** Initialize Other; send the exact name as a requests params value and set a finite timeout.

## Validation

- Original non-browser suite: **78 passed, 6 failed, 4 browser tests deselected**.
- First regression batch on the original code: **26 failed, 2 passed**. After fixes, that same batch passed.
- Final focused regression file: **44 passed**. It covers database upgrades, foreign-key enforcement, trade validation and rollback, permissions, unsafe filenames, search errors, imports, settings, and rendered image URLs.
- Full final suite: **122 passed, 6 failed, 4 browser tests deselected**. The six failures are the same named failures as the original baseline.
- `git diff --check`: clean. Python modules compiled successfully (see verification notes in the bundle).
- Tests ran with Python 3.12, Flask 3.1.3, Flask-Login 0.6.3, Flask-WTF 1.3.0, and pytest 9.1.1. The repository’s entire requirements.txt was not installed.
- Browser tests, live Scryfall/TCGCSV correctness, production data migration, and multi-worker load testing were not performed. The original suite has some inadequately mocked network paths; those tests are not evidence of live integration correctness.

## Six pre-existing test failures

| Tests | Cause | Recommended correction |
| --- | --- | --- |
| Three tests in `test_conftest_boot.py` | They request `/inventory` as a guest but assert 200, while the access tests correctly assert a login redirect. | Use `auth_client` for the three content/render assertions; retain guest redirect tests. |
| `test_trade_binder_requires_login` | Expects login, but the route and existing guest/AJAX tests support public viewing. The trade page explicitly offers guest viewing followed by login to submit. | Decide and document binder visibility. For the current public behavior, expect 200 and verify guests cannot mutate data. If private is intended, add login_required and update the other guest tests. |
| Two price-refresh resilience tests | Mock `stream_refresh_daily_price_snapshot_if_needed`, but the current route calls `refresh_current_day_history_csv_if_due`. | Mock the active refresh helper, snapshot resolver, and local-card targets. Assert the reported snapshot date/fallback and actual writes rather than only a 200/redirect. |

These tests were left unchanged so the patch does not silently choose a conflicting access policy or manufacture a green baseline.

## Additional findings and recommended follow-up fixes

These are source-level findings outside the implemented patch. They should remain on the backlog; the patch is not a claim that the repository is bug-free.

| Priority | Finding | Concrete proposed fix |
| --- | --- | --- |
| P1 | `/run-price-update` performs writes through GET. CSRF protection does not cover GET, and the browser starts the operation with EventSource. | Start jobs through an admin-only, CSRF-protected POST. Make GET stream only the status of an existing job, or consume a POST response stream with fetch. Test that a GET cannot start a job. |
| P2 | `static/js/market.js` treats any progress=100 event as success, including `TCGCSV sync failed` and missing-data warnings. | Add a structured outcome to SSE payloads and branch on success/warning/error. Only reload on success; keep failures visible and re-enable retry. |
| P2 | Registration accepts duplicate/blank usernames and arbitrary role strings. `users.username` has no uniqueness constraint. | Validate nonempty credentials and allowlist roles; resolve existing duplicates, then add a unique username index and handle conflicts. Serialize first-admin bootstrapping in local-app mode. |
| P2 | Premium finishes are not consistently represented across pricing, import, trade, and rendering. The database has only current_price/current_price_foil although overrides are keyed by finish. | Introduce one canonical finish registry and per-printing/per-finish current-price storage, migrate existing values, and join by normalized finish in every valuation query. Until then, avoid treating distinct premium finishes as independently priced. |
| P2 | Deleting the last owned copy of a set also deletes the `sets` catalog row, even if printings, wishlist entries, or decks still use that metadata. | Remove the catalog deletion from `delete_card`; derive owned-set visibility from inventory queries. Add a regression that the last-copy deletion preserves set metadata. |
| P2 | Two classes named CardImporterService exist. Live routes use `services.card_fetcher`, while `tests/test_card_importer.py` exercises `services.card_importer`; legacy Master/Bulk routing therefore is not validated on the live route. | Choose one service API, migrate callers and tests together, then retire the other implementation. Add route-level tests that assert actual inserted copies, destinations, and finish values. |
| P2 | `requirements.txt` is a broad UTF-16 environment export and includes `logging==0.4.9.6`, although logging is a standard-library module. Clean environment installation was not validated. | Replace the export with application runtime dependencies and a separate development/test dependency file; verify installation in clean CI. Keep historical lock information separately if needed. |

## Attached database-manager file

The supplied `02-db_manager.py` is older than the reviewed dev branch. Its missing `self.db_path` assignment and empty-locations `fetchone()[0]` crash are already fixed on dev. I did not overwrite the newer repository module with the attachment. The upgrade regression deliberately represents the older wishlist schema.

## Applying and reviewing

The patch is based on the exact commit above and contains code, the new 500 template, this report, and regression tests. No changes were pushed, merged, or deployed.

```bash
git switch dev
git switch -c review/dev-bug-fixes
git apply --check /path/to/MTGSite-dev-fixes.patch
git apply /path/to/MTGSite-dev-fixes.patch
python -m pytest tests/test_review_regressions.py -q
python -m pytest -q -m "not browser"
```

Before applying to the live database, take a SQLite backup and run `PRAGMA foreign_key_check;` on a copy. Per-connection foreign-key enforcement is intentional, but existing orphan references need explicit cleanup; the patch does not guess which production rows to delete or relocate. Back up with SQLite’s backup mechanism or while the app is stopped, including any necessary WAL state. Test a copy through startup and the import/trade flows before deployment.

An ordinary reverse code patch does not remove newly added schema columns. They are additive and retain data. Restore the database backup if a full database rollback is required.
