## Provided fonts

These are BDF fonts, a simple bitmap font format that many font tools can make. Being bitmap fonts,
they look good on very low resolution screens such as the LED panel.

Every font here is public domain or under the MIT licence (see `LICENSE.md` at the top of the repo):

- The X11 misc-fixed fonts (`4x6`, `5x7`, `5x8`, `6x9`, `6x10`, `6x12`, `6x13`, `6x13B`, `6x13O`,
  `7x13`, `7x13B`, `7x13O`, `7x14`, `7x14B`, `8x13`, `8x13B`, `8x13O`, `9x15`, `9x15B`, `9x18`,
  `9x18B`, `10x20`) are public domain: each file's COPYRIGHT line says "Public domain font. Share
  and enjoy." They are Markus Kuhn's Unicode versions of the classic X11 fonts; see [README](./README)
  and [AUTHORS](./AUTHORS).
- `tom-thumb.bdf` is under the MIT licence. It was made by Robey Pointer (2010) by modifying a 4x6 font by
  Brian Swetland, and first published at https://robey.lag.net/2010/01/23/tiny-monospace-font.html
  (that page adds, from 2015, that Brian Swetland authorised CC0 or CC-BY 3.0 as well).

`clR6x12`, `helvR12` and `texgyre-27` were removed on 25 Nov 2026 because their licences aren't MIT or
public domain. A saved setting that names one now uses the nearest font here (`bongo/fonts.py`, `REPLACED`).

## Adding a font

Only add a font that is public domain or under the MIT licence, and say which in the list above and in
`LICENSE.md`. Fonts are in a human readable `*.bdf` format; a font editor, or [otf2bdf] for a font in
another format, can make one:

```bash
otf2bdf -v -o myfont.bdf -r 72 -p 30 /path/to/font-Bold.ttf
```

[otf2bdf]: https://github.com/jirutka/otf2bdf
