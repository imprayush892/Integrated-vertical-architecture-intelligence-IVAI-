# IVAI UI primitives: rules for stage agents

This is a neumorphic "CNC hardware" kit for the Apartment Planner. The tokens live in `src/styles/tokens.css`, the primitive styles in `src/styles/ui.css`, and the shell in `src/styles/shell.css`. Import components from `../ui`:

```tsx
import { Panel, LatchButton, Channel, Readout, ProvenanceTag, StatusChip, Field, Select, Tabs, EmptyState } from "../ui";
```

## 1. Colour semantics (never mix them)

| Token | Meaning | Use for |
|---|---|---|
| `--det` verdigris | the deterministic rule engine (about 92% of the work) | rule-derived numbers, "run rules" actions, done stages |
| `--ai` amber | the Claude co-pilot (about 8%, always re-checked) | tickets T0-T8, "ask Claude", co-pilot text, waiting decisions |
| `--pass` / `--warn` / `--fail` | statutory DCPR 2034 / NBC 2016 results | check results only (`warn` = REVIEW) |
| `--cobalt` | neutral UI accent | selection, focus, the primary action, user input |

- **Text uses the `-ink` twin**: `--det-ink`, `--ai-ink`, `--pass-ink`, `--warn-ink`, `--fail-ink`, `--cobalt-ink`. These are at least 4.5:1 on the chassis in both themes. The plain LED colours (`--det`, `--ai`, ...) are for fills, pips, strokes and glows, not for small text.
- Body ink comes in three levels: `--ink-1` (primary), `--ink-2` (secondary) and `--ink-3` (muted, still 4.5:1 or better). `--ink-disabled` is only for disabled controls.
- Never hard-code hex values in panels. Use tokens so the dark theme works. Konva colours go through `cssColor("var(--token)")`. The shell remounts the canvases when the theme changes.

## 2. Surfaces (pick by physical meaning)

| Class / `Panel profile` | Looks like | Use for |
|---|---|---|
| `.neo-flat` / `flat` | extruded plinth | cards, docks, modal slabs, option groups |
| `.neo-well` / `well` (`.neo-well-deep`) | carved into the slab | viewports, lists that hold content, readout groups, inputs |
| `.neo-dome` / `dome` | convex pillow | un-pressed buttons, draggable pucks |
| `.neo-dish` / `dish` | concave bowl | sun-path and wind-rose charts, dials |

Nest at most one level: put a well inside a flat, or small flats inside a well. Do not stack flat-in-flat-in-flat. Add `.neo-sm` (or `small` on `Panel`) for nested surfaces.

## 3. Epistemic honesty

- Every number whose origin matters is a `Readout` (sunken LCD) and gets `by` + `kind`: `<Readout label="Buildable" value={a} unit="m²" tone="det" by="engine" kind="derived" source={clause} />`.
- `null`/`undefined`/`NaN` renders an unlit "–". Never invent a value.
- `ProvenanceTag by="engine"` renders an engraved RULE chip, `by="claude"` a backlit CO-PILOT chip, and `by="user"` a USER chip. The evidence `kind` is one of `observed | derived | inferred | unknown`.
- Text written by Claude uses `.backlit` (amber glow). Rule output can use `.engraved` / `.engraved.det`.
- Show statutory results only with `StatusChip status="pass|review|fail|na"`. The colour is always paired with the word.

## 4. Components

| Component | Notes |
|---|---|
| `Panel` | `profile`, `title`, `eyebrow`, `actions`, `pad`. Labels its region when titled. |
| `LatchButton` | `variant`: `default`, `primary` (one per panel), `det` (run or accept a rule step), `ai` (ask Claude or act on a ticket), `danger`, `ghost`. `latched` makes it a toggle (`aria-pressed`, sinks in, LED lights). `loading` shows a spinner. `sub` renders a tall trade-off tile (Keep/Undo, option A/B). |
| `IconButton` | `label` is required (aria-label + tooltip). Optional `pressed`, `badge`, `led`. |
| `Toggle` | `role="switch"`. `tone="ai"` for switches that hand work to Claude. |
| `Channel` / `Slider` | recessed parameter channel: label, hint, range and LCD. `onChange` fires live (preview) and `onCommit` fires on release (RPC). Keyboard: arrows, PageUp/PageDown, Home/End. |
| `Field`, `TextArea` | carved input with `label`, `hint`, `error`, `unit`. Pass `data-autofocus` for modals. |
| `Select` | native select in a well. `options` or `<option>` children. Use `compact` in headers. |
| `Readout` | LCD value, `tone`, `size`, `layout="stack"|"inline"`, provenance. |
| `Led` | `det | ai | pass | warn | fail | cobalt | idle`, `pulse`. Pair it with text or give it a `label`. |
| `StatusChip`, `Chip` | statutory chip / neutral chip with an LED. |
| `Tabs` | switch bank, `role="tablist"`, arrow keys, roving tabindex. |
| `Modal`, `Drawer` | portal, focus trap, Escape and scrim close, focus restore. |
| `ToastViewport` | already mounted by the shell. Call `useStore().toast(kind, text)`. |
| `Tooltip` | hover/focus wrapper around one focusable child. |
| `Progress`, `Skeleton`, `Spinner` | `Progress` without `value` is indeterminate (solver running). |
| `EmptyState` | always give the next step as an `action`. |
| `Kbd`, `Icon`, `Logo` | `Icon name="..."` covers a small stroke set. `Logo` is brand mark B. |

## 5. Shell contract

- `App.tsx` owns the layout: top bar, the **8-stage sequencer** (keys `1`-`8`, `[` / `]`; arrows plus Enter on the pads), the left **stage dock** (your panel renders inside `.side` in `StageDock`), the centre **canvas well** (`.stage`, absolutely-filled children), the right **inspector** (or the decision inbox when it is open) and the bottom **status bar**.
- Stage 6 has two steps (`units`, `program`). The dock shows a Split / Room lists switch, so panels no longer need "next" buttons for that hop, though they still work.
- Stage metadata (titles, rules/co-pilot split, engine and co-pilot roles) is in `src/shell/stages.ts`.
- Global keys ignore events from inputs, selects, textareas and open modals. Do not bind bare digits or `[` `]` elsewhere.
- Legacy classes (`btn`, `badge`, `card`, `kpi`, `field`, `option`, `ticket`, `check`, `modal`) are re-skinned in `styles.css`, so untouched code still looks right. When you restyle a panel, replace them with primitives and delete nothing global.

## 6. Accessibility checklist

- Everything is reachable by keyboard and focus is visible. The global `:focus-visible` rule draws a 2px cobalt outline, so do not remove it.
- Icon-only controls need `label` / `aria-label`.
- Never use colour alone. LEDs come with text, and chips carry words.
- Motion goes through `--dur-*` tokens. Animations and pulses stop under `prefers-reduced-motion`.
- Keep panel text at `--ink-2` or darker for body copy, and `--ink-3` only for small meta text.
