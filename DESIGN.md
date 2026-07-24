# Sherlock Design System

Single-file system: all styles live in `static/index.html` (`login.html` carries a
small mirror of the tokens). Three cascade layers, **in order** — base styles →
theme override block → `/* control consistency */` block (must stay **last**;
it wins by cascade, not specificity).

## Tokens

| Token | Value | Use |
|---|---|---|
| `--brand` / `--brand-deep` | `#00549C` / `#013F78` | Prayaan royal blue — CTAs, active nav, focus rings. `--violet` is a **legacy alias** of `--brand`; don't use it in new code |
| `--ok` / `--ok-bg` | `#2FBF71` | success/positive. `--green` aliases `--ok` |
| `--warn` / `--warn-bg` | `#F2B418` | attention/pending |
| `--danger` / `--danger-bg` | `#D3342C` / `#FDECEB` | errors, LIVE-mode warnings |
| `--navy`, `--ink`, `--muted`, `--faint` | | text hierarchy |
| `--dash`, `--line` | | dashed/solid hairlines |
| `--card`, `--tint`, `--shadow` | | surfaces |
| gold `#f0a800` | | brand accent (nav active underline, logo) |

Rules: **no new hex for brand/status colors** — use the tokens (they work in inline
`style=""` too: `color:var(--danger)`). `#e96a5a` coral is reserved for gradients/
recall outlines only.

## Components

**Buttons** — `.btn` + `-sm` + `-primary|-ghost`. States: hover, active, disabled,
`:focus-visible` (global brand outline). Icon-only rows use `.btn-ico` in `.rowacts`.

**Chips** — `.chip` + one class:
- *Semantic aliases (preferred for generic states):* `ok` `warn` `bad` `neutral`
  (`yes`/`no` are legacy equivalents of `ok`/`neutral`).
- *Domain statuses (reserved, UPPERCASE):* `RETRIEVED FAILED CAPPED PROCESSING
  PULLING INITIATED SUFFICIENT INSUFFICIENT INDETERMINATE PENDING` + buckets
  `COMFORT WATCH SHORTFALL NO_DATA` + sources `LOS PCPL`.
  Never use a domain status as a generic color.

**Forms** — two intentional contexts:
- `.field` (tint background, radius 12): data-entry in **modals**.
- `.aalive-form` (white, dashed border, DM Mono): **tool-tab** consoles (Live Pull,
  Portfolio Sync, Presentment, Consents). Despite the name it is the shared
  tool-tab kit — same for `.aalive-modebar`, `.aalive-step`, `.aalive-actions`.
- All `<select>`s get the global custom chevron automatically. Error state:
  add class `error`.

**Tables** — every data table is `.aa-tbl` (or bare) **inside `.txnwrap`**
(`overflow-x:auto`); the page body must never scroll horizontally.

**Nav** — `.navpill` scrolls internally; `.navmore`/`.navmenu` = the Tools dropdown
(menu is `position:fixed`, reparented to `<body>` on open — the glass nav's
backdrop-filter breaks fixed positioning otherwise). Logo pill = home button.

**Pre-flight checklist** — `.plwrap` > `.plhead` + `.plrow` (chip + `.plbody` + action).

**Toolbar rows** (`.cycle-head`, `.aalive-modebar`, `.funnel`, `.data-head`) wrap
via `flex-wrap` instead of widening the page. Stat cards auto-fit ≥150px.

## Accessibility
- `:focus-visible` brand outline on all interactive elements.
- Dropdown menus carry `aria-haspopup/aria-expanded`; nav has `aria-label`.
- `[hidden]{display:none!important}` global guard (flex containers ignore
  `hidden` otherwise).

## Conventions
- Preview caches `index.html` aggressively — verify with `?nocache=N`.
- New UI: copy an existing component family; put overrides in the consistency
  block only.
