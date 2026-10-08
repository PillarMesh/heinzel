# Heinzel + PillarMesh — selected H1 + P2

Approved selection: Heinzel H1 / Interlock and PillarMesh P2 / Woven links.

Two coordinated identities: **Heinzel** is the product; **PillarMesh** is the company behind it.

## Start here

- Open `brand-preview.png` or `brand-preview.pdf` to see both identities.
- Use `heinzel/website/logo.svg` for the product website.
- Use `pillarmesh/website/logo.svg` for the company website.
- Use `logo-dark.svg` on dark backgrounds, or `logo-white.svg` for a single white mark.
- Prefer SVG on the web. `logo.png` and `logo@2x.png` are transparent PNG alternatives.
- `heinzel/svg/heinzel-endorsed-by-pillarmesh.svg` is the optional parent-company endorsement.

## Design rationale

**Heinzel — the joined H.** Two sturdy forms meet across a diagonal joint. The silhouette remains an H, while the cut through the crossbar hints at the z within the name. The mark suggests careful assembly: turning separate inputs into a coherent result. Electric blue gives the product its own visible identity.

**PillarMesh — woven links.** Two linked diamond forms express connection and collaboration. The lowercase wordmark keeps the company approachable. Deep pine distinguishes the company from the blue product identity. The company name in prose remains PillarMesh.

The wordmarks use outlined IBM Plex Sans: weight 600 for Heinzel, 500 for PillarMesh, with adjusted tracking. No font needs to be installed to display the supplied SVGs or PDFs. The original licensed font and repeatable generation source are included.

## Asset map

Each brand contains:

| Folder | Contents |
| --- | --- |
| `website/` | Convenient default SVGs, transparent PNGs, favicon and Apple icon |
| `svg/` | Horizontal, stacked, symbol-only and wordmark; colour, reverse, black and white |
| `png/` | Transparent raster exports; colour lockups at 256, 512, 1024 and 2048 pixels wide; symbols from 16 to 1024 pixels; other treatments at 1024 pixels |
| `print/` | Vector PDF horizontal and symbol variants |
| `favicon/` | SVG, 16/32/48 px PNG, multi-resolution ICO, 180 px Apple touch icon |
| `social/` | 1024 px avatar and 1200 × 630 share card |

The white-background JPG is for contexts that cannot preserve alpha. PNG and SVG logo masters are transparent. Social cards and app tiles deliberately have solid backgrounds.

## Usage

- Keep at least one symbol-stroke width of clear space around the visible symbol, and preferably half the symbol height around a standalone lockup.
- Recommended minimum horizontal width: Heinzel 140 px; PillarMesh 180 px. Below this, use the symbol instead.
- Favicon minimum: 16 × 16 px. Use the supplied icon, not a miniature wordmark.
- Keep proportions fixed. Do not stretch, rotate, add shadows, apply gradients, or change the joint/link geometry.
- Use the colour logo on white or very pale backgrounds. Use reverse on navy or similarly dark surfaces. Use black/white versions for one-colour work.
- For product pages, lead with Heinzel. Use the optional “by PillarMesh” lockup where ownership needs to be explicit. On company pages, lead with PillarMesh.
- The sentences on the presentation board describe the design; they are not required taglines.
- Vector PDFs are RGB masters. Request an output-profile-specific print proof from your printer rather than treating screen colours as exact ink matches.

## Palette

| Token | Hex | RGB |
| --- | --- | --- |
| Heinzel blue | `#2155F5` | 33, 85, 245 |
| Heinzel reverse blue | `#8BAAFF` | 139, 170, 255 |
| PillarMesh pine | `#14695F` | 20, 105, 95 |
| PillarMesh reverse mint | `#6DD4B6` | 109, 212, 182 |
| Shared navy | `#0D1B2F` | 13, 27, 47 |
| White | `#FFFFFF` | 255, 255, 255 |

## Website example

```html
<img src="/brand/logo.svg" alt="Heinzel" width="200" />
<link rel="icon" href="/brand/favicon.svg" type="image/svg+xml" />
<link rel="icon" href="/brand/favicon.ico" sizes="16x16 32x32 48x48" />
<link rel="apple-touch-icon" href="/brand/apple-touch-icon.png" />
```

Replace the alt text with PillarMesh for the company logo. Supply the other dimension or CSS `aspect-ratio` from the SVG's viewBox to reserve layout space.

## Editable source

All SVG artwork is vector paths, with no linked images, scripts, external stylesheets or live fonts. Edit it in Figma, Illustrator, Affinity Designer or Inkscape. `source/build.py` contains the exact geometric construction and text-to-outline process. `source/IBM-Plex-OFL.txt` is the font's original license. The original marks were drawn for this package; no stock icons were used.

The build uses Python with fontTools, Brotli and Pillow, plus `rsvg-convert`. No image-generation model or raster tracing was used. PNGs and PDFs are rendered from the SVG masters.
