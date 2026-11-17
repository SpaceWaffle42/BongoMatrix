# Attribution logos

Logos that data providers ask to be shown with their data. They ship with every build (device and public).
All of them are the providers' own files, supplied by the project's owner: never redraw, trace or change
them; replace them only with the provider's own.

- `openweather.svg`: OpenWeather's own icon, used for attribution: the Weather screens show it on their
  credit (their loading screen) with OpenWeather's name, because OpenWeather's terms ask for credit
  wherever its data is shown. `openweather.png` is the same icon rasterised (474x369) for the Matrix to
  read. On the panel it is scaled down (12 rows tall on a 64x32 panel, 16 on panels 48 rows or more, 10
  where only that fits) with every LED fully on or off, in its own orange.
  Without the PNG, the credit shows a cloud instead.
- `openweather_logo.webp`: OpenWeather's own full logo (the icon and a white "OpenWeather" wordmark,
  128x55), supplied by the owner. The dashboard shows it, 38 px tall, with "Weather data provided by
  OpenWeather" in the Weather guide, the Add a module dialog and Settings > Weather. White: made for the
  dashboard's dark page. It isn't used on the panel, where the wordmark would be a smudge.
