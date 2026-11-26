# Licence

MIT License

Copyright (c) 2026 spacewaffle42

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.

## Third-party parts

Everything in this repository is our own work under the licence above, except these:

- **The X11 misc-fixed fonts** (`resources/fonts/` 4x6 to 10x20, with their bold and oblique
  versions): public domain. Each file says "Public domain font. Share and enjoy." in its
  COPYRIGHT line. They are Markus Kuhn's Unicode versions of the classic X11 fonts; see
  `resources/fonts/README` and `resources/fonts/AUTHORS`.
- **Tom Thumb** (`resources/fonts/tom-thumb.bdf`): MIT licence. Made by Robey Pointer (2010) by
  modifying a 4x6 font by Brian Swetland. The BDF's own COPYRIGHT line says only "MIT"; the licence
  and the credit are on its author's page, <https://robey.lag.net/2010/01/23/tiny-monospace-font.html>.
  That page adds (2015) that Brian Swetland authorised the font under CC0 or CC-BY 3.0 as well.
- **OpenWeather's logos** (`resources/media/attribution/openweather.*`): OpenWeather's own
  files and trade marks, not covered by the MIT licence. They are shipped only to credit
  OpenWeather wherever its data is shown, as its terms ask; see
  `resources/media/attribution/README.md`.

The dashboard uses the fonts already on the viewer's device; it ships no web fonts.

The Python packages listed in `requirements.txt`, and the `rgbmatrix` library built on the Pi from
hzeller/rpi-rgb-led-matrix, are installed separately. They come under their own licences and are
not part of this repository.
