# Third-Party Notices

This product bundles the following third-party components into the built
console application.

## IBM Plex Sans and IBM Plex Mono

- Copyright 2019 IBM Corp. All rights reserved. (IBM Plex Sans)
- Copyright 2017 IBM Corp. All rights reserved. (IBM Plex Mono)
- License: SIL Open Font License, Version 1.1 (OFL-1.1)
- Reserved Font Name: "Plex", per the upstream IBM/plex `LICENSE.txt`. The
  Fontsource-packaged licence text bundled with these npm packages omits the
  Reserved Font Name line; Heinzel ships the Fontsource files unmodified.
- npm packages: `@fontsource-variable/ibm-plex-sans@5.3.0`, `@fontsource/ibm-plex-mono@5.3.0`
- License text ships with the build at:
  - `dist/licenses/ibm-plex-sans-OFL.txt`
  - `dist/licenses/ibm-plex-mono-OFL.txt`

## Bundled JavaScript dependencies (react, react-dom, react-router, scheduler, ajv, and others)

Minification strips the licence banners these packages would otherwise carry
in the built bundle. A Vite build plugin
(`apps/console/build/third-party-licenses.ts`) collects every third-party
package actually bundled into the console and emits their licence texts,
concatenated with an `name@version` and SPDX licence header per package, to:

- `dist/licenses/THIRD_PARTY.txt`

The build fails if a bundled package has no licence file to ship.
