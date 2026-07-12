# DESIGN — Basecamp 5 design tokens (light + dark)

Extracted **2026-07-04** from the live Basecamp 5 web app (`app.basecamp.com`),
stylesheet `desktop-37307d5f….css` served from `bc3-production-assets-cdn.basecamp-static.com`.
Every CSS custom property declared at `:root` level was pulled by parsing the
stylesheet's rule tree — 370 base (light) tokens, 124 dark-mode overrides, and
5 mobile overrides. This is the canonical token reference for our clone; values
below are verbatim from production.

## 1. Theming architecture

- All tokens live on `:root`. **Light is the default** — there is no explicit
  light override block.
- `<html>` carries `data-color-scheme` ∈ `light | dark | none` (a Stimulus
  `color-scheme` controller persists the user's choice; `none` = follow OS).
- Dark mode is applied by **two identical blocks** (verified byte-for-byte):
  1. `:root[data-color-scheme="dark"]` — explicit user choice
  2. `@media (prefers-color-scheme: dark)` → `:root:not([data-color-scheme="light"])` — OS dark, unless user forced light
- Both dark blocks are wrapped in `@media not print` — **print is always light**.
- Root font size is 10px (`--text-root: 10px`), so **1rem = 10px** everywhere.
  Tokens like `--16px: 1.6rem` encode that convention.
- Dark mode mostly works by **inverting the color ramps in place** (step 10 =
  darkest surface, step 70 = lightest text) so semantic aliases
  (`--color-ink: var(--color-ink-70)` etc.) don't change between modes.

## 2. Color primitives

`--hsl-black: 0, 0%, 0%` · `--hsl-white: 0, 0%, 100%` (raw triplets for alpha composition; not theme-swapped)

### Ink (neutral gray-blue — the workhorse text/border ramp)

| Token | Light | Dark |
|---|---|---|
| `--color-ink-10` | `hsl(240, 7.7%, 97.5%)` | `hsl(200, 18%, 19.6%)` |
| `--color-ink-20` | `hsl(200, 11.1%, 94.7%)` | `hsl(200, 14.3%, 24.7%)` |
| `--color-ink-30` | `hsl(180, 3.7%, 89.4%)` | `hsl(198.8, 10.1%, 31%)` |
| `--color-ink-40` | `hsl(195, 3.6%, 78%)` | `hsl(200, 7.6%, 38.6%)` |
| `--color-ink-50` | `hsl(202.5, 4.1%, 62%)` | `hsl(198.5, 5.5%, 53.5%)` |
| `--color-ink-60` | `hsl(201.4, 6%, 45.5%)` | `hsl(201.8, 6.7%, 68%)` |
| `--color-ink-70` | `hsl(202.1, 18.8%, 19.8%)` | `hsl(205.7, 13.2%, 89.6%)` |

### Sand (warm neutral)

| Token | Light | Dark |
|---|---|---|
| `--color-sand-10` | `hsl(33.3, 60%, 97.1%)` | `hsl(34.3, 7.1%, 19.4%)` |
| `--color-sand-20` | `hsl(34.3, 46.7%, 94.1%)` | `hsl(34.3, 5.6%, 24.5%)` |
| `--color-sand-30` | `hsl(31.4, 36.8%, 88.8%)` | `hsl(33, 13%, 30.2%)` |
| `--color-sand-40` | `hsl(31.6, 31.7%, 76.5%)` | `hsl(33.7, 21.9%, 36.7%)` |
| `--color-sand-50` | `hsl(32.9, 29.5%, 58.8%)` | `hsl(33.1, 23%, 50.6%)` |
| `--color-sand-60` | `hsl(33.8, 29.1%, 43.1%)` | `hsl(32.2, 30.7%, 65.5%)` |
| `--color-sand-70` | `hsl(34.1, 29.8%, 33.5%)` | `hsl(33.3, 25.7%, 79.4%)` |

### Slate (cool neutral — alias of ink in light, real ramp in dark)

| Token | Light | Dark |
|---|---|---|
| `--color-slate-10` | `var(--color-ink-10)` | `hsl(199.3, 28.6%, 19.2%)` |
| `--color-slate-20` | `var(--color-ink-20)` | `hsl(200, 24.2%, 24.3%)` |
| `--color-slate-30` | `var(--color-ink-30)` | `hsl(198.5, 16.7%, 30.6%)` |
| `--color-slate-40` | `var(--color-ink-40)` | `hsl(199, 21.2%, 37.8%)` |
| `--color-slate-50` | `var(--color-ink-50)` | `hsl(200.4, 23.1%, 52.5%)` |
| `--color-slate-60` | `var(--color-ink-60)` | `hsl(200.4, 31.7%, 67.3%)` |
| `--color-slate-70` | `var(--color-ink-70)` | `hsl(200, 41.2%, 80%)` |

### Red

| Token | Light | Dark |
|---|---|---|
| `--color-red-10` | `hsl(10.9, 100%, 97.8%)` | `hsl(6.7, 8.6%, 20.6%)` |
| `--color-red-20` | `hsl(10.9, 100%, 95.7%)` | `hsl(9, 15.2%, 25.9%)` |
| `--color-red-30` | `hsl(12, 100%, 91.2%)` | `hsl(7, 25.4%, 33.1%)` |
| `--color-red-40` | `hsl(12.7, 100%, 80.6%)` | `hsl(7.7, 40.6%, 41.6%)` |
| `--color-red-50` | `hsl(13, 97.9%, 62.9%)` | `hsl(8.9, 66.8%, 56.3%)` |
| `--color-red-60` | `hsl(7.8, 63.6%, 49.6%)` | `hsl(14.4, 93.6%, 69.4%)` |
| `--color-red-70` | `hsl(6.7, 69.1%, 38%)` | `hsl(15.2, 91.2%, 82.2%)` |

### Orange

| Token | Light | Dark |
|---|---|---|
| `--color-orange-10` | `hsl(40, 100%, 95.9%)` | `hsl(34.3, 14.3%, 19.2%)` |
| `--color-orange-20` | `hsl(38.3, 100%, 90.8%)` | `hsl(33.1, 24.4%, 23.3%)` |
| `--color-orange-30` | `hsl(36.5, 100%, 84.5%)` | `hsl(28.3, 35.6%, 29.2%)` |
| `--color-orange-40` | `hsl(32.9, 93.5%, 69.8%)` | `hsl(28.3, 60.9%, 34.1%)` |
| `--color-orange-50` | `hsl(34.6, 98.4%, 48.4%)` | `hsl(28.9, 78.4%, 47.3%)` |
| `--color-orange-60` | `hsl(29.4, 98.9%, 36.9%)` | `hsl(30, 82.7%, 59.2%)` |
| `--color-orange-70` | `hsl(26.4, 98.7%, 29.4%)` | `hsl(28.9, 93.3%, 76.5%)` |

### Yellow

| Token | Light | Dark |
|---|---|---|
| `--color-yellow-10` | `hsl(51.4, 77.8%, 94.7%)` | `hsl(49.1, 11.6%, 18.6%)` |
| `--color-yellow-20` | `hsl(48.4, 93.4%, 88%)` | `hsl(36, 21%, 23.3%)` |
| `--color-yellow-30` | `hsl(48.1, 100%, 77.3%)` | `hsl(38.8, 36.7%, 27.3%)` |
| `--color-yellow-40` | `hsl(45.7, 92.8%, 67.3%)` | `hsl(47.3, 85.3%, 65.3%)` |
| `--color-yellow-50` | `hsl(46, 87.9%, 48.6%)` | `hsl(40.7, 60.3%, 50.6%)` |
| `--color-yellow-60` | `hsl(42.2, 96.2%, 31%)` | `hsl(43.9, 76.2%, 58.8%)` |
| `--color-yellow-70` | `hsl(39.1, 82.4%, 26.7%)` | `hsl(45.4, 73.7%, 67.3%)` |

### Lime

| Token | Light | Dark |
|---|---|---|
| `--color-lime-10` | `hsl(65.5, 73.3%, 94.1%)` | `hsl(126, 10.2%, 19.2%)` |
| `--color-lime-20` | `hsl(62.7, 71.4%, 87.6%)` | `hsl(110, 9.8%, 23.9%)` |
| `--color-lime-30` | `hsl(62.3, 66.4%, 76.7%)` | `hsl(83.1, 18.3%, 27.8%)` |
| `--color-lime-40` | `hsl(61.7, 52.7%, 60.2%)` | `hsl(72, 31.2%, 31.4%)` |
| `--color-lime-50` | `hsl(60.4, 62.2%, 42.5%)` | `hsl(60.6, 44.1%, 41.4%)` |
| `--color-lime-60` | `hsl(60.5, 100%, 23.9%)` | `hsl(61.8, 40.6%, 51.2%)` |
| `--color-lime-70` | `hsl(60.7, 78.2%, 21.6%)` | `hsl(62.4, 54.3%, 63.9%)` |

### Green

| Token | Light | Dark |
|---|---|---|
| `--color-green-10` | `hsl(102, 55.6%, 96.5%)` | `hsl(172.9, 18.3%, 18.2%)` |
| `--color-green-20` | `hsl(105, 55.6%, 92.9%)` | `hsl(166.4, 19%, 22.7%)` |
| `--color-green-30` | `hsl(110.5, 54.3%, 86.3%)` | `hsl(160.6, 23.9%, 27.8%)` |
| `--color-green-40` | `hsl(123, 44.1%, 73.3%)` | `hsl(152.3, 30.2%, 33.7%)` |
| `--color-green-50` | `hsl(135.6, 39.7%, 52.5%)` | `hsl(143.4, 42.6%, 43.7%)` |
| `--color-green-60` | `hsl(148.4, 68.8%, 31.4%)` | `hsl(133.5, 40%, 60.8%)` |
| `--color-green-70` | `hsl(149.3, 68.2%, 25.9%)` | `hsl(122.6, 39.7%, 77.3%)` |

### Aqua

| Token | Light | Dark |
|---|---|---|
| `--color-aqua-10` | `hsl(174, 55.6%, 96.5%)` | `hsl(193.8, 27.7%, 18.4%)` |
| `--color-aqua-20` | `hsl(177.3, 57.9%, 92.5%)` | `hsl(192.4, 24.4%, 23.3%)` |
| `--color-aqua-30` | `hsl(178.8, 62%, 84.5%)` | `hsl(190.8, 35.2%, 27.8%)` |
| `--color-aqua-40` | `hsl(183.5, 55.1%, 69.4%)` | `hsl(189.4, 46.1%, 32.7%)` |
| `--color-aqua-50` | `hsl(184.7, 87.4%, 40.4%)` | `hsl(187.7, 63.5%, 40.8%)` |
| `--color-aqua-60` | `hsl(186.7, 98.6%, 28.4%)` | `hsl(185.8, 52.3%, 57.3%)` |
| `--color-aqua-70` | `hsl(188.5, 79.1%, 26.3%)` | `hsl(182.9, 47.7%, 74.5%)` |

### Blue

| Token | Light | Dark |
|---|---|---|
| `--color-blue-10` | `hsl(201.4, 100%, 97.3%)` | `hsl(204.7, 34%, 19.6%)` |
| `--color-blue-20` | `hsl(205, 92.3%, 94.9%)` | `hsl(209.4, 35.9%, 25.7%)` |
| `--color-blue-30` | `hsl(205.9, 100%, 90%)` | `hsl(208.7, 41.1%, 32%)` |
| `--color-blue-40` | `hsl(208.4, 88.6%, 79.4%)` | `hsl(210, 53.9%, 40%)` |
| `--color-blue-50` | `hsl(209.2, 100%, 62.9%)` | `hsl(212.1, 77.5%, 56.5%)` |
| `--color-blue-60` | `hsl(211.2, 71.4%, 48%)` | `hsl(210.2, 97.4%, 70.4%)` |
| `--color-blue-70` | `hsl(213.3, 67.2%, 39.4%)` | `hsl(208.6, 91.3%, 82%)` |

### Violet

| Token | Light | Dark |
|---|---|---|
| `--color-violet-10` | `hsl(210, 100%, 97.6%)` | `hsl(225.9, 15.6%, 21.4%)` |
| `--color-violet-20` | `hsl(220, 90%, 96.1%)` | `hsl(231.8, 15.7%, 27.5%)` |
| `--color-violet-30` | `hsl(222.6, 100%, 92.5%)` | `hsl(231.6, 26.6%, 36.9%)` |
| `--color-violet-40` | `hsl(226.5, 92.2%, 84.9%)` | `hsl(236.5, 33.9%, 49.8%)` |
| `--color-violet-50` | `hsl(238.4, 100%, 77.6%)` | `hsl(241.3, 98.6%, 72.4%)` |
| `--color-violet-60` | `hsl(242.9, 82.7%, 66.1%)` | `hsl(231.8, 100%, 79.8%)` |
| `--color-violet-70` | `hsl(246.8, 49.4%, 51.2%)` | `hsl(226.2, 88.4%, 86.5%)` |

### Purple

| Token | Light | Dark |
|---|---|---|
| `--color-purple-10` | `hsl(255, 100%, 98.4%)` | `hsl(255, 10.9%, 21.6%)` |
| `--color-purple-20` | `hsl(252, 88.2%, 96.7%)` | `hsl(255, 11.6%, 27.1%)` |
| `--color-purple-30` | `hsl(253.1, 100%, 93.7%)` | `hsl(260.5, 20.9%, 35.7%)` |
| `--color-purple-40` | `hsl(255.2, 88.7%, 86.1%)` | `hsl(265.1, 31.4%, 46.3%)` |
| `--color-purple-50` | `hsl(261.7, 100%, 75.1%)` | `hsl(269.3, 71.3%, 64.5%)` |
| `--color-purple-60` | `hsl(267.9, 84.2%, 60.4%)` | `hsl(264.7, 87.4%, 78.2%)` |
| `--color-purple-70` | `hsl(267.3, 43.7%, 45.3%)` | `hsl(259.6, 84.6%, 87.3%)` |

### Pink

| Token | Light | Dark |
|---|---|---|
| `--color-pink-10` | `hsl(308.6, 63.6%, 97.8%)` | `hsl(308.6, 6.8%, 20.2%)` |
| `--color-pink-20` | `hsl(315, 72.7%, 95.7%)` | `hsl(317.1, 10.6%, 25.9%)` |
| `--color-pink-30` | `hsl(319.4, 81%, 91.8%)` | `hsl(319.4, 20.2%, 32.9%)` |
| `--color-pink-40` | `hsl(322.9, 73.9%, 82%)` | `hsl(324.3, 32.4%, 41.8%)` |
| `--color-pink-50` | `hsl(323.6, 81.4%, 66.3%)` | `hsl(328.2, 54.4%, 57.8%)` |
| `--color-pink-60` | `hsl(326.9, 54.9%, 52.2%)` | `hsl(326.5, 66.9%, 72.7%)` |
| `--color-pink-70` | `hsl(328.1, 55.8%, 39%)` | `hsl(323.5, 87.3%, 84.5%)` |

## 3. Surfaces & page tints

| Token | Light | Dark |
|---|---|---|
| `--color-body` | `var(--color-page-tint)` | `hsl(202.5, 42.1%, 7.5%)` |
| `--color-canvas` | `hsl(var(--hsl-white))` | `hsl(200, 28%, 14.7%)` |
| `--color-canvas-light` | `var(--color-canvas)` | `var(--color-slate-10)` |
| `--color-inverted` | `white` | `hsl(199.1, 44%, 9.8%)` |
| `--color-page-tint` | `var(--color-page-tint-1)` | `var(--color-body)` |
| `--color-page-tint-1` | `hsl(135, 29%, 97%)` | `var(--color-body)` |
| `--color-page-tint-2` | `hsl(277, 100%, 98.5%)` | `var(--color-body)` |
| `--color-page-tint-3` | `hsl(26, 94%, 98%)` | `var(--color-body)` |
| `--color-page-tint-4` | `hsl(214, 100%, 98%)` | `var(--color-body)` |
| `--color-page-tint-5` | `hsl(0, 82%, 97.5%)` | `var(--color-body)` |
| `--color-page-tint-6` | `hsl(0, 0%, 96.5%)` | `var(--color-body)` |

Page tints are the six faint background washes B5 rotates per screen in light
mode; dark mode collapses them all to the single body color.

**Surface hierarchy (dark).** Verified against the live app's computed values
(2026-07-12): `--color-canvas` is a **distinct, darker** surface
(`hsl(200, 28%, 14.7%)` = `rgb(27,41,48)`), not an alias of `--color-canvas-light`
(`--color-slate-10` = `rgb(35,54,63)`). `--color-canvas` is the workhorse raised
surface — the content sheet, dock/project cards, buttons — sitting one subtle
step above the near-black body (`rgb(11,21,27)`). `--color-canvas-light` is the
lighter accent used more sparingly. (An earlier revision of this table aliased
dark `--color-canvas` to `--color-canvas-light`; corrected here.)

## 4. Semantic color aliases

Defined once at `:root`; they survive dark mode unchanged because the ramps
they point at flip underneath them.

```css
--color-ink: var(--color-ink-70);
--color-slate: var(--color-slate-60);
--color-sand: var(--color-sand-60);
--color-red: var(--color-red-60);
--color-orange: var(--color-orange-60);
--color-yellow: var(--color-yellow-60);
--color-green: var(--color-green-60);
--color-blue: var(--color-blue-60);
--color-purple: var(--color-purple-60);

--color-text: var(--color-ink);
--color-text-subtle: var(--color-ink-60);
--color-text-placeholder: var(--color-ink-50);

--color-warning: var(--color-warning-60);
--color-warning-10…60: var(--color-red-10…60);   /* step-for-step */

--focus-ring-color: var(--color-blue);
--client-visibility-color: var(--color-yellow-40);  /* "The client sees this" */
--client-hidden-color: var(--color-blue-30);
```

**Uncolor** (the "no color chosen" ramp) is the one alias that *does* change
personality per mode — warm sand in light, cool slate in dark:

| Token | Light | Dark |
|---|---|---|
| `--color-uncolor` | `var(--color-uncolor-60)` | — (unchanged) |
| `--color-uncolor-10…40` | `var(--color-sand-10…40)` | `var(--color-slate-10…40)` |
| `--color-uncolor-50` | `var(--color-sand-50)` | — (not overridden; stays sand-50) |
| `--color-uncolor-60` | `var(--color-sand-60)` | `var(--color-slate-60)` |
| `--color-uncolor-70` | — (not defined in light) | `var(--color-slate-70)` |

## 5. Alpha tints

```css
/* black at fixed alphas */
--tint-black-2: hsl(var(--hsl-black), 0.02);      /* also 5, 10, 15, 20, 25, 50, 75 */

/* ink mixed into transparency — adapts to mode automatically */
--tint-ink-2: color-mix(in hsl, transparent, var(--color-ink) 2%);
/* also 3, 5, 10, 15, 20, 25, 35, 50, 75 */

/* inverted (white in light / near-black in dark) */
--tint-inverted-2: color-mix(in hsl, transparent, var(--color-inverted) 2%);
/* also 5, 10, 15, 25, 50, 75 */
```

## 6. Recording colors & highlights

Recording colors are the 9-swatch picker used for to-do list icons, card
columns, calendar project colors, folders:

```css
--recording-color-aqua: var(--color-aqua-50);     --recording-color-purple: var(--color-purple-50);
--recording-color-blue: var(--color-blue-50);     --recording-color-red: var(--color-red-50);
--recording-color-green: var(--color-green-50);   --recording-color-sand: var(--color-sand-50);
--recording-color-orange: var(--color-orange-50); --recording-color-yellow: var(--color-yellow-50);
--recording-color-pink: var(--color-pink-50);
--recording-color-gray: var(--color-ink-50);
--recording-color-primary: var(--color-ink-50);
--recording-color-canvas: var(--color-canvas);
```

Rich-text highlight palette (text color + background pairs):

```css
--highlight-1…9:    yellow-60, orange-60, red-60, pink-60, purple-60, blue-60, green-60, sand-60, ink-60
--highlight-bg-1…9: yellow-30, orange-30, red-30, pink-30, purple-30, blue-30, green-30, sand-30, ink-30
```

## 7. Typography

```css
--font-base: -apple-system, system-ui, BlinkMacSystemFont, Aptos, Roboto, "Segoe UI",
             Helvetica, sans-serif, "Apple Color Emoji", "Segoe UI Emoji", "Segoe UI Symbol";
--font-mono: ui-monospace, SFMono-Regular, "SF Mono", Menlo, Consolas, "Liberation Mono", monospace;

--text-root: 10px;            /* html font-size: 1rem = 10px */
--text-10 … --text-32: var(--10px) … var(--32px);   /* 10,12,14,16,18,20,24,28,32 */

--text-xs: var(--12px);
--text-sm: var(--14px);
--text-base: var(--16px);
--text-md: var(--18px);       /* mobile: 16px */
--text-body: var(--20px);     /* mobile: 16px — B5 body copy is large */

--line-height-base: 1.5;
--line-height-snug: 1.35;
--line-height-headings: 1.15;
```

## 8. Spacing scale

Named pixel steps, expressed in rem (÷10):

```css
--1px --2px --4px --6px --8px --10px --12px --14px --16px --18px --20px --24px
--28px --32px --36px --40px --44px --48px --56px --64px --72px --80px --88px --96px
/* each --Npx: N/10 rem */
```

## 9. Borders & radii

```css
--border: 1px solid var(--color-ink-30);
--border-uncolor: 1px solid var(--color-uncolor-30);

--radius-xs: var(--2px);
--radius-sm: var(--4px);
--radius-md: var(--6px);
--radius-lg: var(--8px);
--radius-xl: var(--12px);
```

## 10. Shadows

Layered, each building on the last (same in both modes — they darken naturally
over dark surfaces):

```css
--shadow-base: 0 -1px 1px hsla(var(--hsl-black), 0.025), 0 1px 2px -0.5px hsla(var(--hsl-black), 0.05);
--shadow-sm: var(--shadow-base), 0 2px 4px -1px hsla(var(--hsl-black), 0.1);
--shadow-md: var(--shadow-sm), 0 4px 8px -2px hsla(var(--hsl-black), 0.1);
--shadow-lg: var(--shadow-md), 0 16px 32px -8px hsla(var(--hsl-black), 0.1);
--shadow-xl: 0 0 3rem hsla(var(--hsl-black), 0.08);
--shadow-modal: 0 0 0 1px hsla(var(--hsl-black), 0.1), 0 16px 24px -8px hsla(var(--hsl-black), 0.25),
                0 24px 32px -16px hsla(var(--hsl-black), 0.25);
--shadow-card: 0 0 0 1px hsla(var(--hsl-black), 0.05), 0 0.2em 0.2em hsla(var(--hsl-black), 0.05),
               0 0.4em 0.4em hsla(var(--hsl-black), 0.05), 0 0.8em 0.8em hsla(var(--hsl-black), 0.05);
```

## 11. Motion

```css
--duration-fast: 75ms;
--duration-medium: 150ms;
--duration-slow: 300ms;
--ease-out-overshoot: cubic-bezier(0.25, 1.25, 0.5, 1);
--ease-out-overshoot-lg: cubic-bezier(0.25, 1.35, 0.5, 1);
--ease-out-expo: cubic-bezier(0.16, 1, 0.3, 1);
```

## 12. Layout & component metrics

```css
--avatar-size-2xs: var(--16px);   --component-xs: var(--24px);
--avatar-size-xs: var(--24px);    --component-sm: var(--32px);
--avatar-size-sm: var(--32px);    --component-base: var(--40px);
--avatar-size: var(--40px);       --component-lg: var(--48px);
                                  --component-xl: var(--64px);

--nav-height: calc(var(--toolbar-size) + var(--16px) * 2);
--nav-trigger-width: 15.8rem;
--sidebar-width: clamp(40rem, 25vw, 56rem);
--sidebar-space: var(--sidebar-width);          /* mobile: 0px */
--toolbar-size: var(--component-base);
--toolbar-btn-size: var(--component-sm);
--toolbar-badge-size: var(--24px);
--tray-button-size: var(--component-sm);        /* My Bar buttons */
--tray-block-size: calc(var(--tray-button-size) + 1.6rem);
--field-row-column-size: 12rem;
--page-scroll-padding-start: 5.6rem;
--page-scroll-padding-end: 3.2rem;
--video-min-width: 22rem;
--video-min-height: 12rem;

--custom-safe-inset-top: var(--injected-safe-inset-top, env(safe-area-inset-top, 0px));
/* …same pattern for right / bottom / left (hybrid mobile app injects these) */
```

## 13. Z-index scale

```css
--z-lexxy-toolbar: 2;    --z-popup: 50;
--z-lexxy-internals: 3;  --z-flash: 60;
--z-lexxy-toolbar-open: 4; --z-footer-trays: 70;
--z-action-sheet: 20;    --z-sidebar: 100;   /* mobile: 201 */
--z-banner: 30;          --z-pings: 101;     /* mobile: 202 */
--z-toolbar: 40;         --z-nav: 200;
                         --z-modal: 300;
```

## 14. Feature-specific tokens

**To-dos / checkboxes**

```css
--checkbox-size: var(--20px);
--checkbox-gap: var(--8px);
--todo-padding-block: var(--2px);
```

**Star (project favorite) gradient**

| Mode | Value |
|---|---|
| Light | `linear-gradient(135deg, var(--color-yellow-30), var(--color-yellow-40) 50%, var(--color-orange-40) 54%, var(--color-orange-50))` |
| Dark | `linear-gradient(135deg, var(--color-yellow-70), var(--color-yellow-60) 50%, var(--color-orange-60) 54%, var(--color-orange-50))` |

**Timesheet ledger**

| Token | Light | Dark |
|---|---|---|
| `--ledger-bg-color` | `var(--color-green-10)` | `var(--color-green-30)` |
| `--ledger-toggle-color` | `var(--color-green-20)` | — (not overridden) |
| `--ledger-border-color` | `var(--color-green-30)` | `var(--color-green-40)` |
| `--ledger-border-color-alt` | `var(--color-orange-30)` | `var(--color-yellow-40)` |
| `--ledger-label-color` | `var(--color-green-60)` | `var(--color-green-60)` |

**Card Table (kanban)**

```css
--kanbanColumnWidth: 25.5rem;
--kanbanCardsMinWidth: 22rem;
--kanbanSlimColumnWidth: 5.4rem;   /* collapsed column strip */
--kanbanGridGap: 1rem;
--kanbanHighlightSize: var(--4px);
--kanbanAnimationSpeed: 0.15s ease;
```

## 15. Rich-text editor (Trix + Lexxy)

B5 is mid-migration from Trix to Lexxy (37signals' Lexical-based editor); both
token sets exist.

```css
--trix-contained-radius: 0.8rem;
--trix-toolbar-padding: 0.3rem;
--trix-toolbar-button-size: 3.6rem;
--trix-toolbar-button-radius: calc(var(--trix-toolbar-padding) * 2);
--trix-toolbar-button-gap: 0.2rem;

--lexxy-color-ink: var(--color-ink);
--lexxy-color-ink-medium: var(--tint-ink-50);
--lexxy-color-ink-light: var(--tint-ink-20);
--lexxy-color-ink-lighter: var(--tint-ink-10);
--lexxy-color-ink-lightest: var(--tint-ink-5);
--lexxy-color-ink-inverted: var(--color-canvas);
--lexxy-color-accent-dark: var(--color-blue-70);
--lexxy-color-accent-medium: var(--color-blue-40);
--lexxy-color-accent-light: var(--color-blue-30);
--lexxy-color-accent-lightest: var(--color-blue-20);
--lexxy-color-red: var(--color-red-60);
--lexxy-color-green: var(--color-green-60);
--lexxy-color-blue: var(--color-blue-60);
--lexxy-color-purple: var(--color-purple-60);
--lexxy-color-selected: var(--lexxy-color-accent-light);
--lexxy-color-selected-hover: var(--lexxy-color-accent-medium);
--lexxy-color-selected-dark: var(--lexxy-color-accent-dark);
--lexxy-color-table-cell-add: var(--lexxy-color-selected-hover);
--lexxy-color-table-cell-toggle: var(--lexxy-color-accent-lightest);
--lexxy-color-table-cell-selected-border: var(--lexxy-color-selected-dark);
--lexxy-color-table-cell-selected-bg: var(--lexxy-color-accent-lightest);
--lexxy-color-table-cell-remove: oklch(60% 0.15 27 / 0.15);
--lexxy-color-code-token-att: var(--color-red-60);
--lexxy-color-code-token-comment: var(--color-ink-60);
--lexxy-color-code-token-function: var(--color-purple-60);
--lexxy-color-code-token-operator: var(--color-red-60);
--lexxy-color-code-token-property: var(--color-blue-70);
--lexxy-color-code-token-punctuation: var(--color-ink-70);
--lexxy-color-code-token-selector: var(--color-green-60);
--lexxy-color-code-token-variable: var(--color-orange-60);
--lexxy-text-small: 0.85em;
--lexxy-content-margin: 0;
--lexxy-table-tools-top: -105%;
--lexxy-focus-ring-offset: 0;
```

## 16. Code syntax highlighting (LCH)

| Token | Light | Dark |
|---|---|---|
| `--keyword` | `lch(50.16 68.78 25.97)` | `lch(67.63 58.99 30.64)` |
| `--entity` | `lch(39.03 73.26 304.21)` | `lch(75.13 46.73 306.74)` |
| `--constant` | `lch(39.68 63.13 279.47)` | `lch(74.9 39.71 255.53)` |
| `--string` | `lch(19.22 34.92 275.47)` | `lch(74.9 39.71 255.53)` |
| `--variable` | `lch(57.9 81.69 53.33)` | `lch(76.17 61.1 61.97)` |
| `--comment` | `lch(47.93 7 254.8)` | `lch(60.83 6.66 254.46)` |
| `--entity-tag` | `lch(49.14 52.75 142.85)` | `lch(83.65 59.31 141.61)` |
| `--markup-heading` | `lch(39.68 63.13 279.47)` | `lch(47.93 71.67 280.72)` |
| `--markup-list` | `lch(40.44 43.36 84.69)` | `lch(83.84 57.9 85.03)` |
| `--markup-inserted` | `lch(49.14 52.75 142.85)` | `lch(61.52% 51.9 138.82)` |
| `--markup-deleted` | `lch(39.64 68.17 31.45)` | `lch(59.67% 45.21 23.94)` |

## 17. Third-party: Duet date picker

Light-only (no dark overrides in production):

```css
--duet-color-primary: #005fcc;      --duet-color-surface: #fff;
--duet-color-text: #333;            --duet-color-overlay: rgba(0, 0, 0, 0.8);
--duet-color-text-active: #fff;     --duet-color-border: #333;
--duet-color-placeholder: #666;     --duet-radius: 4px;
--duet-color-button: #f5f5f5;       --duet-z-index: 600;
--duet-font: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
--duet-font-normal: 400;            --duet-font-bold: 600;
```

## 18. Mobile overrides (`@media (width < 768px)` on `:root`)

```css
--text-md: var(--16px);
--text-body: var(--16px);
--sidebar-space: 0px;      /* sidebar overlays instead of reserving space */
--z-sidebar: 201;          /* sidebar/pings rise above nav */
--z-pings: 202;
```

## 19. Implementation notes for the clone

- Ship one `tokens.css`: the §2–§17 light values on `:root`, dark overrides in
  the two blocks described in §1, mobile overrides in §18. Set
  `color-scheme: light`/`dark` alongside so form controls follow.
- Production also declares ~650 *component-scoped* custom-property contexts
  (e.g. `.btn`, `.lineup`, `.kanban-column`, plus 7 dark-mode component tweaks
  like `--card-background-color` on `.dock-card`, `--boost-background-color`
  on `.boosts`). Those are component CSS, not global tokens — define them in
  the component files that need them; this doc intentionally covers root
  tokens only.
- Quirks preserved from production (don't "fix" them): `--color-uncolor-50`
  never flips in dark; `--color-uncolor-70` exists only in dark;
  `--color-slate-*` is an ink alias in light but a distinct ramp in dark;
  Duet has no dark theme.

## 20. Observed component patterns (from the live app, July 4 2026)

Companion to the token sets above: how the components actually look and
compose on screen. Every pattern below is visible in the reference captures
under **`reference/screens/`** (dark mode; file names cited). Component-scoped
custom properties (§19) belong with these components when built.

### 20.1 Page scaffold
- Full-bleed dark body; content lives on a **centered raised sheet**
  (`--color-canvas-light`-style panel) with generous side gutters
  (`adminland.png`, `preferences.png`). Some surfaces are full-bleed instead:
  Lineup, card table scroller, calendar grids.
- Every project/tool page opens with the **breadcrumb bar** pinned atop the
  sheet: `Project ‹ Tool ‹ Item` left; Bookmark · Edit · `…` options right
  (`sample-todos.png`). Above it all floats the centered **account switcher**
  ("🏕 Basecamp ˅").
- **Display headings are huge and extra-bold** (≈40–48px, tighter
  line-height), usually with a one-line muted subtitle
  (`invite-chooser.png` "Who are you inviting?"). Section headings inside
  pages use a heading + full-width hairline rule pattern ("What? ———",
  `notification-settings.png`).

### 20.2 Buttons & controls
- **Primary action** = solid blue pill (blue-50 family), dark-ink label, ~8px
  radius, comfortable padding ("Post this message", "Save my settings").
  **Secondary** = outlined/ghost pill ("Never mind", "Add another tool…").
  **Danger-confirm** = outlined pill with red/orange label on dark ("Yes,
  cancel my account" — border + warm text, no fill).
- **Toolbar row** under a tool title: primary pill first, then ghost select
  pills with carets ("Categories ˅", "View as ˅", "Sort by ˅"), then the
  rounded **Filter…** textbox (`sample-message-board.png`).
- **On/Off switches** carry a literal label inside the knob track: green fill
  + "On" / gray + "Off" (`customize-tools.png`, `voice-notes.png`,
  `project-people-clients.png`).
- **Choice chips**: weekday toggles render as rounded-rect chips, blue-filled
  when active, outline when not (Mon–Fri in
  `notification-settings-work-can-wait.png`).
- **Segmented tabs**: true folder-tabs for Team/Clients
  (`project-people-team.png`); pill segments for Best match/Most recent
  (`search.png`) and Project/To-do List Templates (`templates.png`).
- **Radio option cards**: large bold option title + muted multi-line
  description under it, plain radio at left — used for role chooser, "Who
  should see this?", notification "What?/When?" groups.

### 20.3 Cards & the dock
- Dock tool cards: large rounded (radius-xl) cards on the sheet, the **tool
  name sits above the card** in the salmon/peach accent, previews render
  inside; hover reveals a trash "remove" icon; the add-tool card is a
  **dashed-border square with a big +** (`project-dock.png`).
- Empty tools show **hand-drawn sketch placeholder art** + muted explainer +
  example copy inside a dashed rounded box (`checkins-empty.png`,
  `message-board-empty.png` "No messages just yet").
- Home project cards: title, 2-line muted description clamp, member avatar
  stack bottom-left; hover star; sample project labeled inline
  (`home.png`).

### 20.4 Kanban (card table) anatomy — `sample-card-table.png`
- **Triage** section spans the top (cards in a row, watcher avatars +
  "Watching:" right). User columns below: **colored header strip** (column
  color), name + count + `…`; column body tinted by the column color at low
  alpha.
- **On Hold** is a dashed divider row *inside* a column with small-caps
  "ON HOLD" label; on-hold cards get dashed borders.
- **Not Now / Done** are collapsed **vertical edge strips** with rotated
  labels and counts — Done's strip is green. A floating blue `+` between the
  last column and Done adds a column.
- Card tile: title, "By {person} on {date}" muted byline, subtask badge
  (`✔ 0/4`), comment count, assignee avatar right; hover reveals
  assign-to-me.

### 20.5 Recording pages
- Title block: big heading, muted byline ("Stephen on Jul 4"), category chip
  when set. Body in `--text-body` (20px) rich text.
- **Boosts**: ghost rocket button under the body expands into a horizontal
  quick bar of 7 emoji, with `…` opening a ~50-emoji grid panel; existing
  boosts render as rounded chips.
- **Client visibility ribbon**: a vertical blue tab hugging the right edge of
  the page with rotated text "This message is private to our team. Change"
  (`--client-hidden-color` family). The composer-side control is the
  "Who should see this?" radio pair (🔒 team chip styled blue / 👁 client).
- **Subscription footer**: hairline rule, "N people will be notified…" line,
  avatar stack, "Add/remove people…" + "Don't notify me" ghost pills.
- **Options popover** (`…`): right-anchored panel filled with the vivid blue
  accent — icon + label rows in white, tiny section captions ("Share",
  "History"), ✕ in the corner; submenus (Bubble up) expand inline with
  right-aligned muted schedule labels ("Sat, 4:00pm").
- Trashed state: full-width warm brown banner atop the page ("You put this
  message in the trash… view it, restore it, or go to the trash").

### 20.6 Forms
- Single centered column (~640–900px), **label-above-input**, muted
  "required"/help text beside labels; inputs are dark rounded-md fields with
  1px ink-30 borders (`profile-edit.png`, `enrollment-employee.png`).
- Two-up compact grids where inputs pair naturally (enrollment name/email
  rows). "+" square button adds another row of the same fieldset.
- Composer pages (message/event) are **full-width canvases**, not boxed
  forms: chip/title inputs borderless at display size, Lexxy toolbar pinned
  (`message-new.png`, `event-new.png`); submit cluster repeats in the sticky
  header bar and below the body.
- Date/time rows: date field + time field pairs with an arrow between start
  and end, "All day" switch centered beneath (`event-new.png`); Duet pickers
  (§17) behind the calendar icons.

### 20.7 Shell chrome
- **My Bar** (persistent footer): avatar button far left; icon+label ghost
  buttons centered (My Tasks/Events/Bookmarks/Notes, Do Today center); the
  **"Pings + ● New for you"** pill far right wearing the orange unread dot
  (`home.png`). Trays open as bottom-anchored dialog popovers above the bar
  (`my-bar-tasks-tray.png`).
- **Sidebar** (right slide-over, `sidebar.png`): Pings header with dashed
  "+ Ping" circle and helper tooltip; "N items will bubble up later" link;
  orange-accent "New for you" header with right-aligned "Mark all read";
  "Previous notifications" section; rows = icon/avatar pair, bold title,
  2-line excerpt, timestamp·source line, red unread dot right; **Filter…**
  field pinned at the bottom; "🔕 Shhh…" toggle in the window footer.
- **Jump menu** (`jump-menu.png`): centered sheet dropping from the account
  switcher — four icon shortcut tiles in a row, search field, "Recently
  visited" icon rows (includes non-project pages), "Projects + See all",
  keyboard-hint footer.
- Live-region **toasts/flashes** anchor bottom-center; the event-reminder
  toast shifts greener as start time approaches (INIT §2.1).

### 20.8 Data views
- **Month grids** (`sample-schedule.png`, `global-calendar.png`):
  Sun–Sat header, hairline cell borders, day-number links top-left of cells,
  event pills colored by per-user project color, due to-dos rendered with
  live checkboxes; month navigator = ‹ › + "month year" popover button.
  Global calendar adds the left "Calendars" sources panel (checkbox rows +
  9-swatch color pickers).
- **Lineup** (`lineup.png`): full-bleed horizontal timeline, week columns
  with date labels, blue "Today, {date}" lozenge on the today gridline,
  "← Prev 6 weeks / Next 6 weeks →" pagers, "Add marker" in the header bar.
- **Feeds** (`activity.png`): day sections with big date headings, "N people
  active today" avatar row, icon-coded rows (timestamp column, actor avatar,
  linkified sentence).
- **List rows** (docs & files, people, trash): color label dot + checkbox +
  type icon/thumbnail + title + muted meta, options `…` on hover; selection
  reveals the bulk action bar (INIT §9.11).

### 20.9 People rendering
- Avatars are circles everywhere; fallback = solid recording-color circle
  with white initial (`home.png` "S"). Stacks overlap ~30%.
- Assignee/mention chips: avatar + first-name-last-initial in a rounded pill
  (`sample-todos.png`). Out-of-office adds a yellow "OUT" overlay band on the
  avatar app-wide (INIT §9.32); deactivated/removed people get the hover
  status tooltip (clone decision, INIT §3.5).
