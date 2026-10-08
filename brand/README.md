# Brand assets

The supplied Heinzel and PillarMesh identity, as the product surfaces serve it. Heinzel is the
product; PillarMesh is the company behind it.

These files are the source of truth. Two surfaces need their own copy, because each is a separate
deployment unit with its own delivery: `apps/console/web/public/brand/` is bundled into the
console's static output, and `deploy/quickstart/superset/branding/` is mounted into a container
whose chrome serves no files of ours. `npm run check:brand`, in `apps/console`, fails when a copy
has drifted from this directory -- so this is the one place a mark is ever changed.

| File | Where it is used |
| --- | --- |
| `heinzel/heinzel-horizontal-reverse.svg` | The console's navigation rail, which is dark in both themes |
| `heinzel/heinzel-horizontal-color.svg` | Heinzel on a white or very pale surface |
| `heinzel/heinzel-horizontal-white.svg` | One-colour work on a dark surface |
| `heinzel/heinzel-symbol-color.svg`, `heinzel/heinzel-symbol-reverse.svg` | The mark alone, below the lockup's minimum width |
| `heinzel/favicon.svg`, `heinzel/favicon.ico`, `heinzel/apple-touch-icon.png` | Browser and home-screen icons |
| `pillarmesh/*` | Company-level surfaces, not the product's chrome |

## Palette

| Token | Hex |
| --- | --- |
| Heinzel blue | `#2155F5` |
| Heinzel reverse blue | `#8BAAFF` |
| PillarMesh pine | `#14695F` |
| PillarMesh reverse mint | `#6DD4B6` |
| Shared navy | `#0D1B2F` |

`apps/console/web/src/styles/tokens.css` carries Heinzel blue as the console's primary, and
`deploy/quickstart/superset/superset_config.py` carries it as the dashboard chrome's, so an
action means the same thing on both sides of the link.

## Constraints that travel with the artwork

Every SVG is outlined vector paths: no linked images, scripts, external stylesheets or live
fonts, so a mark renders the same inside an `<img>` where the page's own faces are unavailable.
Keep proportions fixed, keep a symbol-stroke of clear space, and do not restyle the joint or link
geometry. Minimum horizontal width is 140px for Heinzel and 180px for PillarMesh; below that use
the symbol. `USAGE-GUIDE.md` is the supplied guidance in full, and `IBM-Plex-OFL.txt` is the
licence of the face the lettering was outlined from.
