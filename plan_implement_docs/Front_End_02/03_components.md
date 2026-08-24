# Front End 02 · the component inventory

**Status: settled 2026-08-24.** One contract per component. Build the four in
§2 first — everything else composes them.

Every component reads tokens through `var(--…)`. **A component that contains
a literal hex is a bug**, with one exception noted in `SubMarker`.

## 1. The inventory

| Component | Props | From the prototype | Notes |
|---|---|---|---|
| `Button` | `variant: primary \| secondary \| tertiary \| destructive`, `size`, `disabled`, `disabledReason?` | Sidebar upload button, header actions, library actions | `disabledReason` renders as a tooltip — a disabled control must always say why |
| `Icon` | `name: IconName`, `size` | The eight SVGs | §3. No other icons exist |
| `Label` | children | `.lbl` | 11px/700 uppercase `0.07em`, `--muted-2` |
| `StateChip` | `state: ComplianceState \| "neutral"`, `label?` | Criterion row, Overall tile, library column | §2 |
| `SubMarker` | `status: SubRequirementStatus` | Sub-requirement grid | §2 |
| `QuoteCard` | `quote: ResolvedQuote`, `tone?: "analysis" \| "chat"` | Criterion body, chat citations | §2 |
| `MetricTile` | `label`, `value: ReactNode` | The four done-state tiles | Value slot takes a `StateChip` for Overall — which is why all four are one component |
| `Select` | `label`, `value`, `options`, `onChange`, `help?` | Chat settings row | §2 |
| `Tooltip` | `content`, children | Retrieval and Depth labels, disabled buttons | Hover **and** focus; Escape dismisses |
| `Avatar` | `role: "user" \| "assistant"` | Chat transcript | 30px circle, accent fill / `--nav-sel` |
| `ProgressBar` | `value?` (omit for indeterminate) | Upload, analysis, retrieval trail | Indeterminate variant honours `prefers-reduced-motion` |
| `Disclosure` | `open`, `onToggle`, `header`, children | Criterion row | §2 — the header is a slot, which is the whole point |
| `Banner` | `tone: error \| warn \| info`, `title`, `hint?`, `action?` | `no_api_key`, failed, cancelled | `01_ui_spec.md` §4 |
| `EmptyState` | `icon`, `title`, `body`, `action?` | No analysis yet, no documents, not found | Centred on a card |
| `Dialog` | `open`, `onClose`, `title`, children, `destructive?` | Delete confirmation | Focus trap, Escape, restore focus on close |
| `Card` | `padding?`, `interactive?` | Every white surface | `--surface` + `--border-card` + 6px. **Ubiquitous by design** — see `01_ui_spec.md` §2.2 |

Views (`Upload/`, `Library/`, `Analysis/`, `Chat/`) are composition and data
wiring only. A view containing a styled `<div>` that should have been a
component is how the token discipline erodes.

## 2. The four that carry the design

### `QuoteCard`

The component the product is about. Build it first and get it right.

```ts
interface ResolvedQuote {
  text: string;
  section_ref: string;
  page_display: string;
  chunk_id: number | null;
  evidence_id: string;
  verified: boolean;
}
```

* `--surface`, `--border-card`, **3px `--rule-quote` left rule**, radius
  `0 6px 6px 0`.
* Quote text in the serif, 15px/1.55, wrapped in typographic quotes.
* Meta line, 12px `--muted-2`: `§ {section_ref} · p. {page_display} · verified`.
* **`verified: false` is a different card**: the meta line reads
  **"not found verbatim — check the source"** in `#8A6108`, and the left rule
  takes the same amber. This is the hallucination-detection story made
  visible; it must not look like a clean quote.
* One card serves the analysis report *and* the chat citations — which is why
  `05_api_deltas.md` §3 asks the two endpoints to agree on field names. If
  that delta is refused, the shim lives here and nowhere else.

### `Disclosure` (the criterion row)

The primary scanning surface of the product, and the thing the Streamlit
build lost (`02_architecture.md` §1). The contract exists to prevent that
happening again: **the header is a slot**, so it can hold the full cluster.

```
[chevron]  N · Title ..................  k of n met   conf 0.95   [StateChip]
```

* Header is a `<button>` with `aria-expanded` and `aria-controls`; the
  chevron rotates 90° on open.
* All four data points live in the header and are visible while collapsed.
  Moving any of them inside is a functional regression.
* `needs_review` adds a marker beside the confidence.
* Row is a `Card`; the header takes a bottom divider only while open.

### `StateChip`

* 12px/600, radius 3px, padding `4px 10px`, `white-space: nowrap`.
* **The words are always in it.** A chip with its text removed is a bug, not
  a compact variant. Colour is looked up from a table keyed by the state, so
  a value the API invents cannot become CSS.
* Four entries: the three states plus `neutral` (queued, not analysed).

### `Select`

