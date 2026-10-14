-- Matrix Astronomy moon sprites. Run headless:
--   aseprite -b --script-param out=<dir> --script tools/aseprite/moon_sprites.lua
-- Produces editable .aseprite sources; sheets are exported with --sheet.
-- Phase frames: index i covers Skyfield moon_phase angle i*360/FRAMES
-- (0 new, 90 first quarter, 180 full, 270 last quarter). Waxing lit on the
-- right: a fixed northern-hemisphere schematic, not a sky-oriented render.

local out = app.params.out or '.'
local FRAMES = 32

-- Limited pixel-art palette: highland, maria and earthshine tones.
local LIT = { {236,232,222}, {214,210,200}, {184,180,172}, {150,148,142}, {118,116,112} }
local DARK = { {34,36,44}, {24,26,32}, {16,17,22} }

-- Maria in unit-disc coordinates (x right, y down): x, y, radius, depth.
local MARIA = {
  {-0.34,-0.30,0.24,2}, {-0.56, 0.08,0.24,2}, {-0.08,-0.52,0.12,2},  -- Imbrium/Procellarum/Frigoris
  { 0.18,-0.30,0.15,2}, { 0.34,-0.04,0.16,2}, { 0.60,-0.22,0.09,2},  -- Serenitatis/Tranquillitatis/Crisium
  { 0.40, 0.24,0.10,1}, { 0.16, 0.14,0.08,1}, {-0.28, 0.40,0.12,1},  -- Fecunditatis/Nectaris/Nubium
}
-- Bright rayed craters: Tycho, Copernicus, Kepler.
local CRATERS = { {-0.08,0.62,0.07}, {-0.28,-0.05,0.06}, {-0.52,-0.08,0.04} }

local function clamp(v, lo, hi) return math.max(lo, math.min(hi, v)) end
local function rgb(t) return app.pixelColor.rgba(t[1], t[2], t[3], 255) end

local function albedo(nx, ny)
  -- 1 = bright highland, larger = darker tone index.
  local tone = 1
  for _, m in ipairs(MARIA) do
    local dx, dy = nx - m[1], ny - m[2]
    if dx*dx + dy*dy <= m[3]*m[3] then tone = math.max(tone, 1 + m[4]) end
  end
  for _, c in ipairs(CRATERS) do
    local dx, dy = nx - c[1], ny - c[2]
    if dx*dx + dy*dy <= c[3]*c[3] then tone = 0 end
  end
  return tone
end

local function draw_moon(img, size, angle)
  local r = size / 2
  local sx, sz = math.sin(angle), -math.cos(angle)
  for y = 0, size - 1 do
    for x = 0, size - 1 do
      local nx, ny = (x + 0.5 - r) / r, (y + 0.5 - r) / r
      local d = nx*nx + ny*ny
      if d <= 1 then
        local nz = math.sqrt(1 - d)
        local light = nx*sx + nz*sz
        local tone = albedo(nx, ny)
        if light > 0 then
          -- Darken one tone near the limb and along a soft terminator.
          local shade = clamp(tone, 1, 3) + ((nz < 0.30 or light < 0.18) and 1 or 0)
          if tone == 0 then shade = 1 end
          img:drawPixel(x, y, rgb(LIT[clamp(shade, 1, #LIT)]))
        else
          img:drawPixel(x, y, rgb(DARK[clamp(tone, 1, #DARK)]))  -- earthshine
        end
      end
    end
  end
end

local function moon_sheet(size)
  local sprite = Sprite(size, size, ColorMode.RGB)
  for _ = 2, FRAMES do sprite:newEmptyFrame() end
  local layer = sprite.layers[1]
  layer.name = 'moon'
  for i = 1, FRAMES do
    local img = Image(size, size, ColorMode.RGB)
    draw_moon(img, size, math.rad((i - 1) * 360 / FRAMES))
    sprite:newCel(layer, i, img, Point(0, 0))
    sprite.frames[i].duration = 0.1
  end
  local names = {'New Moon','Waxing Crescent','First Quarter','Waxing Gibbous',
                 'Full Moon','Waning Gibbous','Last Quarter','Waning Crescent'}
  local per = FRAMES / 8
  for k, name in ipairs(names) do
    -- Tags match astronomy.PHASE_NAMES' centred 45-degree bins (wrapping new).
    local first = ((k - 1) * per - per / 2) % FRAMES + 1
    local last = first + per - 1
    if last > FRAMES then first, last = 1, per / 2 end  -- New Moon wraps; tag its opening half
    local tag = sprite:newTag(first, last)
    tag.name = name
  end
  sprite:saveAs(out .. '/moon_' .. size .. '.aseprite')
  sprite:close()
end

local function icon_sheet()
  -- 8x8 event icons: frame 1 full moon, 2 new moon, 3 generic star.
  local sprite = Sprite(8, 8, ColorMode.RGB)
  sprite:newEmptyFrame(); sprite:newEmptyFrame()
  local layer = sprite.layers[1]
  layer.name = 'icons'
  local full = Image(8, 8, ColorMode.RGB); draw_moon(full, 8, math.pi)
  local new = Image(8, 8, ColorMode.RGB)
  for y = 0, 7 do for x = 0, 7 do
    local nx, ny = (x + 0.5 - 4) / 4, (y + 0.5 - 4) / 4
    local d = nx*nx + ny*ny
    if d <= 1 then new:drawPixel(x, y, rgb(d > 0.55 and LIT[4] or DARK[1])) end
  end end
  local star = Image(8, 8, ColorMode.RGB)
  local rows = {'...#....', '...#....', '.#.#.#..', '..###...', '#######.', '..###...', '.#.#.#..', '...#....'}
  for y, row in ipairs(rows) do for x = 1, 8 do
    if row:sub(x, x) == '#' then star:drawPixel(x - 1, y - 1, rgb(LIT[1])) end
  end end
  for i, img in ipairs({full, new, star}) do sprite:newCel(layer, i, img, Point(0, 0)) end
  sprite:saveAs(out .. '/event_icons.aseprite')
  sprite:close()
end

moon_sheet(28)
moon_sheet(16)
icon_sheet()
print('wrote moon_28, moon_16, event_icons to ' .. out)
