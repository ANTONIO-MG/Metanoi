# Metanoia Group — brand reference for the Learning Hub

Metanoia Group is a South African B-BBEE verification, transformation and
skills-development group. Tagline: *Empowering through transformation*.

Metanoia House, 86 Fleet Street, Ferndale, Randburg 2194 · +27 11 027 8720 ·
info@metanoiagroup.co.za

This file describes how the brand is applied in the hub: the palette and its
CSS tokens, typography, and the logo files. The stylesheets that implement it
are `static/css/brand.css` (Bootstrap and theme tokens) and
`static/css/metanoia-ui.css` (the hub's own components and the `--t-*` tokens).

> **The logo is a placeholder.** The current mark (a gold and blue "M") and the
> "METANOIA GROUP" wordmark were generated for development until Metanoia
> supplies its official artwork. See [Swapping in the official logo](#swapping-in-the-official-logo).

## Palette

| Colour | Hex | Tokens | Use |
|---|---|---|---|
| Metanoia Brown | `#3E2415` | `--metanoia-brown` (+ `-rgb`, `-700` `#5E4335`, `-400` `#A48E80`, `-100` `#EFE7E0`); `--t-brown` (+ `-80`, `-50`, `-10`) | Headings, dark surfaces, the dark button, the welcome banner, the dark theme's lead colour. Tints for body text (`-700`) and hairlines (`-100`). |
| Metanoia Gold | `#F2A900` | `--metanoia-gold` (+ `-rgb`); `--t-gold` | Progress bars, "done" and active markers, the arrow chip on buttons, the gold stroke of the logo mark. |
| Metanoia Yellow | `#FFC20E` | `--metanoia-yellow` (+ `-rgb`) | Sparing accent only: stars, key numbers, earned badges. |
| Blue | `#0B5FC6` | `--metanoia-blue` (+ `-rgb`, `-400` `#5A93DC`, `-200` `#A9C8EE`, `-50` `#E8F0FB`); `--t-blue` (+ `-70`, `-40`, `-10`) | Links, primary buttons, focus rings, active states, the blue stroke of the logo mark. `-400` is the link and primary colour in the dark theme. |
| Success green | `#4F7F12` (dark theme `#6CC04A`) | `--metanoia-success` (+ `-200` `#B8DB8A`, `-50` `#EEF6E2`) | Success text and badges. It stays green so success still reads as success. |
| Cream | `#FAF7F2` | `--metanoia-cream`; `--t-cloud` | The warm page background and soft panels. |
| Warm Slate | `#6B5E55` | `--metanoia-slate` (+ `-rgb`); `--t-slate` | Small print, secondary text, the secondary button. |
| White | `#FFFFFF` | `--metanoia-white`; `--t-surface` | Cards and surfaces. |

Status colours (danger and warning alerts, red badges) are deliberately left
at Bootstrap's values: they carry meaning, not brand.

The dark theme (`[data-bs-theme="dark"]` in `brand.css`) uses a deep brown body
(`#1E120A`), brown surfaces, white headings and the lighter blue (`#5A93DC`)
for links and primary actions.

## Typography

- **Montserrat** everywhere, through `--metanoia-font` (falls back to the
  system UI stack). Web pages load it from Google Fonts; PDFs and certificates
  use the bundled files in `static/fonts/certificate/` (Regular, SemiBold, Bold,
  ExtraBold, ExtraBold Italic; licence in `OFL-Montserrat.txt`).
- Base size 14px (`--bs-body-font-size: .875rem`): Montserrat runs wider than
  the theme's original font, so 14px keeps rows and buttons at their old length.
- Hierarchy: ExtraBold (800) for display headings, Bold (700) for headlines,
  SemiBold (600) for labels; eyebrows and labels in spaced capitals.

## Logo files (placeholder set)

All of these are the generated placeholder and should be replaced together.

**Inline SVG**

| File | Use |
|---|---|
| `templates/partials/_metanoia_logo.html` | The animated, recolourable header logo (mark + wordmark). The mark is two polylines (`.logo__gold`, `.logo__blue`); the wordmark is `.logo__word` and `.logo__group`. |

**`static/images/brand/`**

| File | Size | Use |
|---|---|---|
| `metanoia-logo.svg` / `metanoia-logo.png` | 656×117 / 1200×351 | Full logo, on light backgrounds |
| `metanoia-logo-reversed.svg` | 656×117 | Full logo for dark backgrounds (shop covers) |
| `metanoia-logo-mono-white.svg` | 656×117 | Single-colour white logo |
| `metanoia-logo-line.svg` / `metanoia-logo-line.png` | 704×158 / 1200×351 | Logo with the tagline line (certificates) |
| `metanoia-logo-line-reversed.svg` / `metanoia-logo-line-reversed.png` | 704×158 / 1200×351 | The same, reversed (sign-in pages, session thumbnails) |
| `metanoia-logo-compact.png` | 1200×298 | Compact logo |
| `metanoia-mark.png` | 512×512 | The "M" mark alone (invoices, statements, README) |
| `metanoia-tick.svg` / `metanoia-tick-white.svg` | 128×117 | The mark as a repeating background pattern (`metanoia-ui.css`) |
| `metanoia-tick-app.svg` / `metanoia-tick-app.png` | 256×256 / 512×512 | App-icon tile (desktop notifications) |
| `avatar-tick-light.png`, `avatar-tick-navy.png`, `avatar-tick-ring.png` | 512×512 | The mark as an avatar: on cream, on brown, and in the gold ring |
| `metanoia-seal.svg` / `metanoia-seal.png` | 256×256 / 512×512 | Round "METANOIA GROUP" seal (shop) |
| `metanoia-alumni.svg` / `metanoia-alumni.png` | 256×256 / 512×512 | Round "METANOIA ALUMNI" seal (certificates) |
| `email-banner.png` | 1280×400 | Logo banner with the tagline |

The file names still contain "tick" and "navy" from the app this hub was built
from. They are referenced by name in templates, CSS and Python, so keep the
names when you replace the artwork.

**`static/images/icons/`** (the favicon set; the other PNGs in this folder are
illustrations, not part of the logo)

| File | Size |
|---|---|
| `favicon-16.png` | 16×16 |
| `favicon-32.png` | 32×32 |
| `favicon-180.png` | 180×180 (Apple touch icon) |
| `favicon-512.png` | 512×512 |

**Elsewhere**

| File | Size | Use |
|---|---|---|
| `static/images/favicon.ico` | 16, 32 and 48 px | `/favicon.ico` and the error pages |
| `static/images/logo.svg` | 512×512 | Square organisation logo: `metanoia-mark.png` embedded as a PNG data URI, so replace the embedded image too |
| `static/email/sig-avatar.png` | 192×192 | The ringed mark in e-mail signatures for shared mailboxes |
| `static/email/sig-logo.png` | 300×74 | Logo in e-mail and notification signatures |
| `static/email/banner.jpg` | 1280×400 | Banner at the foot of e-mails and mailbox signatures (placeholder: "METANOIA GROUP" and the tagline on cream, with the mark on brown) |

## Swapping in the official logo

1. Ask Metanoia for the master artwork as vector (SVG or AI/EPS), plus the
   official colour values if they differ from the palette above.
2. Export each file in the tables above **with the same file name, format and
   pixel size** (or SVG `viewBox` aspect ratio), and overwrite the placeholder.
   Keep transparent backgrounds where the placeholder has one (the logo PNGs,
   the seals, `sig-avatar.png`, `sig-logo.png`). Square files (marks, avatars,
   seals, favicons) need the artwork centred with some padding, because several
   are shown inside a circle.
3. Replace the inline SVG in `templates/partials/_metanoia_logo.html`. Keep the
   class names (`t-logo`, `logo__word`, `logo__group`, `logo__gold`,
   `logo__blue`) on the matching parts so the draw-in animation and the
   reversed colouring in `metanoia-ui.css` keep working, and update the
   `viewBox`.
4. Rebuild `static/images/favicon.ico` from the 512px icon with 16, 32 and 48px
   sizes.
5. If the official colours differ, change the hex values in the token blocks
   at the top of `brand.css` and `metanoia-ui.css` (and the `-rgb` triplets
   next to them); everything else follows the tokens.
6. Run `python manage.py collectstatic --noinput` and hard-refresh. E-mail
   clients cache images, so check a fresh e-mail.