* Label + optional `help` info mark; bordered value box with a chevron;
  absolutely-positioned menu with the current option highlighted.
* Keyboard: Enter/Space opens, arrows move, Enter commits, Escape closes and
  restores focus to the trigger. Click-outside closes.
* `help` renders a `Tooltip` on the label — the pattern Retrieval and Depth
  use, and the reason `Tooltip` must respond to focus as well as hover.
* This is the component most likely to justify adopting a Radix primitive
  (`02_architecture.md` §2). Ship the hand-built one; swap it if the keyboard
  behaviour fights back.

## 3. The icon set

Eight, stroke-only, 1.5–2.0 stroke weight on a 24px grid, `currentColor`.
Adding a ninth is a design decision, not an implementation one.

| Name | Where |
|---|---|
| `upload` | Drop zone |
| `chevron` | Select, disclosure, sidebar library row — rotated, never redrawn |
| `document-check` | Assistant avatar |
| `person` | User avatar |
| `trash` | Library delete |
| `info` | `Select` help mark |
| `document-lines` | Empty states |
| `send` | Chat input |

## 4. Tokens

`tokens.css` is a transcription of `01_ui_spec.md` §2, one custom property
per row, and is the only file in the front end containing a literal colour.

```css
:root {
  --canvas: #FAF8F4;  --sidebar: #F2EEE6;  --surface: #FFFFFF;
  --surface-sel: #FBF8F2;  --nav-sel: #E5DCCC;
  --border: #E0D9CC;  --border-card: #E5DDD0;  --border-ctl: #D6CDBD;
  --divider: #EFE9DE;  --rule-quote: #C8A88C;
  --ink: #23201B;  --ink-body: #3A342B;  --ink-2: #4A443A;
  --muted: #6E665A;  --muted-2: #787061;  --hairline: #9A9082;
  --accent: #7A3B2E;  --accent-hover: #5C2B21;  --on-accent: #FDF9F3;
  --fc-fg: #2F6B4F;  --fc-bg: #EEF5F0;  --fc-br: #B9D3C4;
  --pc-fg: #8A6108;  --pc-bg: #FBF3E3;  --pc-br: #E4D0A6;
  --nc-fg: #8F2E2E;  --nc-bg: #FAEDEC;  --nc-br: #E3BFBB;
  --neutral-fg: #787061;  --neutral-bg: #F4F0E8;  --neutral-br: #E0D9CC;
  --tooltip-bg: #2B2721;  --tooltip-fg: #F5F1E9;
  --serif: "Source Serif 4", Georgia, serif;
  --sans: "Source Sans 3", system-ui, sans-serif;
  --r-lg: 8px;  --r-md: 6px;  --r-sm: 5px;  --r-chip: 3px;
  --gap-block: 22px;  --gap-group: 10px;
}
```

`SubMarker`'s `partial` gradient is the one place a hex may appear inline —
`linear-gradient(135deg, var(--pc-marker) 50%, var(--surface) 50%)` works, so
prefer that; if the gradient needs a literal for any reason, it is the single
documented exception.

**Adding a token requires measuring it** against `--canvas` *and*
`--surface` per `01_ui_spec.md` §2.3. The two that failed that check are
already fixed there; do not reintroduce them.

## 5. Porting from the prototype

`design/Main.dc.html` is HTML with inline styles and a small state object.
The port is mechanical:

1. Find the block in the prototype (the file is sectioned by comment banners:
   sidebar, upload, library, analysis, chat).
2. Lift the markup into TSX; the structure carries over unchanged.
3. Replace each inline `style="…"` with a CSS Module class whose values are
   `var(--…)` lookups from the table above.
4. Replace the prototype's `renderVals()` derivations with props and hooks.

Two things not to port:

* **The prototype's data is baked in.** `CRITERIA`, `DOCS`, `BASE_TURNS` and
  `SUGGESTIONS` are real output used as fixtures. They belong in
  `ui/test/fixtures/`, not in a component.
* **The prototype has no error, gap or streaming states.** Those are
  specified in `01_ui_spec.md` §4 and §5 and have no prototype markup to
  copy. Build them from the spec.

## 6. Testing

Three pure modules carry real logic and are unit-tested; components are not,
beyond a smoke render.

| Test | Asserts |
|---|---|
| `sse.test.ts` | Frames split mid-chunk reassemble; `event:`/`data:` parsed; an `error` event terminates cleanly; a truncated stream does not hang |
| `errors.test.ts` | Every code in `01_ui_spec.md` §4 maps to a surface; an unknown code falls back to the generic inline error rather than throwing |
| `depth.test.ts` | `medium` equals `settings.retrieval_top_k`; the mapping is total over the three options |

The gap fixture from `06_build_and_ship.md` §5 is what `QuoteCard`,
`SubMarker` and `Disclosure` are visually checked against. A green-only
screenshot proves nothing.
