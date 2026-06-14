# MTGSite Design System Standard

Status: Active
Last Updated: 2026-06-11
Owner: UI/Frontend
Applies To: All UI templates, CSS, and UI-related PRs

## 1. Purpose

This document standardizes MTGSite aesthetics based on the current visual language.

Goals:

- Keep the product card-first, compact, readable, and consistent.
- Use the token system as the single source of visual values.
- Eliminate styling drift from inline CSS and hardcoded values.
- Define strict, objective review gates for UI PRs.

Non-goals:

- This is not a redesign.
- This does not introduce a new visual theme.
- This does not require immediate full migration of all legacy pages.

## 2. Source Of Truth

Visual standards derive from existing project assets:

- DESIGN_RULES.md
- static/css/foundation/tokens.css
- static/css/foundation/base.css
- static/css/foundation/layout.css
- static/css/components/*.css
- static/css/pages/*.css
- templates/base.html

When conflicts exist:

1. tokens.css
2. component CSS
3. page CSS
4. legacy CSS
5. template inline styles

## 3. Core Aesthetic Principles

1. Card-first hierarchy

   - Card art, card data, and collection status are the primary visual signal.
   - UI chrome supports the content and must remain restrained.
  
2. Compact density

   - Interfaces are optimized for large collections and fast scanning.
   - Spacing is tight but never cramped.

3. Semantic color discipline

   - Color communicates role (success, danger, warning, info) or page accent identity.
   - Decorative color usage is secondary and subtle.

4. Controlled variation

   - Special pages may vary layout, but not visual language.
   - Variants must still use tokens and approved component patterns.

5. Consistency over novelty

   - Reuse existing patterns before creating new ones.

## 4. Token Contract (Required)

All visual values MUST use tokens from static/css/foundation/tokens.css.

Required token categories:

- Surfaces and backgrounds: --site-bg, --site-surface-*
- Text: --site-text-*
- Borders/dividers: --site-border-*, --site-divider-*
- Accent/semantic colors: --site-accent-*, --site-success, --site-danger, --site-warning, --site-info
- Spacing: --site-space-*
- Radius: --site-radius-*
- Shadows: --site-shadow-*
- Typography: --site-font-*, --site-font-size-*, --site-line-height-*
- Transitions/z-index: --site-transition-*, --site-z-*

Forbidden:

- New hardcoded hex colors in CSS or templates.
- New hardcoded rgba values when equivalent token intent exists.
- Hardcoded spacing/radius/shadow/font-size values unless no token exists.

If a value is genuinely missing:

1. Add a new token in tokens.css.
2. Name it by role, not by page.
3. Use the token everywhere needed.

## 5. Component Contract

All reusable UI must use shared component classes in static/css/components.

### 5.1 Navigation and dropdowns

- Use `site-nav*` and `site-dropdown*` patterns.
- Do not create raw nav-bar/dropdown class families.

### 5.2 Buttons

- Use site-button variants and approved modifiers.
- Do not add one-off button class families for color-only differences.

### 5.3 Cards and panels

- Use site-card/site-panel plus accent modifiers.
- Accent should be applied via local accent variables and utility hooks.

### 5.4 Forms

- Use shared form control patterns (input/select/label/help/error states).
- Auth/admin/trade forms must not duplicate foundational input styles.

### 5.5 Tables

- Use shared table structure and spacing rules.
- No page-only table restyling unless truly domain-specific.

### 5.6 Modals

- Use standard modal shell and visibility patterns.
- Page-specific modal variants must be scoped and non-conflicting.

### 5.7 Search, status, empty states

- Reuse existing site-search patterns for filter/search surfaces.
- Use semantic status styles for owned/missing/warning/info states.
- Use shared empty-state pattern for no-data displays.

## 6. Naming Contract

Required naming conventions:

- Reusable classes: site-*
- Component blocks/elements/modifiers should be BEM-like and readable.
- Page-scoped additions should use a clear prefix tied to page context.

Forbidden naming:

- Generic raw class families: .btn, .card, .container, .dropdown for new work.
- New global selectors that style raw tags without namespace intent.

Allowed exceptions:

- Legacy selectors during migration phases only.
- Exceptions must be tracked in the migration register.

## 7. Accessibility Contract

Every UI change must satisfy:

- Keyboard focus is visible and usable.
- Forms have labels mapped to controls.
- Interactive icon-only controls have accessible labels.
- Color is not the only meaning signal for status.
- Contrast remains readable on dark surfaces.

## 8. Do And Do Not Patterns

Do:

- Use tokenized color, spacing, radius, and shadows.
- Reuse existing site-* components first.
- Keep per-page CSS in `static/css/pages/<page>.css`.
- Keep template markup clean and semantic.

Do not:

- Add new inline style attributes for static styling.
- Add new template block style sections for stable UI patterns.
- Add hardcoded colors in component or page CSS.
- Duplicate form/card/button systems by page.

## 9. Visual Pattern Examples (Textual)

These are implementation-aligned examples to guide consistency.

### Example A: Page hero with compact emphasis

- Surface: elevated dark panel from --site-surface-2.
- Accent: one top border accent from --site-page-accent.
- Typography: kicker uses label spacing token, title uses page-title scale.
- Actions: one primary accent action, one neutral secondary.
- Motion: optional subtle reveal using --site-transition-slow.

### Example B: Admin metric card row

- Layout: 3-4 cards in responsive grid using existing layout tokens.
- Card shell: shared card component with left accent modifier.
- Metric number: strong heading token.
- Secondary text: muted token.
- Status chip: semantic status utility, not custom color per card.

### Example C: Dense table with status context

- Table container uses shared panel shell.
- Header text uses label/uppercase convention where appropriate.
- Rows use subtle surface-hover highlight.
- Status cells use semantic success/danger/warning tokens plus text/icon.

### Example D: Search toolbar

- Shared search shell and control sizing tokens.
- Inputs maintain consistent height and spacing.
- Search/clear actions use approved icon-button styles.
- Focus and hover effects use tokenized glow and border transitions.

## 10. Migration Roadmap

## Phase A: Canon And Audit (short)

Scope:

- Confirm canonical patterns and forbidden practices.
- Build divergence map of legacy styles.

Output:

- Approved token/component contract (this document).
- Hotspot list and migration queue.

## Phase B: Template Style Extraction (parallel track)

Primary targets:

- templates/admin_dashboard.html
- templates/admin.html
- templates/trade_page.html
- templates/login.html
- templates/register.html
- templates/card_adder.html
- templates/edh_gallery.html
- templates/edh_detail.html

Actions:

- Move stable style blocks into static/css/pages/*.css or components.
- Replace hardcoded values with tokens.
- Normalize selectors to naming contract.

## Phase C: Component Consolidation (parallel track)

Primary targets:

- forms.css
- tables.css
- modals.css
- empty-state/status patterns

Actions:

- Consolidate duplicated page-level patterns into shared components.
- Remove overlapping modal/table/form definitions.

## Phase D: Legacy Stylesheet Retirement (depends on B and C)

Primary target:

- static/css/main.css

Actions:

- Verify equivalent standardized coverage exists.
- Remove dependency from templates/base.html only after parity checks.
- Validate no regression in nav, search, card-grid, and common controls.

## Phase E: Lockdown

Actions:

- Enforce strict PR gates for all new/modified UI code.
- Maintain migration register for explicitly approved temporary exceptions.

## 11. Definition Of Done By Phase

Phase B done when:

- No new inline style attributes in touched templates.
- No new block style sections for stable patterns.
- Touched styles tokenized and moved to CSS files.

Phase C done when:

- Shared form/table/modal patterns exist and are reused.
- Duplicated page-only patterns are reduced or removed.

Phase D done when:

- main.css can be disabled with no material visual regression.
- Critical pages pass visual checks.

Phase E done when:

- PR gate checklist is consistently enforced.
- Exception list trends toward zero.

## 12. Strict PR Checklist (Merge Gate)

A UI PR fails if any item below is unmet.

1. No new inline style attributes for static styling.
2. No new hardcoded colors in templates or CSS.
3. No new hardcoded spacing/radius/shadow/font-size where tokens exist.
4. New reusable UI uses site-* component patterns.
5. New styles live in component/page CSS, not template style blocks.
6. Naming follows required contract.
7. Accessibility contract is met (focus, labels, semantics, color redundancy).
8. PR description includes:

- Which tokens were used.
- Which shared components were reused or extended.
- Any exception request with rationale and planned removal phase.

## 13. Legacy Exception Register

Use this table only for temporary migration exceptions.

| Area | File | Exception | Reason | Planned Removal Phase | Owner |
| --- | --- | --- | --- | --- | --- |
| Legacy base styling | static/css/main.css | Legacy selector overlap | Migration in progress | Phase D | UI/Frontend |
| Admin inline styles | templates/admin_dashboard.html | Embedded style block | Not yet extracted | Phase B | UI/Frontend |

Add entries only when strictly required. Remove entries as work is completed.

## 14. Reviewer Workflow

1. Run checklist against changed files.
2. Confirm tokenization and component reuse.
3. Confirm no banned inline/hardcoded patterns were introduced.
4. Confirm accessibility requirements in UI behavior.
5. Approve only if all merge gates pass or a documented exception exists.

## 15. Adoption Policy

Effective immediately:

- Strict enforcement applies to all new or modified UI code.
- Legacy code may remain temporarily only if listed in the exception register.
- Any non-compliant additions without registered exception must be rejected.
