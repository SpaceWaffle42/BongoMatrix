(() => {
  'use strict';

  const editorState = new Map();
  const fonts = globalThis.matrixInstalledFonts || [];
  const easings = ['linear', 'ease_in', 'ease_out', 'ease_in_out', 'step'];
  const layerTypes = ['text', 'time', 'pixels', 'line', 'rectangle', 'polygon', 'circle', 'ellipse', 'media'];
  const layerLabels = {time: 'Time', media: 'Image / GIF / MP4'};
  const mediaPreviews = new Map();
  const mediaFailureStates = new Map();
  const mediaLoadQueue = [];
  let activeMediaLoads = 0;
  let layerSerial = 0;
  let mediaRedrawTimer = null;

  const clone = value => structuredClone(value);
  const byId = id => customModules.find(module => module.id === id);
  const safeId = prefix => prefix + Date.now().toString(36) + (layerSerial++).toString(36);
  const stateFor = module => {
    if (!editorState.has(module.id)) editorState.set(module.id, {frame: 0, selected: module.layers[0]?.id, onion: true, clipboard: null, playing: false, timer: null, easing: 'linear', rotationSnap: 15});
    const state = editorState.get(module.id);
    state.frame = Math.min(state.frame, module.frame_count - 1);
    if (!module.layers.some(layer => layer.id === state.selected)) state.selected = module.layers[0]?.id;
    return state;
  };

  const PIXEL_BITMAP_BYTES = 64 * 32 * 4;
  const PIXEL_HISTORY_LIMIT = 32;

  function pythonJsonStringLength(value) {
    let length = 2;
    for (const character of value) {
      const codepoint = character.codePointAt(0);
      if (character === '"' || character === '\\' || character === '\b'
          || character === '\t' || character === '\n' || character === '\f' || character === '\r') length += 2;
      else if (codepoint < 0x20 || codepoint > 0x7e) length += codepoint > 0xffff ? 12 : 6;
      else length++;
    }
    return length;
  }

  function pythonJsonNumberLength(value) {
    if (!Number.isFinite(value)) return 4;
    if (Number.isInteger(value)) return String(value).length;
    let lexical = String(value), sign = '';
    if (lexical[0] === '-') { sign = '-'; lexical = lexical.slice(1); }
    let digits, exponent;
    if (lexical.includes('e')) {
      const [coefficient, power] = lexical.split('e');
      digits = coefficient.replace('.', ''); exponent = Number(power);
    } else {
      const decimal = lexical.indexOf('.');
      const decimalIndex = decimal < 0 ? lexical.length : decimal;
      const allDigits = lexical.replace('.', '');
      const first = allDigits.search(/[1-9]/);
      digits = allDigits.slice(first); exponent = decimalIndex - first - 1;
    }
    let rendered;
    if (exponent >= -4 && exponent < 16) {
      const decimalIndex = exponent + 1;
      if (decimalIndex <= 0) rendered = '0.' + '0'.repeat(-decimalIndex) + digits;
      else if (decimalIndex >= digits.length) rendered = digits + '0'.repeat(decimalIndex - digits.length);
      else rendered = digits.slice(0, decimalIndex) + '.' + digits.slice(decimalIndex);
    } else {
      const coefficient = digits.length === 1 ? digits : digits[0] + '.' + digits.slice(1);
      const magnitude = Math.abs(exponent).toString().padStart(2, '0');
      rendered = coefficient + 'e' + (exponent < 0 ? '-' : '+') + magnitude;
    }
    return sign.length + rendered.length;
  }

  function pythonJsonByteLength(value, inArray = false) {
    if (value === null) return 4;
    if (typeof value === 'string') return pythonJsonStringLength(value);
    if (typeof value === 'number') return pythonJsonNumberLength(value);
    if (typeof value === 'boolean') return value ? 4 : 5;
    if (Array.isArray(value)) {
      return 2 + Math.max(0, value.length - 1) + value.reduce((total, item) =>
        total + ((item === undefined || typeof item === 'function' || typeof item === 'symbol') ? 4 : pythonJsonByteLength(item, true)), 0);
    }
    if (typeof value === 'object') {
      const entries = Object.entries(value).filter(([, item]) => item !== undefined && typeof item !== 'function' && typeof item !== 'symbol');
      return 2 + Math.max(0, entries.length - 1) + entries.reduce((total, [key, item]) =>
        total + pythonJsonStringLength(key) + 1 + pythonJsonByteLength(item), 0);
    }
    return inArray ? 4 : 0;
  }

  function pixelLineCells(startX, startY, endX, endY) {
    let x = Math.round(startX), y = Math.round(startY);
    const targetX = Math.round(endX), targetY = Math.round(endY);
    const dx = Math.abs(targetX - x), stepX = x < targetX ? 1 : -1;
    const dy = -Math.abs(targetY - y), stepY = y < targetY ? 1 : -1;
    let error = dx + dy;
    const cells = [];
    while (true) {
      cells.push([x, y]);
      if (x === targetX && y === targetY) return cells;
      const doubled = error * 2;
      if (doubled >= dy) { error += dy; x += stepX; }
      if (doubled <= dx) { error += dx; y += stepY; }
    }
  }

  const PIXEL_BRUSH_SHAPES = ['square', 'round', 'flat'];

  function clampPixelBrushSize(size) {
    return Math.max(1, Math.min(16, Math.round(Number(size) || 1)));
  }

  // Brush footprint as display-cell offsets around the pointer cell; size 1 is exactly one physical pixel.
  function pixelBrushOffsets(size, shape) {
    const extent = clampPixelBrushSize(size), start = -Math.floor((extent - 1) / 2), offsets = [];
    const rows = shape === 'flat' ? [0] : Array.from({length: extent}, (_, index) => start + index);
    const centre = start + (extent - 1) / 2, radius = extent / 2;
    for (const y of rows) for (let x = start; x < start + extent; x++) {
      if (shape === 'round' && extent > 2 && (x - centre) ** 2 + (y - centre) ** 2 > radius * radius) continue;
      offsets.push([x, y]);
    }
    return offsets;
  }

  // Paint bitmap-local cells. Output stays canonical binary alpha: opaque colour or transparent black.
  function paintPixelCells(bitmap, cells, tool, rgba = [255, 255, 255, 255]) {
    let changed = false;
    for (const [x, y] of cells) {
      if (x < 0 || x >= 64 || y < 0 || y >= 32) continue;
      const byte = (y * 64 + x) * 4;
      let colour;
      if (tool === 'erase') colour = [0, 0, 0, 0];
      else if (tool === 'blend' && bitmap[byte + 3]) colour = [0, 1, 2].map(channel => Math.round((bitmap[byte + channel] + rgba[channel]) / 2)).concat(255);
      else colour = [rgba[0], rgba[1], rgba[2], 255];
      for (let channel = 0; channel < 4; channel++) {
        if (bitmap[byte + channel] !== colour[channel]) { bitmap[byte + channel] = colour[channel]; changed = true; }
      }
    }
    return changed;
  }

  function disposeTimelineResize(state) {
    state.timelineResizeGeneration = (state.timelineResizeGeneration || 0) + 1;
    for (const frame of state.timelineResizeFrames || []) cancelAnimationFrame(frame);
    state.timelineResizeFrames?.clear();
    state.timelineResizeObserver?.disconnect();
    if (state.timelineResizeHandler) {
      window.removeEventListener('resize', state.timelineResizeHandler);
      window.visualViewport?.removeEventListener('resize', state.timelineResizeHandler);
    }
    if (state.timelineResizeTimer) clearTimeout(state.timelineResizeTimer);
    state.timelineResizeHandler = null;
    state.timelineResizeTimer = null;
  }

  function disposeTimelineState(state) {
    cancelTimelineDrag(state);
    disposeTimelineResize(state);
    if (state.pixel) {
      state.pixel.stroke = null;
      state.pixel.saved.clear();
      state.pixel.undo = [];
      state.pixel.redo = [];
      state.pixel = null;
    }
    if (state.timer !== null) clearInterval(state.timer);
    state.timer = null;
    state.playing = false;
    state.timelineContainer = null;
  }

  const editorCleanupObserver = typeof MutationObserver === 'undefined' || !document.documentElement
    ? null
    : new MutationObserver(() => {
      for (const state of editorState.values()) {
        if (state.timelineContainer && !state.timelineContainer.isConnected) disposeTimelineState(state);
      }
    });
  editorCleanupObserver?.observe(document.documentElement, {childList: true, subtree: true});
  window.addEventListener?.('pagehide', () => {
    editorCleanupObserver?.disconnect();
    for (const state of editorState.values()) disposeTimelineState(state);
    if (mediaRedrawTimer !== null) clearInterval(mediaRedrawTimer);
    mediaRedrawTimer = null;
  }, {once: true});

  function ease(value, kind) {
    value = Math.max(0, Math.min(1, value));
    if (kind === 'step') return 0;
    if (kind === 'ease_in') return value * value;
    if (kind === 'ease_out') return 1 - (1 - value) * (1 - value);
    if (kind === 'ease_in_out') return value < .5 ? 2 * value * value : 1 - ((-2 * value + 2) ** 2) / 2;
    return value;
  }

  function isHexColor(value) {
    if (typeof value !== 'string' || value.length !== 7 || value[0] !== '#') return false;
    const hexDigits = '0123456789abcdefABCDEF';
    return [...value.slice(1)].every(character => hexDigits.includes(character));
  }

  function interpolate(left, right, amount) {
    if (typeof left === 'number' && typeof right === 'number') return left + (right - left) * amount;
    if (isHexColor(left) && isHexColor(right)) {
      const channels = [1, 3, 5].map(offset => Math.round(parseInt(left.slice(offset, offset + 2), 16) + (parseInt(right.slice(offset, offset + 2), 16) - parseInt(left.slice(offset, offset + 2), 16)) * amount));
      return '#' + channels.map(value => value.toString(16).padStart(2, '0')).join('');
    }
    if (Array.isArray(left) && Array.isArray(right) && left.length === right.length) return left.map((value, index) => interpolate(value, right[index], amount));
    return amount < 1 ? clone(left) : clone(right);
  }

  function resolvedProperties(module, layer, frame) {
    const result = clone(layer.properties);
    const keyframes = [...layer.keyframes].sort((a, b) => a.frame - b.frame);
    const names = new Set(keyframes.flatMap(keyframe => Object.keys(keyframe.properties)));
    for (const name of names) {
      const values = keyframes.filter(keyframe => Object.hasOwn(keyframe.properties, name));
      const previous = values.filter(keyframe => keyframe.frame <= frame).at(-1);
      const following = values.find(keyframe => keyframe.frame >= frame);
      if (!previous && following) continue;
      else if (previous && !following) result[name] = clone(previous.properties[name]);
      else if (previous && following) {
        if (previous.frame === following.frame) result[name] = clone(previous.properties[name]);
        else if (name === 'z_index') result[name] = clone(previous.properties[name]);
        else result[name] = interpolate(previous.properties[name], following.properties[name], ease((frame - previous.frame) / (following.frame - previous.frame), following.easing));
      }
    }
    const override = module.overrides.find(item => item.frame === frame && item.layer_id === layer.id);
    if (override) Object.assign(result, clone(override.properties));
    return result;
  }

  function orderedLayersForFrame(module, frame) {
    return module.layers.map((layer, index) => {
      const properties = resolvedProperties(module, layer, frame);
      return {layer, properties, index, depth: Object.hasOwn(properties, 'z_index') ? properties.z_index : index};
    }).sort((left, right) => left.depth - right.depth || left.index - right.index);
  }
  window.orderedLayersForFrame = orderedLayersForFrame;

  function saveFrameProperties(module, layer, values) {
    const state = stateFor(module);
    if (state.frame !== 0 || layer.keyframes.some(keyframe => keyframe.frame === 0)) values = animatablePixelProperties(layer, values);
    if (state.frame === 0 && !layer.keyframes.some(keyframe => keyframe.frame === 0)) {
      Object.assign(layer.properties, clone(values));
      return;
    }
    let override = module.overrides.find(item => item.frame === state.frame && item.layer_id === layer.id);
    if (!override) {
      override = {frame: state.frame, layer_id: layer.id, properties: {}};
      module.overrides.push(override);
    }
    Object.assign(override.properties, clone(values));
  }

  function propertyDefaults(type, source) {
    if (type === 'text') return {present: true, x: 2, y: 8, rotation: 0, text: 'TEXT', font: '5x7', colour: '#ffffff'};
    if (type === 'time') return {present: true, x: 2, y: 12, rotation: 0, format: '24h', font: '7x13', colour: '#ffffff'};
    if (type === 'pixels') return {present: true, bitmap_rgba: emptyPixelBitmap(), offset_x: 0, offset_y: 0, rotation: 0};
    if (type === 'line') return {present: true, x1: 20, y1: 16, x2: 44, y2: 16, rotation: 0, width: 1, colour: '#ffffff'};
    if (type === 'rectangle') return {present: true, x: 24, y: 8, width: 16, height: 16, rotation: 0, colour: '#00ffff', filled: false};
    if (type === 'polygon') return {present: true, points: [[32, 6], [44, 25], [20, 25]], rotation: 0, colour: '#ffffff', filled: false};
    if (type === 'circle') return {present: true, x: 32, y: 16, radius: 8, colour: '#ffffff', filled: false};
    if (type === 'media') return {present: true, x: 0, y: 0, width: 64, height: 32, rotation: 0, source: source || mediaItems[0]?.name || '', keep_aspect: true, playback_speed_percent: 100.0};
    return {present: true, x: 32, y: 16, radius_x: 12, radius_y: 6, rotation: 0, colour: '#ffffff', filled: false};
  }

  function newLayer(type, name, source) {
    return {id: safeId('layer_'), name: name || layerLabels[type] || type[0].toUpperCase() + type.slice(1), type, visible: true, properties: propertyDefaults(type, source), keyframes: []};
  }

  // ---- Starter examples -------------------------------------------------------------------
  // Every example is deterministic, uses only saved-schema layer types and stays inside the
  // config_store limits (<=64 layers, <=512 keyframes, <=120 frames, <=8192 points).
  const r2 = value => Math.round(value * 100) / 100;
  const keyframe = (frame, properties, easing = 'linear') => ({frame, easing, properties});

  function starterLayer(type, name, properties = {}, keyframes = []) {
    const layer = newLayer(type, name);
    Object.assign(layer.properties, properties);
    layer.keyframes = keyframes;
    return layer;
  }

  function useStarter(module, settings, layers) {
    Object.assign(module, {background: '#000000', fps: 20, playback: 'loop', ...settings, layers, overrides: []});
  }

  function seededRandom(seed) {
    let state = seed >>> 0;
    return () => {
      state = (Math.imul(state, 1664525) + 1013904223) >>> 0;
      return state / 4294967296;
    };
  }

  // Colour at `frame` along [[frame, '#rrggbb'], ...] stops.
  function colourAt(stops, frame) {
    const next = stops.findIndex(([stop]) => stop >= frame);
    if (next <= 0) return stops[Math.max(0, next)][1];
    const [leftFrame, left] = stops[next - 1], [rightFrame, right] = stops[next];
    return interpolate(left, right, (frame - leftFrame) / (rightFrame - leftFrame));
  }

  // Keyframes for a value that moves `cycles` times through u in [0, 1) per loop and wraps
  // between two adjacent frames, so a starter loops seamlessly with only a few keyframes.
  function wrappingKeyframes(frameCount, phase, cycles, valueAt) {
    const position = frame => (phase + cycles * frame / frameCount) % 1;
    const frames = new Set([0, frameCount - 1]);
    for (let frame = 0; frame < frameCount - 1; frame += 1) {
      if (position(frame + 1) < position(frame)) frames.add(frame).add(frame + 1);
    }
    return [...frames].sort((a, b) => a - b).map(frame => keyframe(frame, valueAt(position(frame))));
  }

  function pixelArtBitmap(rows, palette, left, top) {
    const bytes = new Uint8Array(PIXEL_BITMAP_BYTES);
    rows.forEach((row, y) => [...row].forEach((key, x) => {
      const colour = palette[key], px = left + x, py = top + y;
      if (!colour || px < 0 || px >= 64 || py < 0 || py >= 32) return;
      bytes.set([1, 3, 5].map(offset => parseInt(colour.slice(offset, offset + 2), 16)).concat(255), (py * 64 + px) * 4);
    }));
    return pixelBitmapBase64(bytes);
  }

  function spinningSquare(module) {
    const squares = [
      {size: 22, filled: false, turn: 90, colours: ['#00e5ff', '#ff3df2']},
      {size: 14, filled: false, turn: -90, colours: ['#ffe14d', '#00ff9c']},
      {size: 6, filled: true, turn: 180, colours: ['#ffffff', '#ff6a00']},
    ];
    const layers = squares.map(({size, filled, turn, colours}, index) => starterLayer('rectangle', 'Square ' + (index + 1), {
      x: 32 - size / 2, y: 16 - size / 2, width: size, height: size, rotation: 0, colour: colours[0], filled,
    }, [
      keyframe(0, {rotation: 0, colour: colours[0]}),
      keyframe(24, {rotation: turn / 2, colour: colours[1]}),
      keyframe(47, {rotation: r2(turn * 47 / 48), colour: interpolate(colours[1], colours[0], 23 / 24)}),
    ]));
    useStarter(module, {frame_count: 48, fps: 24}, layers);
  }

  const cubeVertices = [
    [-1, -1, -1], [1, -1, -1], [-1, 1, -1], [1, 1, -1],
    [-1, -1, 1], [1, -1, 1], [-1, 1, 1], [1, 1, 1],
  ];
  const cubeEdges = [
    [0, 1, '#ff0000'], [2, 3, '#ff0000'], [4, 5, '#ff0000'], [6, 7, '#ff0000'],
    [0, 2, '#00ff00'], [1, 3, '#00ff00'], [4, 6, '#00ff00'], [5, 7, '#00ff00'],
    [0, 4, '#0000ff'], [1, 5, '#0000ff'], [2, 6, '#0000ff'], [3, 7, '#0000ff'],
  ];

  function projectedCubeVertices(angle) {
    const yaw = angle * Math.PI / 180, tilt = (22 + 8 * Math.sin(angle * Math.PI / 180)) * Math.PI / 180;
    return cubeVertices.map(([x, y, z]) => {
      const rotatedX = x * Math.cos(yaw) + z * Math.sin(yaw);
      const rotatedZ = -x * Math.sin(yaw) + z * Math.cos(yaw);
      const tiltedY = y * Math.cos(tilt) - rotatedZ * Math.sin(tilt);
      const depth = y * Math.sin(tilt) + rotatedZ * Math.cos(tilt);
      const perspective = 3.2 / (4 + depth);
      return [r2(32 + rotatedX * 12 * perspective), r2(16 + tiltedY * 12 * perspective)];
    });
  }

  function spinningRgbCube(module) {
    const frames = 48, stops = [];
    for (let frame = 0; frame < frames; frame += 3) stops.push(frame);
    stops.push(frames - 1);
    const at = frame => projectedCubeVertices(frame * 360 / frames);
    const initial = at(0);
    const edges = cubeEdges.map(([start, end, colour], index) => starterLayer('line', 'Cube edge ' + (index + 1), {
      x1: initial[start][0], y1: initial[start][1], x2: initial[end][0], y2: initial[end][1], width: 1, colour,
    }, stops.map(frame => {
      const points = at(frame);
      return keyframe(frame, {x1: points[start][0], y1: points[start][1], x2: points[end][0], y2: points[end][1]});
    })));
    const corners = cubeVertices.map((_, vertex) => starterLayer('rectangle', 'Cube corner ' + (vertex + 1), {
      x: initial[vertex][0], y: initial[vertex][1], width: 1, height: 1, rotation: 0, colour: '#ffffff', filled: true,
    }, [0, 6, 12, 18, 24, 30, 36, 42, 47].map(frame => keyframe(frame, {x: at(frame)[vertex][0], y: at(frame)[vertex][1]}))));
    useStarter(module, {background: '#000000', frame_count: frames, fps: 24}, [...edges, ...corners]);
  }

  function linesAndCircle(module) {
    const labels = [['LINE ONE', '#ffffff'], ['LINE TWO', '#00e5ff'], ['LINE THREE', '#ff71ce']].map(([text, colour], index) =>
      starterLayer('text', 'Text ' + (index + 1), {x: 2, y: 8 + index * 10, text, font: '4x6', colour}));
    const divider = starterLayer('line', 'Divider', {x1: 44, y1: 3, x2: 44, y2: 28, width: 1, colour: '#2a2a44'});
    const ring = starterLayer('circle', 'Circle', {x: 54, y: 16, radius: 7, colour: '#ffb300', filled: false}, [
      keyframe(0, {radius: 6, colour: '#ffb300'}),
      keyframe(12, {radius: 8, colour: '#ff5e00'}, 'ease_in_out'),
      keyframe(23, {radius: 6.17, colour: '#ffa804'}, 'ease_in_out'),
    ]);
    const core = starterLayer('circle', 'Circle core', {x: 54, y: 16, radius: 2, colour: '#ffe9a8', filled: true});
    useStarter(module, {background: '#000000', frame_count: 24, fps: 12}, [...labels, divider, ring, core]);
  }

  function matrixRain(module) {
    const frames = 48, random = seededRandom(7);
    const glyphs = ['0', '1', '7', '+', '*', '$', '#', '=', '%', '&', '?', '<'];
    const colours = ['#eafff0', '#3dff8a', '#00b84a', '#00632a'];
    const low = -12, span = 64;
    const layers = [];
    for (let column = 0; column < 13; column += 1) {
      const phase = random(), cycles = random() < .35 ? 2 : 1;
      for (let trail = 0; trail < 4; trail += 1) {
        const glyphPhase = (phase - trail * 7 / span + 1) % 1;
        layers.push(starterLayer('text', `Rain ${column + 1}.${trail + 1}`, {
          x: column * 5, y: r2(low + glyphPhase * span),
          text: glyphs[Math.floor(random() * glyphs.length)], font: '4x6', colour: colours[trail],
        }, wrappingKeyframes(frames, glyphPhase, cycles, u => ({y: r2(low + u * span)}))));
      }
    }
    useStarter(module, {background: '#000300', frame_count: frames, fps: 20}, layers);
  }

  function rainbowPulse(module) {
    const colours = ['#ff004c', '#ff7a00', '#ffe600', '#20e060', '#00d9ff', '#3973ff', '#a855f7'];
    // One keyframe per frame: browser and device then share exact colours (no rounding drift).
    const frames = 42, samples = Array.from({length: frames}, (_, frame) => frame);
    const colourFor = (index, frame) => {
      const position = (frame / frames + index / colours.length) * colours.length % colours.length;
      const left = Math.floor(position);
      return interpolate(colours[left], colours[(left + 1) % colours.length], position - left);
    };
    const layers = colours.map((colour, index) => {
      const base = 3 + index * 3, radiusAt = frame => r2(base + 1.2 * Math.sin(2 * Math.PI * frame / frames - index * .6));
      return starterLayer('circle', `Rainbow ring ${index + 1}`, {
        x: 32, y: 16, radius: radiusAt(0), colour: colourFor(index, 0), filled: index === 0,
      }, samples.map(frame => keyframe(frame, {radius: radiusAt(frame), colour: colourFor(index, frame)})));
    });
    useStarter(module, {background: '#030008', frame_count: frames, fps: 18}, layers);
  }

  function neonClock(module) {
    const frame = starterLayer('rectangle', 'Frame', {x: 0, y: 0, width: 64, height: 32, rotation: 0, colour: '#0a2a4a', filled: false}, [
      keyframe(0, {colour: '#0a2a4a'}), keyframe(60, {colour: '#2a1a5a'}, 'ease_in_out'), keyframe(119, {colour: '#0a2a4a'}, 'ease_in_out'),
    ]);
    const time = starterLayer('time', 'Time', {x: 12, y: 19, format: '24h', font: '8x13B', colour: '#00e5ff'}, [
      keyframe(0, {colour: '#00e5ff'}), keyframe(60, {colour: '#c45cff'}, 'ease_in_out'), keyframe(119, {colour: '#00e5ff'}, 'ease_in_out'),
    ]);
    const track = starterLayer('line', 'Sweep track', {x1: 6, y1: 25, x2: 57, y2: 25, width: 1, colour: '#10283c'});
    const sweep = starterLayer('line', 'Sweep', {x1: 6, y1: 25, x2: 12, y2: 25, width: 1, colour: '#7df9ff'}, [
      keyframe(0, {x1: 6, x2: 12}), keyframe(59, {x1: 51, x2: 57}, 'ease_in_out'), keyframe(119, {x1: 6.01, x2: 12.01}, 'ease_in_out'),
    ]);
    useStarter(module, {background: '#000000', frame_count: 120, fps: 20}, [frame, track, sweep, time]);
  }

  function starfieldWarp(module) {
    const frames = 48, random = seededRandom(11), layers = [];
    for (let star = 0; star < 36; star += 1) {
      const angle = random() * 2 * Math.PI, dx = Math.cos(angle), dy = Math.sin(angle);
      const edge = Math.min(32 / Math.max(Math.abs(dx), 1e-3), 16 / Math.max(Math.abs(dy), 1e-3));
      const maxRadius = Math.min(60, edge / .65 + 2), phase = random(), cycles = random() < .4 ? 2 : 1;
      const valueAt = u => {
        const depth = u * u, radius = 3 + depth * (maxRadius - 3), tail = .5 + radius * .22;
        return {
          x1: r2(32 + dx * (radius - tail)), y1: r2(16 + dy * (radius - tail)),
          x2: r2(32 + dx * radius), y2: r2(16 + dy * radius),
          colour: interpolate('#101830', star % 5 ? '#ffffff' : '#9fd8ff', Math.min(1, u * 1.6)),
        };
      };
      layers.push(starterLayer('line', 'Star ' + (star + 1), {...valueAt(phase), width: 1}, wrappingKeyframes(frames, phase, cycles, valueAt)));
    }
    useStarter(module, {background: '#000006', frame_count: frames, fps: 24}, layers);
  }

  function bouncingBall(module) {
    const frames = 60, poses = {impact: {rx: 6.5, ry: 3.5, y: 25.5}, low: {rx: 5, ry: 5, y: 24}, apex: {rx: 4.6, ry: 5.4, y: 8}};
    const xAt = frame => r2(frame <= 30 ? 8 + 48 * frame / 30 : 56 - 48 * (frame - 30) / 30);
    const plan = [];
    for (let bounce = 0; bounce < 3; bounce += 1) {
      const base = bounce * 20;
      plan.push([base, 'impact', 'linear'], [base + 2, 'low', 'ease_out'], [base + 10, 'apex', 'ease_out'], [base + 18, 'low', 'ease_in']);
    }
    const colours = ['#ff3b3b', '#3bd1ff', '#ffd23b'];
    const ballFrames = plan.map(([frame, pose, easing]) => {
      const values = {x: xAt(frame), y: poses[pose].y, radius_x: poses[pose].rx, radius_y: poses[pose].ry};
      if (frame % 20 === 0) values.colour = colours[frame / 20];
      return keyframe(frame, values, easing);
    });
    ballFrames.push(keyframe(59, {x: xAt(59), y: 24.75, radius_x: 5.75, radius_y: 4.25, colour: interpolate(colours[2], colours[0], .9)}));
    const shadowFrames = ballFrames.map(({frame, easing, properties}) => {
      const height = (properties.y - 8) / (25.5 - 8);
      return keyframe(frame, {x: properties.x, radius_x: r2(2 + 5 * height), colour: interpolate('#0c0c0c', '#3a3a3a', height)}, easing);
    });
    const highlightFrames = ballFrames.map(({frame, easing, properties}) => keyframe(frame, {
      x: r2(properties.x - properties.radius_x * .4), y: r2(properties.y - properties.radius_y * .4),
    }, easing));
    const floor = starterLayer('line', 'Floor', {x1: 0, y1: 31, x2: 63, y2: 31, width: 1, colour: '#1d5c3a'});
    const shadow = starterLayer('ellipse', 'Shadow', {x: 8, y: 30, radius_x: 7, radius_y: 1, rotation: 0, colour: '#3a3a3a', filled: true}, shadowFrames);
    const ball = starterLayer('ellipse', 'Ball', {x: 8, y: 25.5, radius_x: 6.5, radius_y: 3.5, rotation: 0, colour: colours[0], filled: true}, ballFrames);
    const highlight = starterLayer('ellipse', 'Highlight', {x: 5.4, y: 24.1, radius_x: 1, radius_y: 1, rotation: 0, colour: '#ffffff', filled: true}, highlightFrames);
    useStarter(module, {background: '#000000', frame_count: frames, fps: 30}, [floor, shadow, ball, highlight]);
  }

  function equalizerBars(module) {
    const frames = 48, bars = 12, random = seededRandom(5), samples = [];
    for (let frame = 0; frame < frames; frame += 4) samples.push(frame);
    const levels = Array.from({length: bars}, (_, bar) => samples.map((_, sample) => {
      const beat = sample % 3 === 0 && bar < 4 ? 7 : 0;
      return Math.round(Math.max(2, Math.min(29, 20 - bar * 1.1 + beat + (random() - .5) * 16)));
    }));
    const colourFor = height => height < 16 ? interpolate('#00ff6a', '#ffe600', (height - 2) / 14) : interpolate('#ffe600', '#ff2a2a', (height - 16) / 13);
    const layers = [];
    levels.forEach((heights, bar) => {
      const x = 2 + bar * 5;
      const barFrames = samples.map((frame, sample) => keyframe(frame, {y: 32 - heights[sample], height: heights[sample], colour: colourFor(heights[sample])}, 'ease_out'));
      barFrames.push(keyframe(47, {...barFrames[0].properties}));
      layers.push(starterLayer('rectangle', 'Bar ' + (bar + 1), {x, y: 32 - heights[0], width: 4, height: heights[0], rotation: 0, colour: colourFor(heights[0]), filled: true}, barFrames));
      let peak = 0;
      const peaks = [];
      for (let pass = 0; pass < 2; pass += 1) heights.forEach((height, sample) => { peak = Math.max(height, peak - 4); if (pass) peaks[sample] = peak; });
      const peakFrames = samples.map((frame, sample) => keyframe(frame, {y: 30 - peaks[sample]}));
      peakFrames.push(keyframe(47, {y: 30 - peaks[0]}));
      layers.push(starterLayer('rectangle', 'Peak ' + (bar + 1), {x, y: 30 - peaks[0], width: 4, height: 1, rotation: 0, colour: '#ffffff', filled: true}, peakFrames));
    });
    useStarter(module, {background: '#000000', frame_count: frames, fps: 16}, layers);
  }

  function pixelHeart(module) {
    const palette = {X: '#ff2050', H: '#ffc2d4', D: '#a0102e'};
    const big = [
      '..XXX.....XXX..', '.XHHXX...XXXXX.', 'XHHXXXX.XXXXXXX', 'XHXXXXXXXXXXXXX', 'XXXXXXXXXXXXXXX',
      'XXXXXXXXXXXXXXD', '.XXXXXXXXXXXXD.', '..XXXXXXXXXXD..', '...XXXXXXXXD...', '....XXXXXXD....',
      '.....XXXXD.....', '......XXD......', '.......D.......',
    ];
    const small = [
      '.XXX...XXX.', 'XHHXX.XXXXX', 'XHXXXXXXXXX', 'XXXXXXXXXXX', 'XXXXXXXXXXD',
      '.XXXXXXXXD.', '..XXXXXXD..', '...XXXXD...', '....XXD....', '.....D.....',
    ];
    const heart = (name, rows, left, top, shown) => starterLayer('pixels', name, {
      bitmap_rgba: pixelArtBitmap(rows, palette, left, top), offset_x: 0, offset_y: 0, rotation: 0,
    }, [0, 2, 6, 8].map((frame, index) => keyframe(frame, {present: shown === (index % 2 === 0)}, 'step')));
    const ring = (name, start) => starterLayer('circle', name, {present: false, x: 32, y: 16, radius: 9, colour: '#ff2050', filled: false}, [
      keyframe(start, {present: true, radius: 9, colour: '#ff2050'}),
      keyframe(start + 10, {radius: 26, colour: '#08000a'}, 'ease_out'),
      keyframe(start + 11, {present: false}),
    ]);
    useStarter(module, {background: '#08000a', frame_count: 24, fps: 12}, [
      ring('Pulse ring 1', 0), ring('Pulse ring 2', 6),
      heart('Heart (beat)', big, 25, 10, true), heart('Heart (rest)', small, 27, 11, false),
    ]);
  }

  function chompChase(module) {
    const frames = 60, centreY = 15.5;
    const chompX = frame => -8 + frame * 1.8;
    const chomp = (frame, mouth) => {
      const cx = chompX(frame), points = [[r2(cx - 1), centreY]];
      for (let step = 0; step < 15; step += 1) {
        const angle = (mouth + (360 - 2 * mouth) * step / 14) * Math.PI / 180;
        points.push([r2(cx + 6.5 * Math.cos(angle)), r2(centreY - 6.5 * Math.sin(angle))]);
      }
      return points;
    };
    const chompFrames = [];
    for (let frame = 0; frame < frames; frame += 3) chompFrames.push(frame);
    chompFrames.push(frames - 1);
    const mouthAt = frame => 4 + 38 * Math.abs(Math.cos(Math.PI * frame / 6));
    const ghostShape = [[-6, 7], [-6, -1], [-5, -4], [-3, -6], [0, -7], [3, -6], [5, -4], [6, -1], [6, 7], [4, 5], [2, 7], [0, 5], [-2, 7], [-4, 5]];
    const ghostX = frame => chompX(frame) - 21;
    const ghost = (frame, wiggle) => ghostShape.map(([x, y], index) => [r2(ghostX(frame) + x), r2(centreY + y - (index > 7 && wiggle ? (index % 2 ? 2 : -2) * (y > 6 ? 1 : 0) : 0))]);
    const ghostFrames = [0, 6, 12, 18, 24, 30, 36, 42, 48, 54, 59];
    const moving = (name, x, y, width, height, colour, offset) => starterLayer('rectangle', name, {x: r2(x(0) + offset), y, width, height, rotation: 0, colour, filled: true}, [
      keyframe(0, {x: r2(x(0) + offset)}), keyframe(59, {x: r2(x(59) + offset)}),
    ]);
    const walls = [[5, '#2848ff'], [7, '#14248c'], [24, '#14248c'], [26, '#2848ff']].map(([y, colour], index) =>
      starterLayer('line', 'Wall ' + (index + 1), {x1: 0, y1: y, x2: 63, y2: y, width: 1, colour}));
    const dots = Array.from({length: 8}, (_, index) => {
      const x = 5 + index * 8, eaten = Array.from({length: frames}, (_, frame) => frame).find(frame => chompX(frame) >= x + 1);
      return starterLayer('rectangle', 'Dot ' + (index + 1), {x, y: 15, width: 2, height: 2, rotation: 0, colour: '#ffd9b0', filled: true}, [
        keyframe(0, {present: true}, 'step'), keyframe(eaten, {present: false}, 'step'),
      ]);
    });
    const chomper = starterLayer('polygon', 'Chomper', {points: chomp(0, mouthAt(0)), rotation: 0, colour: '#ffe600', filled: true},
      chompFrames.map(frame => keyframe(frame, {points: chomp(frame, mouthAt(frame))})));
    const chaser = starterLayer('polygon', 'Ghost', {points: ghost(0, false), rotation: 0, colour: '#ff3030', filled: true},
      ghostFrames.map((frame, index) => keyframe(frame, {points: ghost(frame, index % 2 === 1)})));
    useStarter(module, {background: '#000000', frame_count: frames, fps: 20}, [
      ...walls, ...dots, chomper,
      moving('Chomper eye', chompX, 11, 1, 1, '#000000', 0), chaser,
      moving('Ghost eye left', ghostX, 11, 2, 3, '#ffffff', -3), moving('Ghost eye right', ghostX, 11, 2, 3, '#ffffff', 1),
      moving('Ghost pupil left', ghostX, 12, 1, 2, '#2040ff', -2), moving('Ghost pupil right', ghostX, 12, 1, 2, '#2040ff', 2),
    ]);
  }

  function fireworks(module) {
    const background = '#000005', layers = [];
    const bursts = [
      {start: 0, x: 18, y: 10, colour: '#ff4d6d'}, {start: 15, x: 46, y: 8, colour: '#4dd2ff'},
      {start: 30, x: 31, y: 12, colour: '#ffd34d'}, {start: 46, x: 52, y: 13, colour: '#7dff6a'},
    ];
    bursts.forEach(({start, x, y, colour}, number) => {
      const burst = start + 8, fade = start + 22, gone = start + 23;
      layers.push(starterLayer('line', `Rocket ${number + 1}`, {present: false, x1: x, y1: 31, x2: x, y2: 34, width: 1, colour: '#ffeeaa'}, [
        keyframe(start, {present: true, y1: 31, y2: 34}),
        keyframe(burst - 1, {y1: y, y2: y + 3}, 'ease_out'),
        keyframe(burst, {present: false}),
      ]));
      layers.push(starterLayer('circle', `Flash ${number + 1}`, {present: false, x, y, radius: 1, colour: '#ffffff', filled: false}, [
        keyframe(burst, {present: true, radius: 1, colour: '#ffffff'}),
        keyframe(burst + 4, {radius: 6, colour}, 'ease_out'),
        keyframe(fade, {radius: 11, colour: background}, 'ease_out'),
        keyframe(gone, {present: false}),
      ]));
      for (let spark = 0; spark < 10; spark += 1) {
        const angle = (spark * 36 + number * 12) * Math.PI / 180, dx = Math.cos(angle), dy = Math.sin(angle);
        const at = (radius, drop) => ({x1: r2(x + dx * radius), y1: r2(y + dy * radius + drop), x2: r2(x + dx * (radius + 2.5)), y2: r2(y + dy * (radius + 2.5) + drop)});
        layers.push(starterLayer('line', `Spark ${number + 1}.${spark + 1}`, {present: false, ...at(1, 0), width: 1, colour: '#ffffff'}, [
          keyframe(burst, {present: true, ...at(1, 0), colour: '#ffffff'}),
          keyframe(burst + 5, {...at(7, .5), colour}, 'ease_out'),
          keyframe(burst + 12, {...at(10, 2), colour: interpolate(colour, '#ffffff', .25)}, 'ease_out'),
          keyframe(fade, {...at(12, 5), colour: background}, 'ease_in'),
          keyframe(gone, {present: false}),
        ]));
      }
    });
    useStarter(module, {background, frame_count: 70, fps: 24}, layers);
  }

  function sunset(module) {
    const sky = [
      ['#1e64c8', '#2a1a5e', '#05051a'], ['#2e78d2', '#5a2a78', '#0a0a2a'], ['#4890dc', '#a03c78', '#16123c'],
      ['#64a6e4', '#e0566a', '#2a1848'], ['#82bcec', '#ff8a50', '#4a1e4a'], ['#a6d2f2', '#ffc04a', '#7a2a40'],
    ];
    const timeline = colours => [keyframe(0, {colour: colours[0]}), keyframe(60, {colour: colours[1]}, 'ease_in_out'), keyframe(119, {colour: colours[2]}, 'ease_in_out')];
    const bands = sky.map((colours, index) => starterLayer('rectangle', 'Sky ' + (index + 1), {x: 0, y: index * 4, width: 64, height: 4, rotation: 0, colour: colours[0], filled: true}, timeline(colours)));
    const random = seededRandom(3);
    const stars = Array.from({length: 8}, (_, index) => starterLayer('rectangle', 'Star ' + (index + 1), {
      present: false, x: Math.floor(2 + random() * 60), y: Math.floor(1 + random() * 12), width: 1, height: 1, rotation: 0, colour: '#30304a', filled: true,
    }, [keyframe(80 + index * 3, {present: true, colour: '#30304a'}), keyframe(119, {colour: '#ffffff'})]));
    const sun = starterLayer('circle', 'Sun', {x: 32, y: 6, radius: 5, colour: '#fff6c0', filled: true}, [
      keyframe(0, {y: 6, colour: '#fff6c0'}), keyframe(60, {y: 22, colour: '#ffb030'}, 'ease_in_out'), keyframe(119, {y: 31, colour: '#ff4020'}, 'ease_in'),
    ]);
    const sea = starterLayer('rectangle', 'Sea', {x: 0, y: 24, width: 64, height: 8, rotation: 0, colour: '#2a6ab0', filled: true}, timeline(['#2a6ab0', '#3a2a60', '#0a0a20']));
    const glints = [[26, 6], [28, 4], [30, 2]].map(([y, half], index) => {
      const colours = [[0, '#a8d0ff'], [60, '#ffb030'], [119, '#5a1a10']];
      return starterLayer('line', 'Glint ' + (index + 1), {x1: 32 - half, y1: y, x2: 32 + half, y2: y, width: 1, colour: '#a8d0ff'},
        Array.from({length: 13}, (_, step) => {
          const frame = Math.min(119, step * 10), shift = (step + index) % 2 ? 1 : -1;
          return keyframe(frame, {x1: 32 - half + shift, x2: 32 + half + shift, colour: colourAt(colours, frame)}, 'ease_in_out');
        }));
    });
    useStarter(module, {background: '#000000', frame_count: 120, fps: 15, playback: 'ping_pong'}, [...bands, ...stars, sun, sea, ...glints]);
  }

  function rainbowMarquee(module) {
    const colours = ['#ff004c', '#ff7a00', '#ffe600', '#20e060', '#00d9ff', '#3973ff', '#a855f7'];
    const textFrames = colours.map((colour, index) => keyframe(index * 17, {colour}));
    textFrames[0].properties.x = 54;
    textFrames.push(keyframe(119, {x: -104, colour: colours[0]}));
    const text = starterLayer('text', 'Marquee', {x: 54, y: 21, text: 'HELLO MATRIX', font: '9x15B', colour: colours[0]}, textFrames);
    const stripe = (name, y, shift) => starterLayer('line', name, {x1: 0, y1: y, x2: 63, y2: y, width: 1, colour: colours[shift]},
      colours.map((_, index) => keyframe(index * 17, {colour: interpolate(colours[(index + shift) % 7], '#000000', .55)})).concat(keyframe(119, {colour: interpolate(colours[shift], '#000000', .55)})));
    useStarter(module, {background: '#000000', frame_count: 120, fps: 30}, [stripe('Top stripe', 2, 3), stripe('Bottom stripe', 29, 5), text]);
  }

  const canvasStarterExamples = Object.freeze([
    Object.freeze({id: 'spinning_square', label: 'Spinning squares'}),
    Object.freeze({id: 'spinning_rgb_cube', label: 'Spinning RGB cube'}),
    Object.freeze({id: 'three_lines_circle', label: 'Three lines and circle'}),
    Object.freeze({id: 'matrix_rain', label: 'Matrix rain'}),
    Object.freeze({id: 'rainbow_pulse', label: 'Rainbow pulse'}),
    Object.freeze({id: 'neon_clock', label: 'Neon clock'}),
    Object.freeze({id: 'starfield_warp', label: 'Starfield warp'}),
    Object.freeze({id: 'bouncing_ball', label: 'Bouncing ball'}),
    Object.freeze({id: 'equalizer_bars', label: 'Equalizer bars'}),
    Object.freeze({id: 'pixel_heart', label: 'Pixel heart'}),
    Object.freeze({id: 'chomp_chase', label: 'Chomp chase'}),
    Object.freeze({id: 'fireworks', label: 'Fireworks'}),
    Object.freeze({id: 'sunset', label: 'Sunset'}),
    Object.freeze({id: 'rainbow_marquee', label: 'Rainbow marquee'}),
  ]);
  const canvasTemplateBuilders = Object.freeze({
    spinning_square: spinningSquare,
    spinning_rgb_cube: spinningRgbCube,
    three_lines_circle: linesAndCircle,
    matrix_rain: matrixRain,
    rainbow_pulse: rainbowPulse,
    neon_clock: neonClock,
    starfield_warp: starfieldWarp,
    bouncing_ball: bouncingBall,
    equalizer_bars: equalizerBars,
    pixel_heart: pixelHeart,
    chomp_chase: chompChase,
    fireworks,
    sunset,
    rainbow_marquee: rainbowMarquee,
  });

  function publishCanvasDraftMutation(id) {
    if (globalThis.matrixCustomCanvasReady === true
        && typeof globalThis.matrixCustomCanvasDraftChanged === 'function') {
      globalThis.matrixCustomCanvasDraftChanged(id, 150);
    }
  }

  function applyTemplate(id, template) {
    const module = byId(id);
    if (!module || !confirm('Replace this canvas with the ' + template.replaceAll('_', ' ') + ' example?')) return;
    const builder = canvasTemplateBuilders[template];
    if (typeof builder === 'function') builder(module);
    else if (template === 'blank') Object.assign(module, {background: '#000000', frame_count: 1, fps: 10, playback: 'hold', layers: [newLayer('text', 'Text')], overrides: []});
    else return;
    const previousState = editorState.get(module.id);
    if (previousState) disposeTimelineState(previousState);
    editorState.delete(module.id);
    renderCustomModules();
    globalThis.markMatrixDirty?.();
    publishCanvasDraftMutation(module.id);
    setMessage('Starter example loaded locally. Save configuration to apply.');
  }
  window.canvasStarterExamples = canvasStarterExamples;
  window.applyCanvasTemplate = applyTemplate;

  function convertToAdvanced(id) {
    const index = customModules.findIndex(module => module.id === id);
    const source = customModulesFromEditor().find(module => module.id === id);
    if (index < 0 || !source || !confirm('Convert ' + source.name + ' to the advanced canvas? This cannot be changed back to simple text. Basic border styling is not carried into Advanced Canvas.')) return;
    customModules[index] = {
      id: source.id, name: source.name, type: 'canvas', background: source.background,
      frame_count: 1, fps: Math.max(1, Math.min(60, Math.round(1 / source.speed))), playback: 'hold',
      layers: [{id: safeId('layer_'), name: 'Text', type: 'text', visible: true, properties: {x: source.x, y: source.y, text: source.text, font: source.font, colour: source.colour}, keyframes: []}],
      overrides: [],
    };
    renderCustomModules();
    globalThis.markMatrixDirty?.();
    publishCanvasDraftMutation(source.id);
    setMessage('Converted to an advanced layered canvas. Save configuration to apply.');
  }

  function simpleModuleFromCard(card) {
    const type = card.querySelector('[data-field=type]').value;
    const name = card.querySelector('[data-field=name]').value.trim();
    // While the Mode select fires `input` (before its `change` re-render), the card still shows the
    // previous subtype's fields; keep the stored module until the new fields exist.
    const rendered = card.querySelector(type === 'media' ? '[data-field=source]' : '[data-field=text]');
    if (!rendered) {
      const stored = customModules.find(module => module.id === card.dataset.id);
      if (stored) return structuredClone(stored);
    }
    if (type === 'media') {
      return {
        id: card.dataset.id, name, type,
        source: card.querySelector('[data-field=source]').value,
        fit: card.querySelector('[data-field=fit]').value,
        x: Number(card.querySelector('[data-field=x]').value),
        y: Number(card.querySelector('[data-field=y]').value),
        width: Number(card.querySelector('[data-field=width]').value),
        height: Number(card.querySelector('[data-field=height]').value),
        loops: Number(card.querySelector('[data-field=loops]').value),
        playback_speed_percent: Number(card.querySelector('[data-field=playback_speed_percent]').value),
      };
    }
    return {id: card.dataset.id, name, type, text: card.querySelector('[data-field=text]').value, font: card.querySelector('[data-field=font]').value, colour: card.querySelector('[data-field=colour]').value, background: card.querySelector('[data-field=background]').value, border: card.querySelector('[data-field=border]').value, border_colour: card.querySelector('[data-field=border_colour]').value, x: Number(card.querySelector('[data-field=x]').value), y: Number(card.querySelector('[data-field=y]').value), direction: card.querySelector('[data-field=direction]').value, speed: Number(card.querySelector('[data-field=speed]').value), loops: Number(card.querySelector('[data-field=loops]').value)};
  }

  const provisionalSimpleDrafts = new Map([...document.querySelectorAll('.custom-module-card')]
    .filter(card => customModules.find(module => module.id === card.dataset.id)?.type !== 'canvas')
    .map(card => [card.dataset.id, simpleModuleFromCard(card)]));
  customModules = customModules.map(module => module.type === 'canvas' ? module : (provisionalSimpleDrafts.get(module.id) || module));
  const basicMediaSources = new Map(customModules
    .filter(module => module.type === 'media')
    .map(module => [module.id, module.source]));

  window.customModulesFromEditor = function customModulesFromEditor() {
    const simple = new Map([...document.querySelectorAll('.custom-module-card[data-editor-type="simple"]')].map(card => [card.dataset.id, simpleModuleFromCard(card)]));
    return customModules.map(module => module.type === 'canvas' ? clone(module) : (simple.get(module.id) || clone(module)));
  };

  function headerFor(module, card, advanced) {
    const head = document.createElement('div'); head.className = advanced ? 'advanced-head' : 'custom-module-head';
    const identity = document.createElement('div');
    const title = document.createElement('strong'); title.textContent = module.name;
    const id = document.createElement('div'); id.className = 'custom-module-id mono'; id.textContent = module.id;
    identity.append(title, id);
    const actions = document.createElement('div'); actions.className = 'row';
    if (advanced) { const badge = document.createElement('span'); badge.className = 'advanced-badge'; badge.textContent = 'Advanced canvas'; actions.append(badge); }
    actions.append(makeButton('Remove', () => removeCustomModule(module.id), 'btn danger'));
    head.append(identity, actions); card.append(head);
    return title;
  }

  function basicModuleForType(module, type) {
    const identity = {id: module.id, name: module.name};
    const previousMediaSource = basicMediaSources.get(module.id) || (module.type === 'media' ? module.source : null);
    if (previousMediaSource) basicMediaSources.set(module.id, previousMediaSource);
    if (type === 'media') {
      const playable = item => basicMediaChoices().includes(item.name);
      const source = previousMediaSource || mediaItems.find(item => playable(item) && !item.built_in)?.name || mediaItems.find(playable)?.name || '';
      if (source) basicMediaSources.set(module.id, source);
      return {...identity, type, source, fit: 'contain', x: 0, y: 0, width: 64, height: 32, loops: 1, playback_speed_percent: 100.0};
    }
    return {...identity, type, text: 'HELLO MATRIX', font: '5x7', colour: '#ffffff', background: '#000000', border: 'none', border_colour: '#ffffff', x: 2, y: 20, direction: 'left', speed: .05, loops: 1};
  }

  function replaceBasicSubtype(module, type) {
    const index = customModules.findIndex(item => item.id === module.id);
    if (index < 0) return null;
    const replacement = basicModuleForType(module, type);
    customModules.splice(index, 1, replacement);
    return replacement;
  }

  function renderSimple(module, list) {
    const card = document.createElement('div'); card.className = 'custom-module-card'; card.dataset.id = module.id; card.dataset.editorType = 'simple';
    const title = headerFor(module, card, false);
    const fields = document.createElement('div'); fields.className = 'custom-module-fields';
    const name = customControl('Name', 'name', 'text', module.name); name.classList.add('wide');
    name.querySelector('input').addEventListener('input', event => { module.name = event.target.value; title.textContent = event.target.value; syncCustomModuleUI(); });
    const mode = customControl('Mode', 'type', 'select', module.type, ['text', 'text_scroll', 'media']);
    mode.querySelector('select').addEventListener('change', event => {
      const mediaSource = card.querySelector('[data-field="source"]')?.value;
      if (mediaSource) basicMediaSources.set(module.id, mediaSource);
      replaceBasicSubtype(module, event.target.value);
      renderCustomModules();
      document.querySelector(`.custom-module-card[data-id="${CSS.escape(module.id)}"] [data-field="type"]`)?.focus();
    });
    if (module.type === 'media') {
      // Stills (PNG/JPEG/WebP) share the GIF/MP4 fit workflow; loops and speed only apply to animation.
      const loopsControl = customControl('Loops', 'loops', 'number', module.loops ?? 1);
      const speedControl = customControl('Playback speed (%)', 'playback_speed_percent', 'playback_speed', module.playback_speed_percent ?? 100.0);
      for (const control of [loopsControl, speedControl]) control.setAttribute('data-static-image-hidden', '');
      const fitNote = document.createElement('p'); fitNote.className = 'editor-note wide media-fit-note'; fitNote.setAttribute('aria-live', 'polite');
      fields.append(name, mode,
        customControl('Media file', 'source', 'select', module.source, basicMediaChoices()),
        customControl('Fit', 'fit', 'select', module.fit, ['contain', 'cover', 'stretch', 'manual']),
        customControl('X position', 'x', 'number', module.x), customControl('Y position', 'y', 'number', module.y),
        customControl('Width', 'width', 'number', module.width), customControl('Height', 'height', 'number', module.height),
        loopsControl, speedControl, fitNote);
      const syncStatic = () => {
        const value = field => fields.querySelector(`[data-field="${field}"]`)?.value;
        const current = {source: value('source'), fit: value('fit'), x: value('x'), y: value('y'), width: value('width'), height: value('height')};
        const still = isStaticMediaSource(current.source);
        loopsControl.hidden = speedControl.hidden = still;
        fitNote.textContent = mediaFitNote(current) + (still ? ' Still image: set how long it shows with the playlist duration.' : '');
        runtime.hidden = still;
      };
      fields.addEventListener('input', syncStatic); fields.addEventListener('change', syncStatic);
      queueMicrotask(syncStatic);
    } else {
      const text = customControl('Text', 'text', 'text', module.text); text.classList.add('wide');
      fields.append(name, text, mode, customControl('Font', 'font', 'select', module.font, fonts), customControl('Text colour', 'colour', 'color', module.colour), customControl('X position', 'x', 'number', module.x), customControl('Baseline Y', 'y', 'number', module.y), customControl('Scroll direction', 'direction', 'select', module.direction, ['left', 'right']), customControl('Frame delay (seconds)', 'speed', 'number', module.speed), customControl('Scroll loops', 'loops', 'number', module.loops ?? 1));
    }
    for (const control of fields.querySelectorAll('[data-field]')) control.addEventListener('input', syncPlaylistDurations);
    const animatedMedia = module.type === 'media' && !isStaticMediaSource(module.source);
    const runtimeValue = module.type === 'text_scroll' ? calculatedScrollDuration(module) : (animatedMedia ? calculatedMediaDuration(module) : Number.NaN);
    const runtime = scrollPhysicalTimeReadout(runtimeValue); runtime.hidden = !(module.type === 'text_scroll' || animatedMedia);
    card.append(fields);
    if (module.type !== 'media') card.append(simpleAppearanceGroup(module));
    card.append(runtime);
    if (module.type !== 'media') { const convert = makeButton('Convert to advanced canvas', () => convertToAdvanced(module.id), 'btn primary'); convert.style.marginTop = '12px'; card.append(convert); }
    list.append(card);
  }

  function moduleControl(labelText, value, type, handler, options) {
    const label = document.createElement('label'); label.textContent = labelText;
    const control = document.createElement(options ? 'select' : 'input');
    if (options) for (const optionValue of options) { const option = document.createElement('option'); option.value = optionValue; option.textContent = labelText === 'Rotation snap' ? (optionValue === 'off' ? 'Off' : optionValue + '°') : optionValue.replaceAll('_', ' '); control.append(option); }
    else control.type = type;
    if (type === 'checkbox') control.checked = Boolean(value); else control.value = value;
    control.dataset.control = labelText;
    control.addEventListener(type === 'checkbox' ? 'change' : 'input', event => handler(type === 'checkbox' ? event.target.checked : (type === 'number' ? Number(event.target.value) : event.target.value)));
    label.append(control); return label;
  }

  function pointsBounds(points) {
    return {minX: Math.min(...points.map(point => point[0])), minY: Math.min(...points.map(point => point[1])), maxX: Math.max(...points.map(point => point[0])), maxY: Math.max(...points.map(point => point[1]))};
  }

  function pythonRound(value) {
    const lower = Math.floor(value), fraction = value - lower;
    if (fraction < .5) return lower;
    if (fraction > .5) return lower + 1;
    return lower % 2 === 0 ? lower : lower + 1;
  }

  function pixelBitmapCells(properties) {
    const packed = atob(properties.bitmap_rgba), cells = [];
    const offsetX = properties.offset_x || 0, offsetY = properties.offset_y || 0;
    for (let index = 0; index < 64 * 32; index++) {
      const byte = index * 4, alpha = packed.charCodeAt(byte + 3);
      if (alpha) cells.push([
        index % 64 + offsetX, Math.floor(index / 64) + offsetY,
        packed.charCodeAt(byte), packed.charCodeAt(byte + 1), packed.charCodeAt(byte + 2),
      ]);
    }
    if (properties.rotation && cells.length) {
      const box = pointsBounds(cells);
      const centreX = (box.minX + box.maxX) / 2;
      const centreY = (box.minY + box.maxY) / 2;
      const radians = properties.rotation * Math.PI / 180;
      const sine = Math.sin(radians), cosine = Math.cos(radians);
      for (const cell of cells) {
        const x = cell[0], y = cell[1];
        cell[0] = pythonRound(centreX + (x - centreX) * cosine - (y - centreY) * sine);
        cell[1] = pythonRound(centreY + (x - centreX) * sine + (y - centreY) * cosine);
      }
    } else {
      for (const cell of cells) { cell[0] = pythonRound(cell[0]); cell[1] = pythonRound(cell[1]); }
    }
    return cells.filter(([x, y]) => x >= 0 && x < 64 && y >= 0 && y < 32);
  }
  window.pixelBitmapCells = pixelBitmapCells;

  function currentTimeText(format, now = new Date()) {
    let hours = now.getHours();
    const minutes = String(now.getMinutes()).padStart(2, '0');
    const seconds = String(now.getSeconds()).padStart(2, '0');
    const withSeconds = format.endsWith('_seconds');
    if (format.startsWith('12h')) {
      const suffix = hours >= 12 ? 'PM' : 'AM';
      hours = hours % 12 || 12;
      return `${hours}:${minutes}${withSeconds ? `:${seconds}` : ''} ${suffix}`;
    }
    return `${String(hours).padStart(2, '0')}:${minutes}${withSeconds ? `:${seconds}` : ''}`;
  }

  function snapRotation(rotation, increment) {
    return increment ? Math.round(rotation / increment) * increment : Math.round(rotation);
  }

  function bounds(properties, type) {
    if (type === 'text' || type === 'time') { const text = type === 'time' ? currentTimeText(properties.format) : properties.text; return {minX: properties.x, minY: properties.y - 7, maxX: properties.x + Math.max(4, text.length * 5), maxY: properties.y}; }
    if (type === 'pixels' && properties.bitmap_rgba) {
      const cells = pixelBitmapCells(properties);
      return cells.length ? pointsBounds(cells) : {minX: properties.offset_x || 0, minY: properties.offset_y || 0, maxX: properties.offset_x || 0, maxY: properties.offset_y || 0};
    }
    if (type === 'pixels' || type === 'polygon') return pointsBounds(properties.points);
    if (type === 'line') return pointsBounds([[properties.x1, properties.y1], [properties.x2, properties.y2]]);
    if (type === 'rectangle' || type === 'media') return {minX: properties.x, minY: properties.y, maxX: properties.x + properties.width, maxY: properties.y + properties.height};
    if (type === 'circle') return {minX: properties.x - properties.radius, minY: properties.y - properties.radius, maxX: properties.x + properties.radius, maxY: properties.y + properties.radius};
    return {minX: properties.x - properties.radius_x, minY: properties.y - properties.radius_y, maxX: properties.x + properties.radius_x, maxY: properties.y + properties.radius_y};
  }

  function supportsRotation(type) {
    return ['text', 'time', 'pixels', 'line', 'rectangle', 'polygon', 'ellipse', 'media'].includes(type);
  }

  function beginLayerRotation(context, properties, type) {
    if (!['text', 'time', 'pixels', 'line'].includes(type) || !Number(properties.rotation)) return;
    const box = bounds(properties, type), centreX = (box.minX + box.maxX) / 2, centreY = (box.minY + box.maxY) / 2;
    context.translate(centreX, centreY);
    context.rotate(Number(properties.rotation) * Math.PI / 180);
    context.translate(-centreX, -centreY);
  }
  window.beginLayerRotation = beginLayerRotation;

  function mediaTimelineRevision(source) { return mediaItems.find(item => item.name === source)?.revision || 'unversioned'; }

  function mediaTimelineUrl(source, frameCount, fps, playbackSpeedPercent = 100.0, revision = mediaTimelineRevision(source)) { return '/api/media-timeline/' + source.split('/').map(encodeURIComponent).join('/') + '?frame_count=' + frameCount + '&fps=' + fps + '&playback_speed_percent=' + Number(playbackSpeedPercent).toFixed(1) + '&revision=' + encodeURIComponent(revision); }

  function currentCanvasMediaSources() {
    const sources = new Set();
    const maximumSources = 20 * (64 + 512 + 512);
    const remember = source => {
      if (typeof source === 'string' && source && sources.size < maximumSources) sources.add(source);
    };
    for (const module of customModules) {
      if (module.type !== 'canvas') continue;
      const mediaLayerIds = new Set();
      for (const layer of module.layers) {
        if (layer.type !== 'media') continue;
        mediaLayerIds.add(layer.id);
        remember(layer.properties?.source);
        for (const keyframe of layer.keyframes || []) remember(keyframe.properties?.source);
      }
      for (const override of module.overrides || []) {
        if (mediaLayerIds.has(override.layer_id)) remember(override.properties?.source);
      }
    }
    return sources;
  }

  function pruneMediaFailureStates() {
    const referenced = currentCanvasMediaSources();
    for (const source of mediaFailureStates.keys()) {
      if (!referenced.has(source)) mediaFailureStates.delete(source);
    }
    return referenced;
  }


  function mediaFrameIndex(layer, frame) {
    let startFrame = 0;
    if (layer.properties?.present === false) {
      const appearances = (layer.keyframes || []).filter(keyframe => keyframe.properties?.present === true).map(keyframe => keyframe.frame);
      if (appearances.length) startFrame = Math.min(...appearances);
    }
    return Math.max(0, frame - startFrame);
  }

  function redrawMediaCanvases() {
    for (const card of document.querySelectorAll('.advanced-card')) {
      const module = byId(card.dataset.id), canvas = card.querySelector('canvas');
      if (module && canvas) drawEditor(module, canvas);
    }
  }

  function mediaTimelineElement(source, frameCount, fps, playbackSpeedPercent = 100.0) {
    const revision = mediaTimelineRevision(source);
    const key = source + '|' + revision + '|' + frameCount + '|' + fps + '|' + Number(playbackSpeedPercent).toFixed(1);
    if (mediaPreviews.has(key)) return mediaPreviews.get(key);
    const previousFailure = mediaFailureStates.get(source);
    if (previousFailure && previousFailure.revision !== revision) mediaFailureStates.delete(source);
    if (mediaFailureStates.get(source)?.terminal) return null;
    if (mediaPreviews.size >= 32) {
      const oldestKey = mediaPreviews.keys().next().value;
      const oldest = mediaPreviews.get(oldestKey);
      const queued = mediaLoadQueue.findIndex(item => item.element === oldest);
      if (queued >= 0) mediaLoadQueue.splice(queued, 1);
      else if (oldest) oldest.src = '';
      mediaPreviews.delete(oldestKey);
    }
    const element = document.createElement('img');
    const url = mediaTimelineUrl(source, frameCount, fps, playbackSpeedPercent, revision);
    mediaLoadQueue.push({element, url, key, source, revision});
    pumpMediaLoadQueue();
    mediaPreviews.set(key, element);
    return element;
  }

  function pumpMediaLoadQueue() {
    while (activeMediaLoads < 2 && mediaLoadQueue.length) {
      const {element, url, key, source, revision} = mediaLoadQueue.shift();
      activeMediaLoads++;
      let completed = false;
      const complete = redraw => {
        if (completed) return;
        completed = true;
        activeMediaLoads--;
        if (redraw) redrawMediaCanvases();
        pumpMediaLoadQueue();
      };
      element.addEventListener('load', () => {
        const failure = mediaFailureStates.get(source);
        if (failure?.revision === revision && !failure.terminal) mediaFailureStates.delete(source);
        complete(true);
      }, {once: true});
      element.addEventListener('error', () => {
        if (mediaPreviews.get(key) !== element) {
          complete(false);
          return;
        }
        mediaPreviews.delete(key);
        const referenced = pruneMediaFailureStates();
        if (!referenced.has(source)) {
          complete(false);
          return;
        }
        const failure = mediaFailureStates.get(source);
        const attempts = failure?.revision === revision ? failure.attempts : 0;
        if (attempts < 1) {
          mediaFailureStates.set(source, {revision, attempts: attempts + 1, terminal: false});
          complete(false);
          setTimeout(redrawMediaCanvases, 250);
        } else {
          mediaFailureStates.set(source, {revision, attempts: attempts + 1, terminal: true});
          complete(false);
        }
      }, {once: true});
      element.src = url;
    }
  }

  function drawMediaFrame(context, media, properties, mediaFrame, frameCount) {
    const naturalWidth = media.naturalWidth, naturalHeight = media.naturalHeight / frameCount;
    if (!naturalWidth || !naturalHeight) return null;
    const pythonRound = value => {
      const lower = Math.floor(value), fraction = value - lower;
      if (fraction < .5) return lower;
      if (fraction > .5) return lower + 1;
      return lower % 2 === 0 ? lower : lower + 1;
    };
    const x = pythonRound(properties.x), y = pythonRound(properties.y);
    let width = pythonRound(properties.width), height = pythonRound(properties.height);
    if (properties.keep_aspect) {
      const boxWidth = width, boxHeight = height;
      if (boxWidth >= naturalWidth && boxHeight >= naturalHeight) {
        width = naturalWidth; height = naturalHeight;
      } else {
        const aspect = naturalWidth / naturalHeight;
        if (boxWidth / boxHeight >= aspect) {
          height = boxHeight;
          const exactWidth = height * aspect;
          const lower = Math.floor(exactWidth), upper = Math.ceil(exactWidth);
          width = Math.max(1,
            Math.abs(aspect - lower / height) <= Math.abs(aspect - upper / height)
              ? lower : upper);
        } else {
          width = boxWidth;
          const exactHeight = width / aspect;
          const lower = Math.floor(exactHeight), upper = Math.ceil(exactHeight);
          const error = candidate => candidate === 0 ? 0 : Math.abs(aspect - width / candidate);
          height = Math.max(1, error(lower) <= error(upper) ? lower : upper);
        }
      }
    }
    context.beginPath(); context.rect(x, y, width, height); context.clip();
    context.translate(x + width / 2, y + height / 2); context.rotate(properties.rotation * Math.PI / 180);
    context.drawImage(media, 0, mediaFrame * naturalHeight, naturalWidth, naturalHeight, -width / 2, -height / 2, width, height);
    return {x, y, width, height};
  }

  function drawShape(context, layer, properties, alpha = 1, frame = 0, fps = 1, frameCount = 1) {
    context.save(); context.globalAlpha = alpha; context.strokeStyle = properties.colour; context.fillStyle = properties.colour; context.lineWidth = Math.max(.12, layer.type === 'line' && properties.width || .12);
    const packedPixels = layer.type === 'pixels' && properties.bitmap_rgba;
    if (!packedPixels) beginLayerRotation(context, properties, layer.type);
    if (layer.type === 'text' || layer.type === 'time') { const text = layer.type === 'time' ? currentTimeText(properties.format) : properties.text; context.font = `${Math.max(4, Number(properties.font.split('x')[1]?.replace(/\D/g, '') || 7))}px monospace`; context.textBaseline = 'alphabetic'; context.fillText(text, properties.x, properties.y); }
    else if (layer.type === 'pixels' && packedPixels) for (const [x, y, red, green, blue] of pixelBitmapCells(properties)) { context.fillStyle = `rgb(${red},${green},${blue})`; context.fillRect(x, y, 1, 1); }
    else if (layer.type === 'pixels') for (const [x, y] of properties.points) context.fillRect(Math.round(x), Math.round(y), 1, 1);
    else if (layer.type === 'line') { context.beginPath(); context.moveTo(properties.x1, properties.y1); context.lineTo(properties.x2, properties.y2); context.stroke(); }
    else if (layer.type === 'rectangle') { context.translate(properties.x + properties.width / 2, properties.y + properties.height / 2); context.rotate(properties.rotation * Math.PI / 180); properties.filled ? context.fillRect(-properties.width / 2, -properties.height / 2, properties.width, properties.height) : context.strokeRect(-properties.width / 2, -properties.height / 2, properties.width, properties.height); }
    else if (layer.type === 'polygon') { const box = pointsBounds(properties.points); context.translate((box.minX + box.maxX) / 2, (box.minY + box.maxY) / 2); context.rotate(properties.rotation * Math.PI / 180); context.translate(-(box.minX + box.maxX) / 2, -(box.minY + box.maxY) / 2); context.beginPath(); properties.points.forEach((point, index) => index ? context.lineTo(...point) : context.moveTo(...point)); context.closePath(); properties.filled ? context.fill() : context.stroke(); }
    else if (layer.type === 'circle') { context.beginPath(); context.arc(properties.x, properties.y, properties.radius, 0, Math.PI * 2); properties.filled ? context.fill() : context.stroke(); }
    else if (layer.type === 'ellipse') { context.beginPath(); context.ellipse(properties.x, properties.y, properties.radius_x, properties.radius_y, properties.rotation * Math.PI / 180, 0, Math.PI * 2); properties.filled ? context.fill() : context.stroke(); }
    else if (layer.type === 'media' && properties.source) {
      const media = mediaTimelineElement(properties.source, frameCount, fps, properties.playback_speed_percent);
      if (!media) { context.restore(); return; }
      drawMediaFrame(context, media, properties, mediaFrameIndex(layer, frame) % frameCount, frameCount);
    }
    context.restore();
  }
  if (typeof window !== 'undefined') window.drawCanvasShape = drawShape;

  function drawEditor(module, canvas) {
    const state = stateFor(module), context = canvas.getContext('2d');
    context.setTransform(10, 0, 0, 10, 0, 0); context.imageSmoothingEnabled = false; context.fillStyle = module.background; context.fillRect(0, 0, 64, 32);
    if (state.onion) for (const onionFrame of [state.frame - 1, state.frame + 1]) if (onionFrame >= 0 && onionFrame < module.frame_count) for (const {layer, properties} of orderedLayersForFrame(module, onionFrame)) { if (layer.visible && properties.present !== false) drawShape(context, layer, properties, .18, onionFrame, module.fps, module.frame_count); }
    for (const {layer, properties} of orderedLayersForFrame(module, state.frame)) { if (layer.visible && properties.present !== false) drawShape(context, layer, properties, 1, state.frame, module.fps, module.frame_count); }
    const selected = module.layers.find(layer => layer.id === state.selected);
    if (selected?.type === 'pixels' && state.pixel?.grid) {
      context.save(); context.strokeStyle = 'rgba(255,255,255,.14)'; context.lineWidth = .06; context.beginPath();
      for (let x = 1; x < 64; x++) { context.moveTo(x, 0); context.lineTo(x, 32); }
      for (let y = 1; y < 32; y++) { context.moveTo(0, y); context.lineTo(64, y); }
      context.stroke(); context.restore();
    }
    if (selected && !(selected.type === 'pixels' && state.pixel && state.pixel.tool !== 'move')) {
      const box = bounds(resolvedProperties(module, selected, state.frame), selected.type); context.save(); context.strokeStyle = '#00ffff'; context.fillStyle = '#00ffff'; context.lineWidth = .12; context.setLineDash([.5, .35]); context.strokeRect(box.minX, box.minY, Math.max(1, box.maxX - box.minX), Math.max(1, box.maxY - box.minY)); context.setLineDash([]); context.fillRect(box.maxX - .7, box.maxY - .7, 1.4, 1.4); if (supportsRotation(selected.type)) { context.beginPath(); context.arc((box.minX + box.maxX) / 2, box.minY - 2.5, .75, 0, Math.PI * 2); context.fill(); } context.restore();
    }
  }

  function moveProperties(properties, type, dx, dy) {
    if (['text', 'time', 'rectangle', 'circle', 'ellipse', 'media'].includes(type)) { properties.x += dx; properties.y += dy; }
    else if (type === 'line') { properties.x1 += dx; properties.y1 += dy; properties.x2 += dx; properties.y2 += dy; }
    else if (type === 'pixels' && properties.bitmap_rgba) { properties.offset_x = pythonRound((properties.offset_x || 0) + dx); properties.offset_y = pythonRound((properties.offset_y || 0) + dy); }
    else properties.points = properties.points.map(([x, y]) => [x + dx, y + dy]);
  }

  function canvasPointerPoint(canvas, event) {
    const box = canvas.getBoundingClientRect();
    return {
      x: Math.max(-128, Math.min(191, (event.clientX - box.left) * 64 / box.width)),
      y: Math.max(-128, Math.min(191, (event.clientY - box.top) * 32 / box.height)),
    };
  }

  function resizeProperties(properties, type, current, dx, dy) {
    if (type === 'rectangle' || type === 'media') {
      if (type === 'media' && properties.keep_aspect) {
        const scale = Math.max(.01, Math.max((properties.width + dx) / properties.width, (properties.height + dy) / properties.height));
        properties.width = Math.max(1, Math.round(properties.width * scale)); properties.height = Math.max(1, Math.round(properties.height * scale));
      } else {
        properties.width = Math.max(1, Math.round(properties.width + dx)); properties.height = Math.max(1, Math.round(properties.height + dy));
      }
    }
    else if (type === 'circle') properties.radius = Math.max(1, Math.round(Math.hypot(current.x - properties.x, current.y - properties.y)));
    else if (type === 'ellipse') { properties.radius_x = Math.max(1, Math.round(Math.abs(current.x - properties.x))); properties.radius_y = Math.max(1, Math.round(Math.abs(current.y - properties.y))); }
    else if (type === 'line') { properties.x2 = current.x; properties.y2 = current.y; }
  }

  function bindCanvasPointerTransforms(module, canvas) {
    const state = stateFor(module);
    const point = event => canvasPointerPoint(canvas, event);
    canvas.addEventListener('contextmenu', event => { if (selectedPixelLayer(module)) event.preventDefault(); });
    canvas.addEventListener('pointerleave', () => { if (state.pixel && !state.pixel.stroke) { state.pixel.hover = null; updatePixelCursor(module); } });
    canvas.addEventListener('pointerdown', event => {
      if (handlePixelPointerDown(module, canvas, event)) return;
      const layer = module.layers.find(item => item.id === state.selected); if (!layer) return;
      canvas.setPointerCapture(event.pointerId); const current = point(event), properties = resolvedProperties(module, layer, state.frame), box = bounds(properties, layer.type), centre = {x: (box.minX + box.maxX) / 2, y: (box.minY + box.maxY) / 2};
      let mode = 'move';
      if (supportsRotation(layer.type) && Math.hypot(current.x - centre.x, current.y - (box.minY - 2.5)) < 2) { properties.rotation ??= 0; mode = 'rotate'; }
      else if (Math.hypot(current.x - box.maxX, current.y - box.maxY) < 2) mode = 'resize';
      state.drag = {mode, start: current, original: clone(properties), centre}; drawEditor(module, canvas); syncPropertyInputs(module);
    });
    canvas.addEventListener('pointermove', event => {
      if (!state.drag && handlePixelPointerMove(module, canvas, event)) return;
      if (!state.drag) return; const layer = module.layers.find(item => item.id === state.selected); if (!layer) return;
      const current = point(event), drag = state.drag, properties = clone(drag.original), dx = current.x - drag.start.x, dy = current.y - drag.start.y;
      if (drag.mode === 'move') moveProperties(properties, layer.type, dx, dy);
      else if (drag.mode === 'rotate' && Object.hasOwn(properties, 'rotation')) { const rawRotation = Math.atan2(current.y - drag.centre.y, current.x - drag.centre.x) * 180 / Math.PI + 90; properties.rotation = snapRotation(rawRotation, state.rotationSnap); }
      else if (drag.mode === 'resize') resizeProperties(properties, layer.type, current, dx, dy);
      saveFrameProperties(module, layer, animatablePixelProperties(layer, properties)); drawEditor(module, canvas); syncPropertyInputs(module);
    });
    const end = commit => event => {
      if (handlePixelPointerEnd(module, event, commit)) return;
      if (state.drag) { state.drag = null; refreshAdvanced(module.id, true); }
    };
    canvas.addEventListener('pointerup', end(true)); canvas.addEventListener('pointercancel', end(false));
    canvas.addEventListener('lostpointercapture', end(true));
  }

  globalThis.matrixMediaCanvasTransforms = Object.freeze({
    drawMediaFrame,
    canvasPointerPoint,
    moveProperties,
    resizeProperties,
    bindCanvasPointerTransforms,
  });

  const fieldDefinitions = {
    text: [['text', 'Text', 'text'], ['font', 'Font', 'select'], ['x', 'X', 'number'], ['y', 'Baseline Y', 'number'], ['rotation', 'Rotation', 'number'], ['colour', 'Colour', 'color']],
    time: [['format', 'Time format', 'time-format'], ['font', 'Font', 'select'], ['x', 'X', 'number'], ['y', 'Baseline Y', 'number'], ['rotation', 'Rotation', 'number'], ['colour', 'Colour', 'color']],
    pixels: [['offset_x', 'Offset X', 'number'], ['offset_y', 'Offset Y', 'number'], ['rotation', 'Rotation', 'number']],
    line: [['x1', 'Start X', 'number'], ['y1', 'Start Y', 'number'], ['x2', 'End X', 'number'], ['y2', 'End Y', 'number'], ['rotation', 'Rotation', 'number'], ['width', 'Width', 'number'], ['colour', 'Colour', 'color']],
    rectangle: [['x', 'X', 'number'], ['y', 'Y', 'number'], ['width', 'Width', 'number'], ['height', 'Height', 'number'], ['rotation', 'Rotation', 'number'], ['colour', 'Colour', 'color'], ['filled', 'Filled', 'checkbox']],
    polygon: [['points', 'Points (x,y per line)', 'points'], ['rotation', 'Rotation', 'number'], ['colour', 'Colour', 'color'], ['filled', 'Filled', 'checkbox']],
    circle: [['x', 'Centre X', 'number'], ['y', 'Centre Y', 'number'], ['radius', 'Radius', 'number'], ['colour', 'Colour', 'color'], ['filled', 'Filled', 'checkbox']],
    ellipse: [['x', 'Centre X', 'number'], ['y', 'Centre Y', 'number'], ['radius_x', 'Radius X', 'number'], ['radius_y', 'Radius Y', 'number'], ['rotation', 'Rotation', 'number'], ['colour', 'Colour', 'color'], ['filled', 'Filled', 'checkbox']],
    media: [['source', 'Media file', 'media-select'], ['playback_speed_percent', 'Playback speed (%)', 'playback-speed'], ['x', 'X', 'number'], ['y', 'Y', 'number'], ['width', 'Width', 'number'], ['height', 'Height', 'number'], ['rotation', 'Rotation', 'number'], ['keep_aspect', 'Keep aspect ratio', 'checkbox']],
  };

  function parsePoints(value) { return value.split(/\n|;/).map(line => line.trim()).filter(Boolean).map(line => line.split(',').map(Number)).filter(point => point.length === 2 && point.every(Number.isFinite)); }
  function formatPoints(points) { return points.map(point => point.map(value => Math.round(value * 100) / 100).join(',')).join('\n'); }

  function pixelBytes(properties) {
    const bytes = new Uint8Array(PIXEL_BITMAP_BYTES);
    if (properties.bitmap_rgba) {
      const packed = atob(properties.bitmap_rgba);
      for (let index = 0; index < bytes.length; index++) bytes[index] = packed.charCodeAt(index);
      return bytes;
    }
    const colour = hexToRgba(properties.colour || '#ffffff');
    for (const [rawX, rawY] of properties.points || []) {
      const x = Math.round(rawX), y = Math.round(rawY);
      if (x < 0 || x >= 64 || y < 0 || y >= 32) continue;
      bytes.set(colour, (y * 64 + x) * 4);
    }
    return bytes;
  }

  function pixelBitmapBase64(bytes) {
    let packed = '';
    for (let offset = 0; offset < bytes.length; offset += 1024) {
      packed += String.fromCharCode(...bytes.subarray(offset, offset + 1024));
    }
    return btoa(packed);
  }

  function legacyPixelBitmap(properties) {
    const bytes = new Uint8Array(PIXEL_BITMAP_BYTES);
    const points = (properties.points || []).map(point => [Number(point[0]), Number(point[1])]);
    if (!points.length) return bytes;
    const box = pointsBounds(points), centreX = (box.minX + box.maxX) / 2, centreY = (box.minY + box.maxY) / 2;
    const radians = Number(properties.rotation || 0) * Math.PI / 180;
    const sine = Math.sin(radians), cosine = Math.cos(radians), colour = hexToRgba(properties.colour || '#ffffff');
    for (const [rawX, rawY] of points) {
      const x = pythonRound(centreX + (rawX - centreX) * cosine - (rawY - centreY) * sine);
      const y = pythonRound(centreY + (rawX - centreX) * sine + (rawY - centreY) * cosine);
      if (x >= 0 && x < 64 && y >= 0 && y < 32) bytes.set(colour, (y * 64 + x) * 4);
    }
    return bytes;
  }

  function legacyPixelFlatteningRequired(module, layer) {
    if (Number(layer.properties.rotation || 0)) return true;
    const partial = properties => ['points', 'colour', 'rotation'].some(name => Object.hasOwn(properties || {}, name));
    return layer.keyframes.some(item => partial(item.properties))
      || module.overrides.some(item => item.layer_id === layer.id && partial(item.properties));
  }

  function bakeLegacyPixelLayer(module, layer, properties) {
    if (layer.properties.bitmap_rgba) return true;
    if (legacyPixelFlatteningRequired(module, layer)
        && !confirm('Editing these pixels will flatten point, colour, or rotation changes on other frames. Continue?')) return false;
    const bytes = legacyPixelBitmap(properties);
    layer.properties.bitmap_rgba = pixelBitmapBase64(bytes);
    layer.properties.offset_x = 0; layer.properties.offset_y = 0; layer.properties.rotation = 0;
    delete layer.properties.points; delete layer.properties.colour;
    for (const keyframe of layer.keyframes) for (const name of ['points', 'colour', 'rotation']) delete keyframe.properties[name];
    for (const override of module.overrides) if (override.layer_id === layer.id) {
      for (const name of ['points', 'colour', 'rotation']) delete override.properties[name];
    }
    layer.keyframes = layer.keyframes.filter(item => Object.keys(item.properties).length);
    module.overrides = module.overrides.filter(item => Object.keys(item.properties).length);
    return true;
  }

  function savedPixelLayerSnapshot(module, layer) {
    return {
      layer: clone(layer),
      overrides: module.overrides.flatMap((override, index) => override.layer_id === layer.id ? [{index, value: clone(override)}] : []),
    };
  }

  function restoreSavedPixelLayer(module, layer, snapshot) {
    if (!snapshot?.layer || snapshot.layer.id !== layer.id || !Array.isArray(snapshot.overrides)) return false;
    for (const key of Object.keys(layer)) delete layer[key];
    Object.assign(layer, clone(snapshot.layer));
    module.overrides = module.overrides.filter(override => override.layer_id !== layer.id);
    for (const entry of snapshot.overrides) module.overrides.splice(Math.min(entry.index, module.overrides.length), 0, clone(entry.value));
    return true;
  }

  globalThis.matrixPixelLayerLifecycle = Object.freeze({
    bakeLegacy(module, layer, frame = 0) {
      return bakeLegacyPixelLayer(module, layer, resolvedProperties(module, layer, frame));
    },
    bitmapBase64: pixelBitmapBase64,
    savedSnapshot: savedPixelLayerSnapshot,
    restoreSaved: restoreSavedPixelLayer,
  });

  function hexToRgba(hex) {
    const value = /^#[0-9a-f]{6}$/i.test(hex) ? hex : '#ffffff';
    return [1, 3, 5].map(offset => parseInt(value.slice(offset, offset + 2), 16)).concat(255);
  }

  function rgbToHex(value) {
    if (/^#[0-9a-f]{6}$/i.test(value)) return value.toLowerCase();
    const channels = String(value).match(/\d+/g)?.slice(0, 3).map(Number);
    return channels?.length === 3 ? '#' + channels.map(channel => channel.toString(16).padStart(2, '0')).join('') : null;
  }

  let emptyPixelBitmapCache = null;
  function emptyPixelBitmap() {
    emptyPixelBitmapCache ??= pixelBitmapBase64(new Uint8Array(PIXEL_BITMAP_BYTES));
    return emptyPixelBitmapCache;
  }

  // Bitmap layers keep their pixels only in base properties; keyframes and overrides carry placement.
  function animatablePixelProperties(layer, properties) {
    if (layer.type !== 'pixels' || !layer.properties.bitmap_rgba) return properties;
    const result = {...properties};
    for (const name of ['bitmap_rgba', 'points', 'colour']) delete result[name];
    return result;
  }

  const PIXEL_TOOLS = ['move', 'draw', 'erase', 'blend', 'pick'];
  const PIXEL_TOOL_LABELS = {move: 'Move', draw: 'Draw', erase: 'Erase', blend: 'Blend', pick: 'Pick colour'};
  const PIXEL_TOOL_ICONS = {move: '✥', draw: '✎', erase: '⌫', blend: '◐', pick: '⌾'};
  const PIXEL_SHORTCUTS = {m: 'move', d: 'draw', e: 'erase', b: 'blend', i: 'pick'};

  function capturePixelBaselines(module, pixel) {
    pixel.saved.clear();
    for (const layer of module.layers) if (layer.type === 'pixels') pixel.saved.set(layer.id, savedPixelLayerSnapshot(module, layer));
  }

  // Per-canvas pixel state lives with the editor state, so card re-renders never lose history or the saved baseline.
  function pixelState(module) {
    const state = stateFor(module);
    if (!state.pixel) {
      const legacyColour = module.layers.find(item => item.id === state.selected && item.type === 'pixels')?.properties.colour;
      state.pixel = {tool: 'draw', colour: /^#[0-9a-f]{6}$/i.test(legacyColour || '') ? legacyColour.toLowerCase() : '#ffffff', size: 1, shape: 'square', grid: false, menu: false,
        undo: [], redo: [], stroke: null, hover: null, saved: new Map()};
      capturePixelBaselines(module, state.pixel);
    }
    return state.pixel;
  }

  function selectedPixelLayer(module) {
    const state = stateFor(module), layer = module.layers.find(item => item.id === state.selected);
    return layer?.type === 'pixels' ? layer : null;
  }

  function pixelPainting(module) {
    return Boolean(selectedPixelLayer(module)) && pixelState(module).tool !== 'move';
  }

  function canvasCard(module) {
    return document.querySelector(`.custom-module-card[data-id="${CSS.escape(module.id)}"]`);
  }

  function redrawCanvasStage(module) {
    const stage = canvasCard(module)?.querySelector('.editor-canvas');
    if (stage) drawEditor(module, stage);
  }

  // Freeze the layer's displayed placement at stroke start so a stroke stays stable while bounds change.
  function pixelTransform(module, layer, anchor) {
    const properties = resolvedProperties(module, layer, stateFor(module).frame);
    const offsetX = Number(properties.offset_x || 0), offsetY = Number(properties.offset_y || 0);
    const rotation = Number(properties.rotation || 0);
    let centreX = anchor[0], centreY = anchor[1];
    if (rotation && layer.properties.bitmap_rgba) {
      const bytes = pixelBytes(layer.properties);
      let minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity;
      for (let index = 0; index < 64 * 32; index++) {
        if (!bytes[index * 4 + 3]) continue;
        const x = index % 64 + offsetX, y = Math.floor(index / 64) + offsetY;
        minX = Math.min(minX, x); minY = Math.min(minY, y); maxX = Math.max(maxX, x); maxY = Math.max(maxY, y);
      }
      if (minX !== Infinity) { centreX = (minX + maxX) / 2; centreY = (minY + maxY) / 2; }
    }
    return {offsetX, offsetY, rotation, centreX, centreY};
  }

  // Inverse of pixelBitmapCells: a displayed 64x32 cell to the bitmap-local cell that renders there.
  function pixelDisplayToLocal(transform, x, y) {
    let displayX = x, displayY = y;
    if (transform.rotation) {
      const radians = -transform.rotation * Math.PI / 180, sine = Math.sin(radians), cosine = Math.cos(radians);
      const dx = x - transform.centreX, dy = y - transform.centreY;
      displayX = pythonRound(transform.centreX + dx * cosine - dy * sine);
      displayY = pythonRound(transform.centreY + dx * sine + dy * cosine);
    }
    return [pythonRound(displayX - transform.offsetX), pythonRound(displayY - transform.offsetY)];
  }

  function pushPixelHistory(pixel, entry) {
    pixel.undo.push(entry);
    if (pixel.undo.length > PIXEL_HISTORY_LIMIT) pixel.undo.shift();
    pixel.redo = [];
  }

  function pixelHistoryEntry(module, layer, kind) {
    return {layerId: layer.id, kind, value: kind === 'layer' ? savedPixelLayerSnapshot(module, layer) : layer.properties.bitmap_rgba};
  }

  function publishPixelChange(module) {
    globalThis.markMatrixDirty?.();
    publishCanvasDraftMutation(module.id);
    syncPixelToolbar(module);
  }

  // Legacy point layers are baked once, on the first edit; the pre-bake layer becomes the undo entry.
  function preparePixelBitmap(module, layer) {
    if (layer.properties.bitmap_rgba) return {ok: true, legacy: null};
    const before = savedPixelLayerSnapshot(module, layer);
    if (!bakeLegacyPixelLayer(module, layer, resolvedProperties(module, layer, stateFor(module).frame))) return {ok: false, legacy: null};
    return {ok: true, legacy: before};
  }

  function beginPixelStroke(module, layer, tool, cell) {
    const pixel = pixelState(module);
    if (pixel.stroke) finishPixelStroke(module);
    const prepared = preparePixelBitmap(module, layer);
    if (!prepared.ok) return false;
    pixel.stroke = {
      layerId: layer.id, tool, rgba: hexToRgba(pixel.colour), offsets: pixelBrushOffsets(pixel.size, pixel.shape),
      visited: new Set(), previous: null, legacy: prepared.legacy, before: layer.properties.bitmap_rgba,
      bytes: pixelBytes(layer.properties), transform: pixelTransform(module, layer, cell), pointerId: null, canvas: null,
    };
    extendPixelStroke(module, cell);
    return true;
  }

  function extendPixelStroke(module, cell) {
    const stroke = editorState.get(module.id)?.pixel?.stroke;
    if (!stroke) return false;
    const layer = module.layers.find(item => item.id === stroke.layerId);
    if (!layer) { editorState.get(module.id).pixel.stroke = null; return false; }
    const centres = stroke.previous ? pixelLineCells(...stroke.previous, ...cell) : [cell];
    stroke.previous = cell;
    const cells = [];
    for (const [centreX, centreY] of centres) for (const [offsetX, offsetY] of stroke.offsets) {
      const x = centreX + offsetX, y = centreY + offsetY;
      if (x < 0 || x >= 64 || y < 0 || y >= 32) continue;
      const key = y * 64 + x;
      if (stroke.visited.has(key)) continue;
      stroke.visited.add(key);
      cells.push(pixelDisplayToLocal(stroke.transform, x, y));
    }
    if (!paintPixelCells(stroke.bytes, cells, stroke.tool, stroke.rgba)) return false;
    layer.properties.bitmap_rgba = pixelBitmapBase64(stroke.bytes);
    redrawCanvasStage(module);
    return true;
  }

  function releasePixelPointer(stroke) {
    try { if (stroke.canvas?.hasPointerCapture?.(stroke.pointerId)) stroke.canvas.releasePointerCapture(stroke.pointerId); } catch (_) { /* Capture already ended. */ }
  }

  function finishPixelStroke(module) {
    const pixel = editorState.get(module.id)?.pixel, stroke = pixel?.stroke;
    if (!stroke) return false;
    pixel.stroke = null;
    releasePixelPointer(stroke);
    const layer = module.layers.find(item => item.id === stroke.layerId);
    if (!layer) return false;
    if (layer.properties.bitmap_rgba === stroke.before) {
      if (stroke.legacy) { restoreSavedPixelLayer(module, layer, stroke.legacy); redrawCanvasStage(module); }
      return false;
    }
    pushPixelHistory(pixel, stroke.legacy
      ? {layerId: layer.id, kind: 'layer', value: stroke.legacy}
      : {layerId: layer.id, kind: 'bitmap', value: stroke.before});
    publishPixelChange(module);
    return true;
  }

  function cancelPixelStroke(module) {
    const pixel = editorState.get(module.id)?.pixel, stroke = pixel?.stroke;
    if (!stroke) return false;
    pixel.stroke = null;
    releasePixelPointer(stroke);
    const layer = module.layers.find(item => item.id === stroke.layerId);
    if (!layer) return false;
    if (stroke.legacy) restoreSavedPixelLayer(module, layer, stroke.legacy);
    else layer.properties.bitmap_rgba = stroke.before;
    redrawCanvasStage(module);
    return true;
  }

  function stepPixelHistory(module, direction) {
    finishPixelStroke(module);
    const pixel = pixelState(module), from = direction < 0 ? pixel.undo : pixel.redo, to = direction < 0 ? pixel.redo : pixel.undo;
    while (from.length) {
      const entry = from.pop(), layer = module.layers.find(item => item.id === entry.layerId && item.type === 'pixels');
      if (!layer) continue;
      to.push(pixelHistoryEntry(module, layer, entry.kind));
      if (entry.kind === 'layer') restoreSavedPixelLayer(module, layer, entry.value);
      else layer.properties.bitmap_rgba = entry.value;
      stateFor(module).selected = layer.id;
      globalThis.markMatrixDirty?.();
      refreshAdvanced(module.id, true);
      return true;
    }
    syncPixelToolbar(module);
    return false;
  }

  // Fill paints every displayed cell through the layer transform; Clear empties the whole bitmap.
  function fillPixelLayer(module, rgba) {
    const layer = selectedPixelLayer(module);
    if (!layer) return false;
    finishPixelStroke(module);
    const pixel = pixelState(module), prepared = preparePixelBitmap(module, layer);
    if (!prepared.ok) return false;
    const previous = layer.properties.bitmap_rgba;
    let bytes = new Uint8Array(PIXEL_BITMAP_BYTES);
    if (rgba) {
      bytes = pixelBytes(layer.properties);
      const transform = pixelTransform(module, layer, [32, 16]), cells = [];
      for (let y = 0; y < 32; y++) for (let x = 0; x < 64; x++) cells.push(pixelDisplayToLocal(transform, x, y));
      paintPixelCells(bytes, cells, 'draw', rgba);
    }
    const encoded = pixelBitmapBase64(bytes);
    if (encoded === previous) {
      if (prepared.legacy) restoreSavedPixelLayer(module, layer, prepared.legacy);
      return false;
    }
    layer.properties.bitmap_rgba = encoded;
    pushPixelHistory(pixel, prepared.legacy
      ? {layerId: layer.id, kind: 'layer', value: prepared.legacy}
      : {layerId: layer.id, kind: 'bitmap', value: previous});
    redrawCanvasStage(module);
    publishPixelChange(module);
    return true;
  }

  function resetPixelLayer(module) {
    const layer = selectedPixelLayer(module);
    if (!layer) return false;
    finishPixelStroke(module);
    const pixel = pixelState(module), saved = pixel.saved.get(layer.id);
    if (!saved) return false;
    const current = savedPixelLayerSnapshot(module, layer);
    if (JSON.stringify(current) === JSON.stringify(saved)) return false;
    restoreSavedPixelLayer(module, layer, saved);
    pushPixelHistory(pixel, {layerId: layer.id, kind: 'layer', value: current});
    globalThis.markMatrixDirty?.();
    refreshAdvanced(module.id, true);
    return true;
  }

  // The eyedropper samples the composited 64x32 output (background and every visible layer).
  function pickCompositeColour(module, x, y) {
    const canvas = document.createElement('canvas'); canvas.width = 64; canvas.height = 32;
    const context = canvas.getContext('2d', {willReadFrequently: true}), state = stateFor(module);
    context.fillStyle = module.background; context.fillRect(0, 0, 64, 32);
    for (const {layer, properties} of orderedLayersForFrame(module, state.frame)) {
      if (layer.visible && properties.present !== false) drawShape(context, layer, properties, 1, state.frame, module.fps, module.frame_count);
    }
    const data = context.getImageData(x, y, 1, 1).data;
    return '#' + [data[0], data[1], data[2]].map(value => value.toString(16).padStart(2, '0')).join('');
  }

  function setPixelTool(module, tool) {
    if (!PIXEL_TOOLS.includes(tool)) return;
    finishPixelStroke(module);
    pixelState(module).tool = tool;
    syncPixelToolbar(module);
    redrawCanvasStage(module);
  }

  function setPixelBrushSize(module, size) {
    pixelState(module).size = clampPixelBrushSize(size);
    syncPixelToolbar(module);
  }

  function pixelCellAt(canvas, event) {
    const box = canvas.getBoundingClientRect();
    return [
      Math.max(0, Math.min(63, Math.floor((event.clientX - box.left) * 64 / box.width))),
      Math.max(0, Math.min(31, Math.floor((event.clientY - box.top) * 32 / box.height))),
    ];
  }

  function updatePixelCursor(module) {
    const card = canvasCard(module), cursor = card?.querySelector('.pixel-cursor');
    if (!cursor) return;
    const pixel = pixelState(module), painting = pixelPainting(module) && pixel.hover;
    cursor.hidden = !painting;
    if (painting) {
      const offsets = pixel.tool === 'pick' ? [[0, 0]] : pixelBrushOffsets(pixel.size, pixel.shape);
      const xs = offsets.map(item => item[0] + pixel.hover[0]), ys = offsets.map(item => item[1] + pixel.hover[1]);
      const left = Math.max(0, Math.min(...xs)), top = Math.max(0, Math.min(...ys));
      const right = Math.min(63, Math.max(...xs)), bottom = Math.min(31, Math.max(...ys));
      Object.assign(cursor.style, {left: left / 64 * 100 + '%', top: top / 32 * 100 + '%',
        width: (right - left + 1) / 64 * 100 + '%', height: (bottom - top + 1) / 32 * 100 + '%'});
    }
    const status = card.querySelector('.pixel-status');
    if (status) {
      const brush = pixel.tool === 'pick' || pixel.tool === 'move' ? '' : ` · ${pixel.size}px ${pixel.shape}`;
      status.textContent = PIXEL_TOOL_LABELS[pixel.tool] + (pixel.hover ? ` · X ${pixel.hover[0]} · Y ${pixel.hover[1]}` : ' · move over the canvas') + brush;
    }
  }

  function syncPixelToolbar(module) {
    const card = canvasCard(module), root = card?.querySelector('.pixel-editor');
    if (!root) return;
    const pixel = pixelState(module);
    for (const button of root.querySelectorAll('[data-pixel-tool]')) button.setAttribute('aria-pressed', String(button.dataset.pixelTool === pixel.tool));
    const colour = root.querySelector('[data-pixel-action="colour"]'); if (colour && colour.value !== pixel.colour) colour.value = pixel.colour;
    const size = root.querySelector('[data-pixel-action="size"]'); if (size && document.activeElement !== size) size.value = String(pixel.size);
    const shape = root.querySelector('[data-pixel-action="shape"]'); if (shape) shape.value = pixel.shape;
    root.querySelector('[data-pixel-action="undo"]').disabled = !pixel.undo.length;
    root.querySelector('[data-pixel-action="redo"]').disabled = !pixel.redo.length;
    const layer = selectedPixelLayer(module), reset = root.querySelector('[data-pixel-more="reset"]');
    if (reset) reset.disabled = !layer || !pixel.saved.has(layer.id);
    const grid = root.querySelector('[data-pixel-more="grid"]'); if (grid) grid.setAttribute('aria-pressed', String(pixel.grid));
    const menu = root.querySelector('.pixel-more-menu'), more = root.querySelector('[data-pixel-action="more"]');
    if (menu) menu.hidden = !pixel.menu;
    if (more) more.setAttribute('aria-expanded', String(pixel.menu));
    const wrap = card.querySelector('.editor-canvas-wrap');
    wrap?.classList.toggle('pixel-painting', pixelPainting(module));
    wrap?.classList.toggle('pixel-grid-on', pixel.grid && Boolean(layer));
    updatePixelCursor(module);
  }

  function renderPixelToolbar(module) {
    const pixel = pixelState(module), root = document.createElement('section');
    root.className = 'pixel-editor'; root.setAttribute('aria-label', 'Pixel drawing editor');
    const toolbar = document.createElement('div'); toolbar.className = 'pixel-toolbar'; toolbar.setAttribute('role', 'toolbar'); toolbar.setAttribute('aria-label', 'Pixel drawing tools');
    const button = (action, label, text, handler, parent = toolbar) => {
      const item = document.createElement('button'); item.type = 'button'; item.className = 'btn pixel-tool';
      item.dataset.pixelAction = action; item.setAttribute('aria-label', label); item.title = label; item.textContent = text;
      item.addEventListener('click', handler); parent.append(item); return item;
    };
    const shortcutFor = tool => Object.keys(PIXEL_SHORTCUTS).find(key => PIXEL_SHORTCUTS[key] === tool).toUpperCase();
    for (const tool of PIXEL_TOOLS) {
      const item = button(tool, PIXEL_TOOL_LABELS[tool], `${PIXEL_TOOL_ICONS[tool]} ${PIXEL_TOOL_LABELS[tool].split(' ')[0]}`, () => setPixelTool(module, tool));
      item.dataset.pixelTool = tool; item.title = `${PIXEL_TOOL_LABELS[tool]} (${shortcutFor(tool)})`;
    }
    const colour = document.createElement('input'); colour.type = 'color'; colour.className = 'pixel-colour'; colour.dataset.pixelAction = 'colour';
    colour.value = pixel.colour; colour.title = 'Colour'; colour.setAttribute('aria-label', 'Colour');
    colour.addEventListener('input', () => { pixel.colour = colour.value; if (!['draw', 'blend'].includes(pixel.tool)) setPixelTool(module, 'draw'); });
    const size = document.createElement('input'); size.type = 'number'; size.min = '1'; size.max = '16'; size.step = '1'; size.className = 'pixel-size';
    size.dataset.pixelAction = 'size'; size.value = String(pixel.size); size.title = 'Brush size ([ and ])'; size.setAttribute('aria-label', 'Brush size');
    size.addEventListener('input', () => { if (size.value !== '') pixel.size = clampPixelBrushSize(size.value); updatePixelCursor(module); });
    size.addEventListener('change', () => setPixelBrushSize(module, size.value));
    const shape = document.createElement('select'); shape.className = 'pixel-shape'; shape.dataset.pixelAction = 'shape'; shape.title = 'Brush shape'; shape.setAttribute('aria-label', 'Brush shape');
    for (const value of PIXEL_BRUSH_SHAPES) { const option = document.createElement('option'); option.value = value; option.textContent = value[0].toUpperCase() + value.slice(1); shape.append(option); }
    shape.value = pixel.shape; shape.addEventListener('change', () => { pixel.shape = shape.value; syncPixelToolbar(module); });
    toolbar.append(colour, size, shape);
    button('undo', 'Undo (Ctrl/Cmd + Z)', '↶', () => stepPixelHistory(module, -1));
    button('redo', 'Redo (Ctrl/Cmd + Shift + Z)', '↷', () => stepPixelHistory(module, 1));
    const more = button('more', 'More pixel actions', 'More', () => { pixel.menu = !pixel.menu; syncPixelToolbar(module); });
    const menuId = 'pixel-more-' + module.id; more.setAttribute('aria-controls', menuId);

    const swatches = document.createElement('div'); swatches.className = 'pixel-swatches'; swatches.setAttribute('aria-label', 'Matrix colour swatches');
    const matrixColours = [...document.querySelectorAll('#lava-palettes .swatch')].map(item => rgbToHex(getComputedStyle(item).backgroundColor)).filter(Boolean);
    for (const value of [...new Set(matrixColours)].slice(0, 12)) {
      const swatch = document.createElement('button'); swatch.type = 'button'; swatch.className = 'pixel-swatch'; swatch.style.backgroundColor = value;
      swatch.title = `Use ${value}`; swatch.setAttribute('aria-label', `Use ${value}`);
      swatch.addEventListener('click', () => { pixel.colour = value; setPixelTool(module, pixel.tool === 'blend' ? 'blend' : 'draw'); });
      swatches.append(swatch);
    }

    const menu = document.createElement('div'); menu.className = 'pixel-more-menu'; menu.id = menuId; menu.hidden = !pixel.menu;
    const moreAction = (action, label, handler) => { const item = button('more-' + action, label, label, handler, menu); item.dataset.pixelMore = action; delete item.dataset.pixelAction; return item; };
    moreAction('grid', 'Grid on/off', () => { pixel.grid = !pixel.grid; syncPixelToolbar(module); });
    moreAction('fill', 'Fill layer', () => { if (confirm('Fill the whole visible pixel layer with the current colour?')) fillPixelLayer(module, hexToRgba(pixel.colour)); });
    moreAction('clear', 'Clear layer', () => { if (confirm('Clear every pixel in this layer?')) fillPixelLayer(module, null); });
    moreAction('reset', 'Reset to last saved state', () => { if (confirm('Reset this pixel layer to its last saved state?')) resetPixelLayer(module); });
    const help = document.createElement('p'); help.className = 'pixel-shortcuts';
    help.textContent = 'Shortcuts: M Move · D Draw · E Erase · B Blend · I Pick · [ ] brush size · Ctrl/Cmd + Z Undo · Ctrl/Cmd + Shift + Z or Ctrl/Cmd + Y Redo · right-click erases';
    menu.append(help);
    root.append(toolbar, swatches, menu);
    return root;
  }

  function handlePixelPointerDown(module, canvas, event) {
    const layer = selectedPixelLayer(module);
    if (!layer) return false;
    const pixel = pixelState(module), erase = event.button === 2;
    if (pixel.tool === 'move' && !erase) return false;
    event.preventDefault();
    canvas.focus?.({preventScroll: true});
    if (event.button !== 0 && !erase) return true;
    const cell = pixelCellAt(canvas, event);
    pixel.hover = cell;
    if (pixel.tool === 'pick' && !erase) {
      pixel.colour = pickCompositeColour(module, ...cell);
      setPixelTool(module, 'draw');
      return true;
    }
    if (pixel.stroke) return true;
    if (!beginPixelStroke(module, layer, erase ? 'erase' : pixel.tool, cell)) return true;
    pixel.stroke.pointerId = event.pointerId; pixel.stroke.canvas = canvas;
    try { canvas.setPointerCapture(event.pointerId); } catch (_) { /* Synthetic pointers may have no capture target. */ }
    updatePixelCursor(module);
    return true;
  }

  function handlePixelPointerMove(module, canvas, event) {
    if (!selectedPixelLayer(module)) return false;
    const pixel = pixelState(module);
    if (!pixelPainting(module) && !pixel.stroke) return false;
    const cell = pixelCellAt(canvas, event);
    pixel.hover = cell;
    if (pixel.stroke && pixel.stroke.pointerId === event.pointerId) extendPixelStroke(module, cell);
    updatePixelCursor(module);
    return true;
  }

  function handlePixelPointerEnd(module, event, commit) {
    const stroke = editorState.get(module.id)?.pixel?.stroke;
    if (!stroke) return Boolean(selectedPixelLayer(module)) && pixelPainting(module);
    if (stroke.pointerId !== null && stroke.pointerId !== event.pointerId) return true;
    if (commit) finishPixelStroke(module); else cancelPixelStroke(module);
    return true;
  }

  globalThis.matrixSettlePixelEditors = () => {
    for (const module of customModules.filter(item => item.type === 'canvas')) finishPixelStroke(module);
  };

  globalThis.matrixCustomCanvasSaved = () => {
    for (const module of customModules.filter(item => item.type === 'canvas')) {
      const pixel = editorState.get(module.id)?.pixel;
      if (!pixel) continue;
      pixel.stroke = null; pixel.undo = []; pixel.redo = [];
      capturePixelBaselines(module, pixel);
      syncPixelToolbar(module);
    }
  };

  if (typeof window !== 'undefined' && typeof window.addEventListener === 'function') {
    window.addEventListener('blur', () => { for (const module of customModules.filter(item => item.type === 'canvas')) cancelPixelStroke(module); });
  }

  if (typeof document !== 'undefined' && typeof document.addEventListener === 'function') document.addEventListener('keydown', event => {
    if (event.defaultPrevented || event.altKey) return;
    const card = event.target?.closest?.('.advanced-card');
    if (!card?.querySelector('.pixel-toolbar')) return;
    if (event.target.matches?.('input,textarea,select,[contenteditable="true"]')) return;
    const module = byId(card.dataset.id);
    if (!module || !selectedPixelLayer(module)) return;
    const key = event.key.toLowerCase(), modifier = event.ctrlKey || event.metaKey;
    let handled = true;
    if (modifier && key === 'z') stepPixelHistory(module, event.shiftKey ? 1 : -1);
    else if (modifier && key === 'y') stepPixelHistory(module, 1);
    else if (modifier || event.shiftKey && !['[', ']', '{', '}'].includes(key)) handled = false;
    else if (PIXEL_SHORTCUTS[key]) setPixelTool(module, PIXEL_SHORTCUTS[key]);
    else if (key === '[' || key === '{') setPixelBrushSize(module, pixelState(module).size - 1);
    else if (key === ']' || key === '}') setPixelBrushSize(module, pixelState(module).size + 1);
    else handled = false;
    if (handled) event.preventDefault();
  });

  globalThis.matrixPixelEditing = Object.freeze({
    lineCells: pixelLineCells,
    brushOffsets: pixelBrushOffsets,
    paintCells: paintPixelCells,
    displayToLocal: pixelDisplayToLocal,
    emptyBitmap: emptyPixelBitmap,
    animatableProperties: animatablePixelProperties,
  });

  function renderProperties(module, container) {
    container.replaceChildren(); const state = stateFor(module), layer = module.layers.find(item => item.id === state.selected); if (!layer) return;
    const values = resolvedProperties(module, layer, state.frame); const title = document.createElement('h4'); title.textContent = layer.name + ' · frame ' + (state.frame + 1); container.append(title);
    if (layer.type === 'pixels') { const note = document.createElement('p'); note.className = 'pixel-sidebar-note'; note.textContent = 'Draw directly on the canvas with the pixel toolbar. Choose Move to drag the whole layer.'; container.append(note); }
    const grid = document.createElement('div'); grid.className = 'property-grid';
    const name = moduleControl('Layer name', layer.name, 'text', value => { layer.name = value; refreshAdvanced(module.id, true); }); name.className = 'wide'; grid.append(name);
    const editableFields = [['present', 'Present at this frame', 'checkbox'], ['z_index', 'Depth at this frame', 'number'], ...fieldDefinitions[layer.type]];
    if (!Object.hasOwn(values, 'present')) values.present = true;
    if (!Object.hasOwn(values, 'z_index')) values.z_index = module.layers.indexOf(layer);
    if (supportsRotation(layer.type) && !Object.hasOwn(values, 'rotation')) values.rotation = 0;
    for (const [field, labelText, type] of editableFields) {
      let label;
      if (type === 'playback-speed') label = globalThis.playbackSpeedControl(labelText, values[field], value => { saveFrameProperties(module, layer, {[field]: value}); drawEditor(module, container.closest('.advanced-card').querySelector('canvas')); }, control => control.dataset.property = field);
      else if (type === 'select' || type === 'media-select' || type === 'time-format') label = moduleControl(labelText, values[field], 'select', value => { saveFrameProperties(module, layer, {[field]: value}); if (type === 'media-select') { pruneMediaFailureStates(); const adopted = adoptMediaSource(module, layer.id, value, state.frame); if (adopted.changed) { Object.assign(module, {frame_count: adopted.module.frame_count, fps: adopted.module.fps, playback: adopted.module.playback}); setMessage(layer.name + ' now uses ' + value + '; ' + adopted.message); refreshAdvanced(module.id, true); return; } } drawEditor(module, container.closest('.advanced-card').querySelector('canvas')); }, type === 'media-select' ? mediaItems.map(item => item.name) : (type === 'time-format' ? ['24h', '24h_seconds', '12h', '12h_seconds'] : (field === 'font' ? fonts : [])));
      else if (type === 'points') { label = document.createElement('label'); label.textContent = labelText; label.className = 'wide'; const textarea = document.createElement('textarea'); textarea.rows = 4; textarea.dataset.property = field; textarea.value = formatPoints(values[field]); textarea.addEventListener('change', () => { const points = parsePoints(textarea.value); if (points.length) { saveFrameProperties(module, layer, {points}); refreshAdvanced(module.id, true); } }); label.append(textarea); }
      else label = moduleControl(labelText, values[field], type, value => { const nextValue = field === 'z_index' ? Math.max(-64, Math.min(64, Math.round(value))) : value; saveFrameProperties(module, layer, {[field]: nextValue}); drawEditor(module, container.closest('.advanced-card').querySelector('canvas')); }, null);
      for (const control of label.querySelectorAll('input,select,textarea')) control.dataset.property = field; grid.append(label);
    }
    container.append(grid);
    const tools = document.createElement('div'); tools.className = 'advanced-section'; const toolsTitle = document.createElement('h4'); toolsTitle.textContent = 'Keyframes and frame override';
    const easing = moduleControl('Easing into this keyframe', state.easing, 'select', value => { state.easing = value; }, easings);
    const row = document.createElement('div'); row.className = 'row'; row.append(makeButton('Add/update keyframe', () => addCanvasKeyframe(module.id), 'btn primary'), makeButton('Show from this frame', () => setLayerPresence(module.id, true)), makeButton('Hide from this frame', () => setLayerPresence(module.id, false), 'btn danger'), makeButton('Save frame override', () => { saveFrameProperties(module, layer, animatablePixelProperties(layer, resolvedProperties(module, layer, state.frame))); refreshAdvanced(module.id, true); }), makeButton('Clear frame override', () => { module.overrides = module.overrides.filter(item => item.frame !== state.frame || item.layer_id !== layer.id); refreshAdvanced(module.id, true); }, 'btn danger'));
    const list = document.createElement('div'); list.className = 'keyframe-list';
    for (const keyframe of layer.keyframes) { const item = document.createElement('div'); item.className = 'keyframe-row'; const text = document.createElement('span'); text.textContent = 'Frame ' + (keyframe.frame + 1) + ' · ' + keyframe.easing.replaceAll('_', ' '); item.append(text, makeButton('Remove', () => { layer.keyframes = layer.keyframes.filter(candidate => candidate !== keyframe); refreshAdvanced(module.id, true); }, 'btn danger')); list.append(item); }
    tools.append(toolsTitle, easing, row, list); container.append(tools);
  }

  function syncPropertyInputs(module) {
    const card = document.querySelector(`.custom-module-card[data-id="${CSS.escape(module.id)}"]`), state = stateFor(module), layer = module.layers.find(item => item.id === state.selected); if (!card || !layer) return;
    const values = resolvedProperties(module, layer, state.frame);
    for (const input of card.querySelectorAll('[data-property]')) { const field = input.dataset.property; if (!Object.hasOwn(values, field)) continue; if (input.type === 'checkbox') input.checked = values[field]; else input.value = field === 'points' ? formatPoints(values[field]) : (typeof values[field] === 'number' ? Math.round(values[field] * 100) / 100 : values[field]); }
  }

  function addSimpleImage(id, input) {
    const image = mediaItems.find(item => /\.(png|jpe?g|webp)$/i.test(item.name));
    if (image) { addAdvancedLayer(id, 'media', image.name); return; }
    input.accept = '.png,.jpg,.jpeg,.webp,image/png,image/jpeg,image/webp';
    input.click();
  }

  function moveLayerStack(module, layerId, direction) {
    const index = module.layers.findIndex(layer => layer.id === layerId);
    const target = Math.max(0, Math.min(module.layers.length - 1, index + direction));
    if (index < 0 || target === index) return;
    const [layer] = module.layers.splice(index, 1);
    module.layers.splice(target, 0, layer);
    refreshAdvanced(module.id, true);
  }

  function duplicateLayer(module, layerId) {
    if (module.layers.length >= 64) { setMessage('Cannot duplicate layer: this canvas already has 64 layers.', true); return false; }
    const index = module.layers.findIndex(layer => layer.id === layerId);
    if (index < 0) { setMessage('Cannot duplicate layer: the source layer is no longer available.', true); return false; }
    const source = module.layers[index];
    if (module.layers.reduce((total, layer) => total + layer.keyframes.length, 0) + source.keyframes.length > 512) {
      setMessage('Cannot duplicate layer: this canvas would exceed 512 keyframes.', true); return false;
    }
    const sourceOverrides = module.overrides.filter(item => item.layer_id === source.id);
    if (module.overrides.length + sourceOverrides.length > 512) {
      setMessage('Cannot duplicate layer: this canvas would exceed 512 frame overrides.', true); return false;
    }
    const pointsIn = properties => properties?.points?.length || 0;
    const layerPoints = layer => pointsIn(layer.properties) + layer.keyframes.reduce((total, keyframe) => total + pointsIn(keyframe.properties), 0);
    const pointTotal = module.layers.reduce((total, layer) => total + layerPoints(layer), 0)
      + module.overrides.reduce((total, override) => total + pointsIn(override.properties), 0);
    const copiedPoints = layerPoints(source) + sourceOverrides.reduce((total, override) => total + pointsIn(override.properties), 0);
    if (pointTotal + copiedPoints > 8192) {
      setMessage('Cannot duplicate layer: this canvas would exceed 8192 points.', true); return false;
    }
    const copy = clone(source);
    const loadedIds = new Set(module.layers.map(layer => layer.id));
    const timestamp = Date.now().toString(36);
    let candidateSerial = layerSerial;
    do copy.id = 'layer_' + timestamp + (candidateSerial++).toString(36);
    while (loadedIds.has(copy.id));
    copy.name = source.name.slice(0, 35) + ' copy';
    const copiedOverrides = sourceOverrides.map(override => ({...clone(override), layer_id: copy.id}));
    const candidate = clone(module);
    candidate.layers.splice(index + 1, 0, clone(copy)); candidate.overrides.push(...clone(copiedOverrides));
    if (pythonJsonByteLength(candidate) > 1048576) {
      setMessage('Cannot duplicate layer: this canvas would exceed the 1 MiB module limit.', true); return false;
    }
    layerSerial = candidateSerial;
    module.layers.splice(index + 1, 0, copy);
    module.overrides.push(...copiedOverrides);
    stateFor(module).selected = copy.id; refreshAdvanced(module.id, true); return true;
  }

  function renderLayers(module, container) {
    container.replaceChildren(); const state = stateFor(module);
    const heading = document.createElement('h4'); heading.textContent = 'Default layer stack · top is front'; container.append(heading);
    const toolbar = document.createElement('div'); toolbar.className = 'layer-toolbar';
    const select = document.createElement('select'); for (const type of layerTypes) { const option = document.createElement('option'); option.value = type; option.textContent = layerLabels[type] || type; select.append(option); }
    const upload = document.createElement('input'); upload.type = 'file'; upload.accept = '.png,.jpg,.jpeg,.webp,.gif,.mp4,image/*,video/mp4'; upload.hidden = true;
    toolbar.append(select, makeButton('Add layer', () => addAdvancedLayer(module.id, select.value), 'btn primary'), makeButton('Add time', () => addAdvancedLayer(module.id, 'time')), makeButton('Add image', () => addSimpleImage(module.id, upload)), makeButton('Upload image, GIF or MP4', () => { upload.accept = '.png,.jpg,.jpeg,.webp,.gif,.mp4,image/*,video/mp4'; upload.click(); }), upload); upload.addEventListener('change', () => uploadAdvancedMedia(module.id, upload)); container.append(toolbar);
    const list = document.createElement('div'); list.className = 'layer-list';
    for (let index = module.layers.length - 1; index >= 0; index--) {
      const layer = module.layers[index], properties = resolvedProperties(module, layer, state.frame);
      const depth = Object.hasOwn(properties, 'z_index') ? properties.z_index : index;
      const row = document.createElement('div'); row.className = 'layer-row' + (layer.id === state.selected ? ' active' : '');
      const visible = document.createElement('input'); visible.type = 'checkbox'; visible.checked = layer.visible; visible.title = 'Visible'; visible.setAttribute('aria-label', 'Show ' + layer.name); visible.onchange = () => { layer.visible = visible.checked; refreshAdvanced(module.id, true); };
      const name = document.createElement('button'); name.className = 'btn layer-select'; name.setAttribute('aria-label', 'Select ' + layer.name + ', ' + layer.type + ', depth ' + depth); name.onclick = () => { state.selected = layer.id; refreshAdvanced(module.id); };
      const layerName = document.createElement('span'); layerName.className = 'layer-name'; layerName.textContent = layer.name;
      const layerMeta = document.createElement('span'); layerMeta.className = 'layer-meta'; layerMeta.textContent = 'depth ' + depth + ' · ' + layer.type; name.append(layerName, layerMeta);
      const forward = makeButton('Bring forward', () => moveLayerStack(module, layer.id, 1), 'btn icon layer-stack-action layer-forward'); forward.textContent = '↑'; forward.title = 'Bring forward'; forward.setAttribute('aria-label', 'Bring forward'); forward.disabled = index === module.layers.length - 1;
      const backward = makeButton('Send backward', () => moveLayerStack(module, layer.id, -1), 'btn icon layer-stack-action layer-backward'); backward.textContent = '↓'; backward.title = 'Send backward'; backward.setAttribute('aria-label', 'Send backward'); backward.disabled = index === 0;
      const remove = makeButton('×', () => { if (module.layers.length === 1) { setMessage('An advanced canvas needs at least one layer.', true); return; } module.layers.splice(index, 1); module.overrides = module.overrides.filter(item => item.layer_id !== layer.id); state.selected = module.layers[Math.max(0, index - 1)].id; refreshAdvanced(module.id, true); }, 'btn danger layer-remove'); remove.title = 'Remove ' + layer.name; remove.setAttribute('aria-label', 'Remove ' + layer.name);
      const duplicate = makeButton('⧉', () => duplicateLayer(module, layer.id), 'btn icon layer-duplicate'); duplicate.title = 'Duplicate ' + layer.name; duplicate.setAttribute('aria-label', 'Duplicate ' + layer.name);
      row.append(visible, name, forward, backward, duplicate, remove); list.append(row);
    }
    container.append(list);
  }

  function moveTimelineItem(module, layerId, sourceFrame, targetFrame) {
    const invalidFrame = frame => !Number.isInteger(frame) || frame < 0 || frame >= module.frame_count;
    if (invalidFrame(sourceFrame) || invalidFrame(targetFrame)) return {moved: false, module, reason: 'out_of_bounds', frame: sourceFrame};
    if (sourceFrame === targetFrame) return {moved: false, module, reason: 'same_frame', frame: sourceFrame};
    const layer = module.layers.find(item => item.id === layerId);
    if (!layer) return {moved: false, module, reason: 'source_missing', frame: sourceFrame};
    const sourceKeyframe = layer.keyframes.find(item => item.frame === sourceFrame);
    const sourceOverride = module.overrides.find(item => item.layer_id === layerId && item.frame === sourceFrame);
    if (!sourceKeyframe && !sourceOverride) return {moved: false, module, reason: 'source_missing', frame: sourceFrame};
    const targetOccupied = layer.keyframes.some(item => item.frame === targetFrame)
      || module.overrides.some(item => item.layer_id === layerId && item.frame === targetFrame);
    if (targetOccupied) return {moved: false, module, reason: 'target_occupied', frame: sourceFrame};
    const next = JSON.parse(JSON.stringify(module));
    const nextLayer = next.layers.find(item => item.id === layerId);
    const nextKeyframe = nextLayer.keyframes.find(item => item.frame === sourceFrame);
    const nextOverride = next.overrides.find(item => item.layer_id === layerId && item.frame === sourceFrame);
    if (nextKeyframe) nextKeyframe.frame = targetFrame;
    if (nextOverride) nextOverride.frame = targetFrame;
    nextLayer.keyframes.sort((left, right) => left.frame - right.frame);
    return {moved: true, module: next, reason: null, frame: targetFrame};
  }
  window.moveTimelineItem = moveTimelineItem;

  function timelineMoveReason(reason) {
    return {
      out_of_bounds: 'timeline edge', same_frame: 'same frame',
      source_missing: 'no entry at this frame', target_occupied: 'target occupied',
    }[reason] || 'unavailable';
  }

  function restoreTimelineActionFocus(id, action, direction) {
    requestAnimationFrame(() => {
      const card = document.querySelector(`[data-id="${id}"]`);
      const preferred = card?.querySelector(`.timeline-context button[data-action="${action}"][data-direction="${direction}"]`);
      const reverse = card?.querySelector(`.timeline-context button[data-action="${action}"][data-direction="${-direction}"]`);
      const target = preferred && !preferred.disabled ? preferred : (reverse && !reverse.disabled ? reverse : card?.querySelector('.timeline-context'));
      target?.focus({preventScroll: true});
    });
  }

  function applyCanvasTimelineMove(id, direction) {
    const module = byId(id), state = module && stateFor(module); if (!module || !state) return false;
    const result = moveTimelineItem(module, state.selected, state.frame, state.frame + direction);
    if (!result.moved) return false;
    const moduleIndex = customModules.findIndex(item => item.id === id);
    if (moduleIndex < 0) return false;
    customModules[moduleIndex] = result.module;
    state.frame = result.frame;
    syncCustomModuleUI(); refreshAdvanced(id, true);
    restoreTimelineActionFocus(id, 'move', direction);
    return true;
  }

  function applyCanvasTimelineSeek(id, direction) {
    const module = byId(id), state = module && stateFor(module); if (!module || !state) return false;
    const target = state.frame + direction; if (target < 0 || target >= module.frame_count) return false;
    state.frame = target; refreshAdvanced(id); restoreTimelineActionFocus(id, 'seek', direction); return true;
  }

  function wireTimelineAction(button, action) {
    let touchStartedAt = 0, touchStartX = 0, touchStartY = 0, touchMoved = false, multiTouch = false, ignoreClickUntil = 0;
    button.addEventListener('touchstart', event => {
      if (event.touches.length === 1) {
        const touch = event.touches[0];
        touchStartedAt = event.timeStamp; touchStartX = touch.clientX; touchStartY = touch.clientY; touchMoved = false; multiTouch = false;
      } else multiTouch = true;
    }, {passive: true});
    button.addEventListener('touchmove', event => {
      if (event.touches.length !== 1) multiTouch = true;
      else if (Math.hypot(event.touches[0].clientX - touchStartX, event.touches[0].clientY - touchStartY) >= 8) touchMoved = true;
    }, {passive: true});
    button.addEventListener('touchend', event => {
      event.preventDefault(); ignoreClickUntil = performance.now() + 500;
      if (event.touches.length !== 0) { multiTouch = true; return; }
      const shortSingleTap = !multiTouch && !touchMoved && touchStartedAt > 0
        && event.changedTouches.length === 1 && event.timeStamp - touchStartedAt < 500;
      touchStartedAt = 0; touchMoved = false; multiTouch = false;
      if (shortSingleTap && !button.disabled) action();
    });
    button.addEventListener('touchcancel', () => {
      touchStartedAt = 0; touchMoved = false; multiTouch = false; ignoreClickUntil = performance.now() + 500;
    });
    button.onclick = event => {
      if (event.detail === 0 || (performance.now() >= ignoreClickUntil && !event.sourceCapabilities?.firesTouchEvents)) action();
    };
  }

  function cancelTimelineDrag(state) {
    const drag = state.timelineDrag;
    if (!drag) return;
    try {
      if (drag.cell?.hasPointerCapture?.(drag.pointerId)) drag.cell.releasePointerCapture(drag.pointerId);
    } catch (_) {}
    drag.cell?.classList.remove('dragging');
    if (drag.blurHandler) window.removeEventListener('blur', drag.blurHandler);
    state.timelineDrag = null;
  }

  function restoreTimelineCellFocus(id, layerId, frame) {
    requestAnimationFrame(() => {
      const card = document.querySelector(`[data-id="${id}"]`);
      card?.querySelector(`.layer-frame-cell[data-layer-id="${layerId}"][data-frame="${frame}"]`)?.focus({preventScroll: true});
    });
  }

  function wireTimelineMarker(cell, module, state) {
    const layerId = cell.dataset.layerId, sourceFrame = Number(cell.dataset.frame);
    const finish = (event, cancelled = false) => {
      const drag = state.timelineDrag;
      if (!drag || drag.pointerId !== event.pointerId) return;
      const moved = drag.moved, targetFrame = drag.targetFrame;
      cancelTimelineDrag(state);
      if (cancelled || !moved) return;
      event.preventDefault(); event.stopPropagation();
      state.timelineSuppressClickUntil = performance.now() + 500;
      const result = moveTimelineItem(module, layerId, sourceFrame, targetFrame);
      if (!result.moved) {
        const status = cell.closest('.timeline')?.querySelector('.timeline-context-status');
        if (status) status.textContent = `Cannot move item: ${timelineMoveReason(result.reason)}`;
        return;
      }
      const moduleIndex = customModules.findIndex(item => item.id === module.id);
      if (moduleIndex < 0) return;
      customModules[moduleIndex] = result.module;
      state.selected = layerId; state.frame = result.frame;
      syncCustomModuleUI(); refreshAdvanced(module.id, true);
      restoreTimelineCellFocus(module.id, layerId, result.frame);
    };
    cell.addEventListener('pointerdown', event => {
      if (!event.isPrimary || event.button !== 0) { cancelTimelineDrag(state); return; }
      cancelTimelineDrag(state);
      const blurHandler = () => cancelTimelineDrag(state);
      state.timelineDrag = {
        pointerId: event.pointerId, cell, layerId, sourceFrame,
        startX: event.clientX, startY: event.clientY,
        targetFrame: sourceFrame, moved: false, blurHandler,
      };
      cell.setPointerCapture(event.pointerId); cell.classList.add('dragging');
      window.addEventListener('blur', blurHandler, {once: true});
    });
    cell.addEventListener('pointermove', event => {
      const drag = state.timelineDrag;
      if (!drag || drag.pointerId !== event.pointerId) return;
      if (Math.hypot(event.clientX - drag.startX, event.clientY - drag.startY) < 8) return;
      drag.moved = true;
      const target = document.elementFromPoint(event.clientX, event.clientY)?.closest('.layer-frame-cell');
      const sameTimeline = target?.closest('.custom-module-card')?.dataset.id === module.id;
      if (sameTimeline && target.dataset.layerId === layerId) drag.targetFrame = Number(target.dataset.frame);
      event.preventDefault();
    });
    cell.addEventListener('pointerup', event => finish(event));
    cell.addEventListener('pointercancel', event => finish(event, true));
    cell.addEventListener('click', event => {
      if (performance.now() < (state.timelineSuppressClickUntil || 0)) {
        event.preventDefault(); event.stopImmediatePropagation();
      }
    }, true);
  }

  function renderTimelineContext(module, state) {
    const layer = module.layers.find(item => item.id === state.selected);
    const context = document.createElement('div'); context.className = 'timeline-context'; context.tabIndex = -1;
    const summary = document.createElement('strong'); summary.className = 'timeline-context-summary';
    summary.textContent = `${layer?.name || 'No layer'} · frame ${state.frame + 1}`;
    const actions = document.createElement('div'); actions.className = 'timeline-context-actions';
    const reasons = [];
    for (const [direction, label] of [[-1, 'Previous frame'], [1, 'Next frame']]) {
      const button = document.createElement('button'); button.type = 'button'; button.dataset.action = 'seek'; button.dataset.direction = direction;
      button.textContent = label; button.setAttribute('aria-label', label); button.disabled = state.frame + direction < 0 || state.frame + direction >= module.frame_count;
      wireTimelineAction(button, () => applyCanvasTimelineSeek(module.id, direction)); actions.append(button);
    }
    for (const [direction, label] of [[-1, 'Move item earlier'], [1, 'Move item later']]) {
      const availability = moveTimelineItem(module, state.selected, state.frame, state.frame + direction);
      const button = document.createElement('button'); button.type = 'button'; button.dataset.action = 'move'; button.dataset.direction = direction;
      button.textContent = label; button.setAttribute('aria-label', label); button.disabled = !availability.moved;
      button.title = availability.moved ? `Move to frame ${availability.frame + 1}` : `Cannot move: ${timelineMoveReason(availability.reason)}`;
      wireTimelineAction(button, () => applyCanvasTimelineMove(module.id, direction)); actions.append(button);
      if (!availability.moved) reasons.push(`${label}: ${timelineMoveReason(availability.reason)}`);
    }
    const status = document.createElement('span'); status.className = 'timeline-context-status'; status.setAttribute('aria-live', 'polite');
    status.textContent = reasons.length ? reasons.join(' · ') : 'Adjacent moves available';
    const group = (title, ...children) => {
      const section = document.createElement('section'); section.className = 'timeline-group';
      const heading = document.createElement('h5'); heading.className = 'timeline-group-title'; heading.textContent = title;
      section.setAttribute('aria-label', title); section.append(heading, ...children); return section;
    };
    const playhead = document.createElement('output'); playhead.className = 'timeline-playhead'; playhead.setAttribute('aria-live', 'polite');
    playhead.textContent = timelinePlayheadText(module, state.frame).message;
    const seekActions = document.createElement('div'); seekActions.className = 'timeline-seek-actions'; seekActions.append(...actions.querySelectorAll('[data-action="seek"]'));
    const frameActions = document.createElement('div'); frameActions.className = 'timeline-frame-actions';
    const frameAction = (label, shortcut, disabledReason, operation, confirmText) => {
      const button = document.createElement('button'); button.type = 'button'; button.className = 'btn'; button.dataset.frameAction = label;
      button.textContent = label; button.setAttribute('aria-keyshortcuts', shortcut); button.disabled = Boolean(disabledReason);
      button.title = disabledReason ? `${label} unavailable: ${disabledReason}` : `${label} (${shortcut.replace('Shift+', 'Shift + ')})`;
      wireTimelineAction(button, () => { if (!confirmText || confirm(confirmText)) applyTimelineFrameOperation(module.id, operation); });
      frameActions.append(button);
    };
    frameAction('Duplicate frame', 'D', module.frame_count >= 120 ? FRAME_ACTION_REASONS.frame_limit : '', insertTimelineFrame);
    frameAction('Delete frame', 'Delete', module.frame_count <= 1 ? FRAME_ACTION_REASONS.last_frame : '', deleteTimelineFrame, `Delete frame ${state.frame + 1}?`);
    frameAction('Move frame earlier', 'Shift+ArrowLeft', state.frame <= 0 ? 'already the first frame' : '', (item, frame) => moveTimelineFrame(item, frame, -1));
    frameAction('Move frame later', 'Shift+ArrowRight', state.frame >= module.frame_count - 1 ? 'already the last frame' : '', (item, frame) => moveTimelineFrame(item, frame, 1));
    const selectedLayer = module.layers.find(item => item.id === state.selected), generatePlan = mediaGeneratePlan(module, selectedLayer);
    const generate = document.createElement('button'); generate.type = 'button'; generate.className = 'btn primary'; generate.dataset.frameAction = 'Generate';
    generate.textContent = 'Generate'; generate.disabled = !generatePlan.ok;
    generate.title = generatePlan.ok ? `Generate frames from ${generatePlan.source}` : `Generate unavailable: ${GENERATE_REASONS[generatePlan.reason]}`;
    wireTimelineAction(generate, () => openGenerateDialog(module.id)); frameActions.prepend(generate);
    const keys = document.createElement('p'); keys.className = 'timeline-keys';
    keys.textContent = 'Keys on the timeline: ← → step · Home/End first/last · Shift+← → move frame · D duplicate · Delete remove';
    context.append(
      group('Playhead', playhead, seekActions),
      group('Frames', frameActions, keys),
      group('Selected layer on this frame', summary, actions, status),
    );
    return context;
  }

  function renderLayerTimeline(module, container) {
    const state = stateFor(module); cancelTimelineDrag(state); container.replaceChildren();
    disposeTimelineResize(state);
    state.timelineContainer = container;
    const title = document.createElement('div'); title.className = 'layer-timeline-title'; title.textContent = 'Layer timeline · top rows are in front';
    const legend = document.createElement('span'); legend.className = 'layer-timeline-legend'; legend.textContent = '◆ keyframe  ● frame edit  · present  ○ hidden'; title.append(legend);
    const context = renderTimelineContext(module, state);
    container.addEventListener('keydown', event => onTimelineKeydown(module, event));
    container.addEventListener('pointerdown', event => {
      const drag = state.timelineDrag;
      if (drag && event.pointerId !== drag.pointerId) cancelTimelineDrag(state);
    }, true);
    container.addEventListener('touchstart', event => {
      if (event.touches.length > 1) cancelTimelineDrag(state);
    }, {capture: true, passive: true});
    const scroll = document.createElement('div'); scroll.className = 'aseprite-timeline-scroll';
    const grid = document.createElement('div'); grid.className = 'aseprite-timeline'; grid.style.gridTemplateColumns = `var(--timeline-label-size) repeat(${module.frame_count},var(--timeline-frame-size))`;
    const corner = document.createElement('div'); corner.className = 'layer-track-corner'; corner.textContent = 'Frames'; grid.append(corner);
    for (let frame = 0; frame < module.frame_count; frame++) {
      const header = document.createElement('button'); header.type = 'button'; header.className = 'frame-chip' + (frame === state.frame ? ' active' : ''); header.dataset.frame = frame; header.textContent = frame + 1; header.title = 'Select frame ' + (frame + 1) + ' (' + (frame / module.fps).toFixed(2) + ' s)'; header.setAttribute('aria-label', 'Frame ' + (frame + 1) + (frame === state.frame ? ', current' : '')); if (frame === state.frame) header.setAttribute('aria-current', 'true'); header.setAttribute('aria-keyshortcuts', 'ArrowLeft ArrowRight Home End'); wireTimelineAction(header, () => { state.frame = frame; refreshAdvanced(module.id); }); grid.append(header);
    }
    const currentStack = [...orderedLayersForFrame(module, state.frame)].reverse();
    for (const {layer, properties: current, index, depth} of currentStack) {
      const row = document.createElement('div'); row.className = 'layer-track-row';
      const label = document.createElement('button'); label.type = 'button'; label.className = 'layer-track-label' + (layer.id === state.selected ? ' active' : ''); label.dataset.layerId = layer.id; label.textContent = layer.name + ' · z ' + depth; label.title = layer.type + ' — select layer'; wireTimelineAction(label, () => { state.selected = layer.id; refreshAdvanced(module.id); }); row.append(label);
      for (let frame = 0; frame < module.frame_count; frame++) {
        const keyframe = layer.keyframes.some(item => item.frame === frame);
        const override = module.overrides.some(item => item.frame === frame && item.layer_id === layer.id);
        const present = resolvedProperties(module, layer, frame).present !== false;
        const cell = document.createElement('button'); cell.type = 'button'; cell.className = 'layer-frame-cell';
        cell.dataset.layerId = layer.id; cell.dataset.frame = frame;
        cell.classList.toggle('active', layer.id === state.selected && frame === state.frame);
        cell.classList.toggle('selected-frame', frame === state.frame);
        cell.classList.toggle('has-keyframe', keyframe); cell.classList.toggle('has-override', override); cell.classList.toggle('absent', !present); cell.classList.toggle('held', present && !keyframe && !override);
        cell.textContent = keyframe ? '◆' : (override ? '●' : (!present ? '○' : '·'));
        cell.title = layer.name + ', frame ' + (frame + 1) + (keyframe ? ', keyframe' : (override ? ', frame override' : (!present ? ', hidden' : ', held')));
        cell.setAttribute('aria-label', cell.title);
        if (keyframe || override) wireTimelineMarker(cell, module, state);
        wireTimelineAction(cell, () => { if (performance.now() < (state.timelineSuppressClickUntil || 0)) return; state.selected = layer.id; state.frame = frame; refreshAdvanced(module.id); });
        row.append(cell);
      }
      grid.append(row);
    }
    scroll.append(grid); container.append(title, context, scroll);
    scroll.scrollLeft = state.timelineScrollLeft || 0;
    scroll.scrollTop = state.timelineScrollTop || 0;
    scroll.addEventListener('scroll', () => {
      state.timelineScrollLeft = scroll.scrollLeft;
      state.timelineScrollTop = scroll.scrollTop;
      state.timelineScrollRevision = (state.timelineScrollRevision || 0) + 1;
    }, {passive: true});
    {
      let resizeHandler = null;
      const keepActiveVisible = () => {
        if (!scroll.isConnected) {
          if (state.timelineResizeHandler === resizeHandler) disposeTimelineResize(state);
          return;
        }
        const active = scroll.querySelector('.layer-frame-cell.active');
        const label = scroll.querySelector('.layer-track-label.active');
        if (!active || !label) return;
        const activeRect = active.getBoundingClientRect();
        const scrollRect = scroll.getBoundingClientRect();
        const labelRect = label.getBoundingClientRect();
        const focused = document.activeElement;
        // Passive reflow retains the user's timeline position. Only the focused
        // active cell needs local visibility; unrelated editor focus is not a seek.
        if (focused === active) {
          if (activeRect.right > scrollRect.right) scroll.scrollLeft += activeRect.right - scrollRect.right + 4;
          else if (activeRect.left < labelRect.right) scroll.scrollLeft -= labelRect.right - activeRect.left + 4;
        }
        const card = container.closest('.advanced-card');
        if (focused instanceof HTMLElement && card?.contains(focused)) {
          const focusedRect = focused.getBoundingClientRect();
          const viewportTop = (window.visualViewport?.offsetTop || 0) + 16;
          const viewportBottom = (window.visualViewport?.offsetTop || 0) + (window.visualViewport?.height || innerHeight) - 16;
          if (focusedRect.top < viewportTop) window.scrollBy(0, focusedRect.top - viewportTop);
          else if (focusedRect.bottom > viewportBottom) window.scrollBy(0, focusedRect.bottom - viewportBottom);
        }
      };
      resizeHandler = () => {
        const generation = (state.timelineResizeGeneration || 0) + 1;
        const scrollRevision = state.timelineScrollRevision || 0;
        state.timelineResizeGeneration = generation;
        for (const frame of state.timelineResizeFrames || []) cancelAnimationFrame(frame);
        state.timelineResizeFrames?.clear();
        if (state.timelineResizeTimer) clearTimeout(state.timelineResizeTimer);
        state.timelineResizeTimer = null;
        let remaining = 4;
        const settle = () => {
          const frame = requestAnimationFrame(() => {
            state.timelineResizeFrames?.delete(frame);
            if (state.timelineResizeGeneration !== generation || state.timelineResizeHandler !== resizeHandler
                || (state.timelineScrollRevision || 0) !== scrollRevision) return;
          keepActiveVisible();
          if (--remaining > 0) settle();
          });
          (state.timelineResizeFrames ||= new Set()).add(frame);
        };
        settle();
        state.timelineResizeTimer = setTimeout(() => {
          if (state.timelineResizeGeneration !== generation || state.timelineResizeHandler !== resizeHandler
              || (state.timelineScrollRevision || 0) !== scrollRevision) return;
          state.timelineResizeTimer = null;
          keepActiveVisible();
        }, 100);
      };
      state.timelineResizeHandler = resizeHandler;
      window.addEventListener('resize', resizeHandler);
      window.visualViewport?.addEventListener('resize', resizeHandler);
    }
  }

  function renderTimeline(module, container) { renderLayerTimeline(module, container); }

  function updateTimelineSelection(module, container) {
    const state = stateFor(module);
    for (const header of container.querySelectorAll('.frame-chip')) {
      const current = Number(header.dataset.frame) === state.frame;
      header.classList.toggle('active', current);
      header.setAttribute('aria-label', 'Frame ' + (Number(header.dataset.frame) + 1) + (current ? ', current' : ''));
      if (current) header.setAttribute('aria-current', 'true'); else header.removeAttribute('aria-current');
    }
    const playhead = container.querySelector('.timeline-playhead'); if (playhead) playhead.textContent = timelinePlayheadText(module, state.frame).message;
    for (const cell of container.querySelectorAll('.layer-frame-cell')) {
      const selectedFrame = Number(cell.dataset.frame) === state.frame;
      cell.classList.toggle('selected-frame', selectedFrame);
      cell.classList.toggle('active', selectedFrame && cell.dataset.layerId === state.selected);
    }
  }

  function addCanvasKeyframe(id) {
    const module = byId(id), state = module && stateFor(module), layer = module?.layers.find(item => item.id === state.selected); if (!layer) return;
    const properties = animatablePixelProperties(layer, resolvedProperties(module, layer, state.frame)); let keyframe = layer.keyframes.find(item => item.frame === state.frame);
    if (keyframe) Object.assign(keyframe, {easing: state.easing, properties}); else layer.keyframes.push({frame: state.frame, easing: state.easing, properties});
    layer.keyframes.sort((a, b) => a.frame - b.frame); module.overrides = module.overrides.filter(item => item.frame !== state.frame || item.layer_id !== layer.id); refreshAdvanced(id, true);
  }
  window.addCanvasKeyframe = addCanvasKeyframe;

  function setLayerPresence(id, present) {
    const module = byId(id), state = module && stateFor(module), layer = module?.layers.find(item => item.id === state.selected); if (!layer) return;
    let keyframe = layer.keyframes.find(item => item.frame === state.frame);
    if (keyframe) { keyframe.easing = 'step'; keyframe.properties.present = present; }
    else layer.keyframes.push({frame: state.frame, easing: 'step', properties: {present}});
    layer.keyframes.sort((a, b) => a.frame - b.frame);
    for (const override of module.overrides) if (override.frame === state.frame && override.layer_id === layer.id) delete override.properties.present;
    module.overrides = module.overrides.filter(item => Object.keys(item.properties).length);
    refreshAdvanced(id, true);
  }

  function expandTimelineForAnimatedMedia(module, source) {
    const item = mediaItems.find(candidate => candidate.name === source);
    if (!item?.animated || !item.duration_ms) return null;
    const state = stateFor(module);
    const starterTimeline = module.frame_count === 1 && state.frame === 0 && module.overrides.length === 0
      && module.layers.every(layer => layer.keyframes.every(keyframe => keyframe.frame === 0));
    if (starterTimeline) {
      module.fps = item.fps;
      module.playback = 'loop';
    }
    const requestedFrames = Math.max(1, Math.round(item.duration_ms * module.fps / 1000));
    const mediaFrames = Math.max(1, Math.min(120 - state.frame, requestedFrames));
    module.frame_count = Math.min(120, Math.max(module.frame_count, state.frame + mediaFrames));
    return {item, mediaFrames, requestedFrames, startFrame: state.frame, clipped: mediaFrames < requestedFrames};
  }
  window.expandTimelineForAnimatedMedia = expandTimelineForAnimatedMedia;

  function initializeLayerAtCurrentFrame(module, layer) {
    const state = stateFor(module), initialProperties = animatablePixelProperties(layer, clone(layer.properties));
    if (state.frame > 0) layer.properties.present = false;
    layer.keyframes = [{frame: state.frame, easing: 'step', properties: clone(initialProperties)}];
  }

  function addAdvancedLayer(id, type, source) {
    const module = byId(id);
    if (!module || module.layers.length >= 64) return false;
    if (type === 'media' && !(source || mediaItems.length)) {
      setMessage('Upload an image, GIF or MP4 before adding a media layer.', true);
      return false;
    }
    const effectiveSource = type === 'media' ? (source || mediaItems[0]?.name) : source;
    const expansion = type === 'media' ? expandTimelineForAnimatedMedia(module, effectiveSource) : null;
    const layer = newLayer(type, null, effectiveSource);
    module.layers.push(layer);
    initializeLayerAtCurrentFrame(module, layer);
    stateFor(module).selected = layer.id;
    if (expansion) {
      setMessage(layer.name + ' added; ' + mediaImportMessage(expansion.item, expansion.mediaFrames, expansion.requestedFrames, module.fps, expansion.startFrame));
    } else {
      setMessage(layer.name + ' added with a complete keyframe at frame ' + (stateFor(module).frame + 1) + '.');
    }
    refreshAdvanced(id, true);
    return true;
  }
  window.addAdvancedLayer = addAdvancedLayer;

  async function uploadAdvancedMedia(id, input) {
    const file = input.files[0]; if (!file) return;
    const reconcile = item => {
      try { globalThis.reconcileMediaItem?.(item); return ''; }
      catch (error) { return ' Local reconciliation failed: ' + error.message; }
    };
    const adopt = item => {
      try {
        return addAdvancedLayer(id, 'media', item.name) === false
          ? ' Editor update failed: the media layer could not be added.' : '';
      }
      catch (error) { return ' Editor update failed: ' + error.message; }
    };
    if (file.size > 128 * 1024 * 1024) { setMessage('That file exceeds the 128 MB upload limit.', true); input.value = ''; return; }
    const form = new FormData(); form.append('file', file); setMessage('Uploading and optimizing ' + file.name + '…');
    try {
      let item;
      try { item = await api('/api/media', {method: 'POST', body: form}); }
      catch (error) {
        if (error.data?.committed && error.data?.item?.name) {
          let refreshWarning = '';
          try { await refreshMedia(); }
          catch (refreshError) {
            refreshWarning = ' The saved item could not be refreshed: ' + refreshError.message + reconcile(error.data.item);
          }
          const editorWarning = adopt(error.data.item);
          setMessage(error.message + refreshWarning + editorWarning, true);
        } else setMessage(error.message, true);
        return;
      }
      try { await refreshMedia(); }
      catch (refreshError) {
        const reconciliationWarning = reconcile(item);
        const editorWarning = adopt(item);
        setMessage(item.name + ' was saved, but the library refresh failed: ' + refreshError.message + reconciliationWarning + editorWarning, true);
        return;
      }
      const editorWarning = adopt(item);
      if (editorWarning) setMessage(item.name + ' was saved, but the editor could not be updated.' + editorWarning, true);
    } finally { input.value = ''; }
  }
  window.uploadAdvancedMedia = uploadAdvancedMedia;

  // Roadmap 1.8: whole-frame timeline operations. Each rebuilds the timeline from a new frame order
  // (order[newFrame] = oldFrame): keyframes keep their first position, and per-frame overrides pin any
  // frame whose interpolated picture would otherwise change, so saved data and the panel match exactly.
  const PLAYBACK_LABELS = {loop: 'Loop', ping_pong: 'Ping-pong', hold: 'Hold last frame'};

  function reorderTimelineFrames(module, order) {
    const desired = order.map(oldFrame => new Map(module.layers.map(layer => [layer.id, animatablePixelProperties(layer, resolvedProperties(module, layer, oldFrame))])));
    const firstPosition = new Map();
    order.forEach((oldFrame, newFrame) => { if (!firstPosition.has(oldFrame)) firstPosition.set(oldFrame, newFrame); });
    const next = clone(module);
    next.frame_count = order.length;
    next.overrides = [];
    for (const layer of next.layers) {
      layer.keyframes = layer.keyframes.filter(item => firstPosition.has(item.frame)).map(item => ({...item, frame: firstPosition.get(item.frame)})).sort((a, b) => a.frame - b.frame);
    }
    for (let frame = 0; frame < order.length; frame++) {
      for (const layer of next.layers) {
        const want = desired[frame].get(layer.id), have = animatablePixelProperties(layer, resolvedProperties(next, layer, frame)), properties = {};
        for (const [name, value] of Object.entries(want)) if (JSON.stringify(have[name]) !== JSON.stringify(value)) properties[name] = clone(value);
        if (Object.keys(properties).length) next.overrides.push({frame, layer_id: layer.id, properties});
      }
    }
    if (next.overrides.length > 512) return {changed: false, reason: 'override_limit', module, frame: null};
    return {changed: true, module: next};
  }

  function moveTimelineFrame(module, frame, direction) {
    const target = frame + direction;
    if (!Number.isInteger(frame) || !Number.isInteger(target) || frame < 0 || target < 0 || frame >= module.frame_count || target >= module.frame_count) return {changed: false, reason: 'out_of_bounds', module, frame};
    const order = [...Array(module.frame_count).keys()]; [order[frame], order[target]] = [order[target], order[frame]];
    const result = reorderTimelineFrames(module, order);
    return result.changed ? {...result, frame: target} : {...result, frame};
  }

  function deleteTimelineFrame(module, frame) {
    if (module.frame_count <= 1) return {changed: false, reason: 'last_frame', module, frame};
    if (!Number.isInteger(frame) || frame < 0 || frame >= module.frame_count) return {changed: false, reason: 'out_of_bounds', module, frame};
    const result = reorderTimelineFrames(module, [...Array(module.frame_count).keys()].filter(item => item !== frame));
    return result.changed ? {...result, frame: Math.min(frame, module.frame_count - 2)} : {...result, frame};
  }

  function insertTimelineFrame(module, frame) {
    if (module.frame_count >= 120) return {changed: false, reason: 'frame_limit', module, frame};
    if (!Number.isInteger(frame) || frame < 0 || frame >= module.frame_count) return {changed: false, reason: 'out_of_bounds', module, frame};
    const order = [...Array(module.frame_count).keys()]; order.splice(frame + 1, 0, frame);
    const result = reorderTimelineFrames(module, order);
    return result.changed ? {...result, frame: frame + 1} : {...result, frame};
  }

  function timelinePlayheadText(module, frame) {
    const seconds = value => (value / module.fps).toFixed(2);
    return {changed: false, module, frame, message: `Frame ${frame + 1} of ${module.frame_count} · ${seconds(frame)} s of ${seconds(module.frame_count)} s · ${module.fps} FPS · ${PLAYBACK_LABELS[module.playback] || module.playback}`};
  }

  function mediaLayerStartFrame(layer, frame) { return frame - mediaFrameIndex(layer, frame); }

  function mediaImportMessage(item, mediaFrames, requestedFrames, fps, startFrame) {
    let message = mediaFrames + ' timeline frames created automatically at ' + fps + ' FPS.';
    if (mediaFrames < requestedFrames) {
      const fitFps = Math.floor((120 - startFrame) * 1000 / item.duration_ms);
      message += ` The 120-frame limit was reached: only the first ${(mediaFrames / fps).toFixed(2)} s of ${(item.duration_ms / 1000).toFixed(2)} s plays.`
        + (fitFps >= 1 ? ` Set FPS to ${fitFps} FPS or lower to fit all of it.` : ' Shorten the clip to fit all of it.');
    }
    return message;
  }

  // Choosing a GIF/MP4 on an existing media layer builds its frames like adding a new one does.
  function adoptMediaSource(module, layerId, source, frame) {
    const item = mediaItems.find(candidate => candidate.name === source), layer = module.layers.find(candidate => candidate.id === layerId);
    if (!item?.animated || !item.duration_ms || !layer) return {changed: false, reason: 'not_animated', module, frame};
    const next = clone(module), startFrame = Math.max(0, Math.min(119, mediaLayerStartFrame(layer, frame)));
    // A single-frame canvas has no timing to preserve, so it adopts the media's own rate and loops it.
    const starterTimeline = next.frame_count === 1;
    if (starterTimeline) { next.fps = item.fps; next.playback = 'loop'; }
    const requestedFrames = Math.max(1, Math.round(item.duration_ms * next.fps / 1000));
    const mediaFrames = Math.max(1, Math.min(120 - startFrame, requestedFrames));
    const frameCount = Math.min(120, Math.max(next.frame_count, startFrame + mediaFrames));
    const changed = frameCount !== module.frame_count || next.fps !== module.fps || next.playback !== module.playback;
    next.frame_count = frameCount;
    return {changed, module: next, frame, message: mediaImportMessage(item, mediaFrames, requestedFrames, next.fps, startFrame)};
  }

  // Generate: rebuild a media layer's frames on demand.
  //   'replace'  — the timeline becomes the media: a GIF/MP4 sets the frame count and its own FPS
  //                and loops; a still image is shown on every existing frame.
  //   'in_place' — the media starts at the highlighted frame; earlier frames are untouched, the
  //                timeline grows if needed, and an animation is hidden again once it has played.
  function mediaGeneratePlan(module, layer) {
    const source = layer?.type === 'media' ? layer.properties.source : '';
    const item = source ? mediaItems.find(candidate => candidate.name === source) : null;
    if (!item) return {ok: false, reason: source ? 'missing_media' : 'not_media'};
    const speed = Math.max(0.1, Number(layer.properties.playback_speed_percent ?? 100) / 100);
    const animated = Boolean(item.animated && item.duration_ms);
    return {ok: true, item, source, animated, playedMs: animated ? item.duration_ms / speed : 0};
  }

  function generateMediaFrames(module, layerId, mode, frame) {
    const layer = module.layers.find(candidate => candidate.id === layerId), plan = mediaGeneratePlan(module, layer);
    if (!plan.ok) return {changed: false, reason: plan.reason, module, frame};
    if (!['replace', 'in_place'].includes(mode)) return {changed: false, reason: 'invalid_mode', module, frame};
    const start = mode === 'replace' ? 0 : frame;
    if (!Number.isInteger(start) || start < 0 || start >= module.frame_count) return {changed: false, reason: 'out_of_bounds', module, frame};
    const next = clone(module), target = next.layers.find(candidate => candidate.id === layerId);
    if (mode === 'replace' && plan.animated) { next.fps = plan.item.fps; next.playback = 'loop'; }
    const wanted = plan.animated ? Math.max(1, Math.round(plan.playedMs * next.fps / 1000)) : 0;
    const mediaFrames = plan.animated ? Math.min(120 - start, wanted) : 0;
    if (plan.animated) next.frame_count = mode === 'replace' ? mediaFrames : Math.min(120, Math.max(next.frame_count, start + mediaFrames));
    // The generated run owns this layer's visibility and source; other layers keep their edits in range.
    const strip = properties => { const rest = {...properties}; delete rest.present; delete rest.source; return rest; };
    for (const item of next.layers) item.keyframes = item.keyframes.filter(key => key.frame < next.frame_count);
    target.keyframes = target.keyframes.map(key => ({...key, properties: strip(key.properties)}))
      .filter(key => Object.keys(key.properties).length);
    next.overrides = next.overrides.filter(item => item.frame < next.frame_count)
      .map(item => item.layer_id === layerId ? {...item, properties: strip(item.properties)} : item)
      .filter(item => Object.keys(item.properties).length);
    target.properties.source = plan.source;
    target.properties.present = start === 0;
    const setPresence = (at, present) => {
      const existing = target.keyframes.find(key => key.frame === at);
      if (existing) { existing.properties.present = present; existing.easing = 'step'; }
      else target.keyframes.push({frame: at, easing: 'step', properties: {present}});
    };
    if (start > 0) setPresence(start, true);
    const end = start + mediaFrames;
    if (plan.animated && end < next.frame_count) setPresence(end, false);
    target.keyframes.sort((a, b) => a.frame - b.frame);
    const clipped = plan.animated && mediaFrames < wanted;
    let message;
    if (plan.animated) {
      message = `Generated ${mediaFrames} frame${mediaFrames === 1 ? '' : 's'} of ${plan.source} at ${next.fps} FPS`
        + (mode === 'replace' ? ', replacing the timeline.' : ` from frame ${start + 1} to ${end}.`);
      if (clipped) message += ` The 120-frame limit was reached: only ${(mediaFrames / next.fps).toFixed(2)} s of ${(plan.playedMs / 1000).toFixed(2)} s plays.`;
    } else {
      message = mode === 'replace' ? `${plan.source} now shows on all ${next.frame_count} frames.` : `${plan.source} now shows from frame ${start + 1} to the end.`;
    }
    return {changed: true, module: next, frame: start, mediaFrames, clipped, message};
  }

  globalThis.matrixTimelineFrames = Object.freeze({
    moveFrame: moveTimelineFrame, deleteFrame: deleteTimelineFrame, insertFrame: insertTimelineFrame,
    playheadText: timelinePlayheadText, adoptMediaSource, generateMedia: generateMediaFrames,
  });

  const GENERATE_REASONS = {not_media: 'select an image, GIF or MP4 layer first', missing_media: 'the selected media file is not in the library', out_of_bounds: 'no frame is highlighted', invalid_mode: 'unknown option'};

  function openGenerateDialog(id) {
    const module = byId(id), state = module && stateFor(module); if (!module || !state) return false;
    const layer = module.layers.find(item => item.id === state.selected), plan = mediaGeneratePlan(module, layer);
    if (!plan.ok) { setMessage('Generate unavailable: ' + GENERATE_REASONS[plan.reason] + '.', true); return false; }
    const dialog = document.createElement('dialog'); dialog.className = 'generate-dialog'; dialog.setAttribute('aria-labelledby', 'generate-dialog-title');
    const title = document.createElement('h3'); title.id = 'generate-dialog-title'; title.textContent = 'Generate frames';
    const text = document.createElement('p');
    text.textContent = plan.animated
      ? `${plan.source} plays for ${(plan.playedMs / 1000).toFixed(2)} s.`
        + ` Replace all frames makes the timeline exactly this media at ${plan.item.fps} FPS (other layers keep their edits that fit).`
        + ` Generate frames in place starts it at frame ${state.frame + 1} at ${module.fps} FPS and keeps earlier frames.`
      : `${plan.source} is a still image. Replace all frames shows it on every frame; generate frames in place shows it from frame ${state.frame + 1} onwards.`;
    const actions = document.createElement('div'); actions.className = 'generate-dialog-actions';
    const close = () => { dialog.close(); dialog.remove(); };
    const choose = mode => () => {
      close();
      const latest = byId(id), current = latest && stateFor(latest); if (!latest) return;
      const result = generateMediaFrames(latest, layer.id, mode, current.frame);
      if (!result.changed) { setMessage('Frames not generated: ' + (GENERATE_REASONS[result.reason] || result.reason) + '.', true); return; }
      applyTimelineFrameOperation(id, () => result);
      setMessage(result.message, result.clipped);
    };
    const replace = makeButton('Replace all frames', choose('replace'), 'btn primary'); replace.dataset.generate = 'replace';
    const inPlace = makeButton('Generate frames in place', choose('in_place'), 'btn'); inPlace.dataset.generate = 'in_place';
    const cancel = makeButton('Cancel', close, 'btn'); cancel.dataset.generate = 'cancel';
    dialog.addEventListener('cancel', event => { event.preventDefault(); close(); });
    actions.append(replace, inPlace, cancel); dialog.append(title, text, actions); document.body.append(dialog);
    dialog.showModal(); replace.focus();
    return true;
  }
  window.openGenerateDialog = openGenerateDialog;

  const FRAME_ACTION_REASONS = {out_of_bounds: 'no frame on that side', last_frame: 'an animation needs at least one frame', frame_limit: 'animations are limited to 120 frames', override_limit: 'too many per-frame edits to rebuild this order'};

  function applyTimelineFrameOperation(id, operation, focusFrame = true) {
    const module = byId(id), state = module && stateFor(module); if (!module || !state) return false;
    const result = operation(module, state.frame);
    if (!result.changed) { setMessage('Frame not changed: ' + (FRAME_ACTION_REASONS[result.reason] || result.reason) + '.', true); return false; }
    const index = customModules.findIndex(item => item.id === id); if (index < 0) return false;
    if (state.playing) { clearInterval(state.timer); state.playing = false; }
    customModules[index] = result.module; state.frame = result.frame;
    syncCustomModuleUI(); refreshAdvanced(id, true);
    if (focusFrame) focusTimelineFrame(id, result.frame);
    return true;
  }

  function focusTimelineFrame(id, frame) {
    requestAnimationFrame(() => requestAnimationFrame(() => document.querySelector(`.custom-module-card[data-id="${CSS.escape(id)}"] .frame-chip[data-frame="${frame}"]`)?.focus({preventScroll: true})));
  }

  function seekTimelineFrame(id, frame) {
    const module = byId(id), state = module && stateFor(module); if (!module || !state) return false;
    const target = Math.max(0, Math.min(module.frame_count - 1, frame)); if (target === state.frame) return false;
    state.frame = target; refreshAdvanced(id); focusTimelineFrame(id, target); return true;
  }

  function onTimelineKeydown(module, event) {
    if (event.defaultPrevented || event.altKey || event.ctrlKey || event.metaKey) return;
    if (event.target.matches?.('input,textarea,select,[contenteditable="true"]')) return;
    const state = stateFor(module), key = event.key;
    let handled = true;
    if (event.shiftKey && key === 'ArrowLeft') applyTimelineFrameOperation(module.id, (item, frame) => moveTimelineFrame(item, frame, -1));
    else if (event.shiftKey && key === 'ArrowRight') applyTimelineFrameOperation(module.id, (item, frame) => moveTimelineFrame(item, frame, 1));
    else if (event.shiftKey) handled = false;
    else if (key === 'ArrowLeft') seekTimelineFrame(module.id, state.frame - 1);
    else if (key === 'ArrowRight') seekTimelineFrame(module.id, state.frame + 1);
    else if (key === 'Home') seekTimelineFrame(module.id, 0);
    else if (key === 'End') seekTimelineFrame(module.id, module.frame_count - 1);
    else if (key.toLowerCase() === 'd') applyTimelineFrameOperation(module.id, insertTimelineFrame);
    else if (key === 'Delete') { if (confirm('Delete frame ' + (state.frame + 1) + '?')) applyTimelineFrameOperation(module.id, deleteTimelineFrame); }
    else handled = false;
    if (handled) { event.preventDefault(); event.stopPropagation(); }
  }

  function copyFrame(module) { const state = stateFor(module); state.clipboard = module.layers.map(layer => ({layer_id: layer.id, properties: animatablePixelProperties(layer, resolvedProperties(module, layer, state.frame))})); setMessage('Frame ' + (state.frame + 1) + ' copied.'); }
  function pasteFrame(module) { const state = stateFor(module); if (!state.clipboard) return; for (const snapshot of state.clipboard) { const layer = module.layers.find(item => item.id === snapshot.layer_id); if (layer) { module.overrides = module.overrides.filter(item => item.frame !== state.frame || item.layer_id !== layer.id); module.overrides.push({frame: state.frame, layer_id: layer.id, properties: clone(snapshot.properties)}); } } refreshAdvanced(module.id, true); }
  function duplicateFrame(module) { return applyTimelineFrameOperation(module.id, insertTimelineFrame); }


  window.advanceCanvasPlaybackFrame = function advanceCanvasPlaybackFrame(frame, frameCount, playback, direction = 1) {
    frameCount = Math.max(1, Math.round(frameCount));
    frame = Math.max(0, Math.min(frameCount - 1, Math.round(frame)));
    if (playback === 'hold' && frame === frameCount - 1) return {frame, direction, done: true};
    if (playback !== 'ping_pong') return {frame: (frame + 1) % frameCount, direction: 1, done: false};
    if (frameCount === 1) return {frame: 0, direction: 1, done: false};
    if (frame === frameCount - 1) direction = -1;
    else if (frame === 0) direction = 1;
    return {frame: frame + direction, direction, done: false};
  };

  function playBrowserPreview(module) {
    const state = stateFor(module); if (state.playing) { clearInterval(state.timer); state.playing = false; refreshAdvanced(module.id); return; }
    state.playing = true; let direction = 1; state.timer = setInterval(() => {
      const advanced = window.advanceCanvasPlaybackFrame(state.frame, module.frame_count, module.playback, direction);
      if (advanced.done) { clearInterval(state.timer); state.playing = false; refreshAdvanced(module.id); return; }
      state.frame = advanced.frame; direction = advanced.direction;
      const card = document.querySelector(`.custom-module-card[data-id="${CSS.escape(module.id)}"]`); if (card) { drawEditor(module, card.querySelector('canvas')); updateTimelineSelection(module, card.querySelector('.timeline')); }
    }, 1000 / module.fps); refreshAdvanced(module.id);
  }

  function captureTimelineViewState(card, state) {
    const scroll = card.querySelector('.aseprite-timeline-scroll');
    if (scroll) {
      state.timelineScrollLeft = scroll.scrollLeft;
      state.timelineScrollTop = scroll.scrollTop;
    }
    state.viewStateCaptured = true; state.pageScrollX = window.scrollX; state.pageScrollY = window.scrollY;
    const active = document.activeElement;
    state.editorFocus = null;
    if (!active || !card.contains(active)) return;
    const selection = typeof active.selectionStart === 'number'
      ? {start: active.selectionStart, end: active.selectionEnd, direction: active.selectionDirection} : null;
    if (active.matches('.timeline-context button[data-action]')) {
      state.editorFocus = {type: 'action', action: active.dataset.action, direction: active.dataset.direction};
    } else if (active.matches('.layer-frame-cell')) {
      state.editorFocus = {type: 'cell', layerId: active.dataset.layerId, frame: active.dataset.frame};
    } else if (active.matches('.frame-chip')) {
      state.editorFocus = {type: 'frame', frame: active.dataset.frame};
    } else if (active.matches('.layer-track-label[data-layer-id]')) {
      state.editorFocus = {type: 'layer-label', layerId: active.dataset.layerId};
    } else if (active.matches('[data-property]')) {
      state.editorFocus = {type: 'property', property: active.dataset.property, selection};
    } else if (active.matches('[data-control]')) {
      state.editorFocus = {type: 'control', control: active.dataset.control, selection};
    }
  }

  function restoreEditorViewState(card, state) {
    if (!state.viewStateCaptured) return;
    requestAnimationFrame(() => {
      if (!card.isConnected) return;
      const focus = state.editorFocus;
      let target = null;
      if (focus?.type === 'action') target = card.querySelector(`.timeline-context button[data-action="${CSS.escape(focus.action)}"][data-direction="${CSS.escape(focus.direction)}"]`);
      else if (focus?.type === 'cell') target = card.querySelector(`.layer-frame-cell[data-layer-id="${CSS.escape(focus.layerId)}"][data-frame="${CSS.escape(focus.frame)}"]`);
      else if (focus?.type === 'frame') target = card.querySelector(`.frame-chip[data-frame="${CSS.escape(focus.frame)}"]`);
      else if (focus?.type === 'layer-label') target = card.querySelector(`.layer-track-label[data-layer-id="${CSS.escape(focus.layerId)}"]`);
      else if (focus?.type === 'property') target = card.querySelector(`[data-property="${CSS.escape(focus.property)}"]`);
      else if (focus?.type === 'control') target = card.querySelector(`[data-control="${CSS.escape(focus.control)}"]`);
      if (target && !target.disabled) {
        target.focus({preventScroll: true});
        if (focus.selection && typeof target.setSelectionRange === 'function') {
          try { target.setSelectionRange(focus.selection.start, focus.selection.end, focus.selection.direction); } catch (_) {}
        }
      }
      const scroll = card.querySelector('.aseprite-timeline-scroll');
      if (scroll) { scroll.scrollLeft = state.timelineScrollLeft || 0; scroll.scrollTop = state.timelineScrollTop || 0; }
      window.scrollTo(state.pageScrollX || 0, state.pageScrollY || 0);
    });
  }

  function refreshAdvanced(id, draftChanged = false) {
    pruneMediaFailureStates(); const module = byId(id); if (!module) return;
    const card = document.querySelector(`.custom-module-card[data-id="${CSS.escape(id)}"]`);
    if (!card) { renderCustomModules(); if (draftChanged) publishCanvasDraftMutation(id); return; }
    captureTimelineViewState(card, stateFor(module)); renderAdvancedInto(module, card);
    if (draftChanged) publishCanvasDraftMutation(id);
  }

  function renderAdvancedInto(module, card) {
    const state = stateFor(module);
    if (state.pixel?.stroke && state.pixel.stroke.layerId !== state.selected) cancelPixelStroke(module); card.replaceChildren(); card.className = 'custom-module-card advanced-card'; card.dataset.id = module.id; card.dataset.editorType = 'canvas'; headerFor(module, card, true);
    const layout = document.createElement('div'); layout.className = 'advanced-layout'; const stage = document.createElement('div'); stage.className = 'canvas-stage'; const sidebar = document.createElement('div'); sidebar.className = 'advanced-sidebar';
    const nameRow = document.createElement('div'); nameRow.className = 'canvas-toolbar'; nameRow.append(moduleControl('Name', module.name, 'text', value => { module.name = value; syncCustomModuleUI(); }), moduleControl('Background', module.background, 'color', value => { module.background = value; drawEditor(module, canvas); }), moduleControl('Frames', module.frame_count, 'number', value => { module.frame_count = Math.max(1, Math.min(120, Math.round(value))); module.overrides = module.overrides.filter(item => item.frame < module.frame_count); for (const layer of module.layers) layer.keyframes = layer.keyframes.filter(item => item.frame < module.frame_count); refreshAdvanced(module.id); }), moduleControl('FPS', module.fps, 'number', value => { module.fps = Math.max(1, Math.min(60, value)); }), moduleControl('Playback', module.playback, 'select', value => { module.playback = value; }, ['loop', 'ping_pong', 'hold']), moduleControl('Rotation snap', state.rotationSnap || 'off', 'select', value => { state.rotationSnap = value === 'off' ? 0 : Number(value); }, ['off', '15', '30', '45', '90']));
    const templateRow = document.createElement('div'); templateRow.className = 'template-row'; const template = document.createElement('select'); [...canvasStarterExamples, {id: 'blank', label: 'Blank canvas'}].forEach(({id, label}) => { const option = document.createElement('option'); option.value = id; option.textContent = label; template.append(option); }); const templateLabel = document.createElement('label'); templateLabel.textContent = 'Starter example'; templateLabel.append(template); templateRow.append(templateLabel, makeButton('Load example', () => applyTemplate(module.id, template.value)));
    const canvasWrap = document.createElement('div'); canvasWrap.className = 'editor-canvas-wrap'; const canvas = document.createElement('canvas'); canvas.className = 'editor-canvas'; canvas.width = 640; canvas.height = 320; canvas.setAttribute('aria-label', 'Direct 64 by 32 custom module canvas editor'); canvas.tabIndex = 0; const cursor = document.createElement('div'); cursor.className = 'pixel-cursor'; cursor.hidden = true; cursor.setAttribute('aria-hidden', 'true'); canvasWrap.append(canvas, cursor);
    const pixelLayer = selectedPixelLayer(module), pixelToolbar = pixelLayer ? renderPixelToolbar(module) : null;
    const help = document.createElement('p'); help.className = 'canvas-help'; help.textContent = 'Drag a layer to move it. Drag the square handle to resize, the round handle to rotate, or paint directly with the pixel toolbar when a Pixels layer is selected. Higher depth values draw in front; keyframe Depth to change the stack on a chosen frame.';
    const timelineTools = document.createElement('div'); timelineTools.className = 'timeline-toolbar'; const onion = moduleControl('Onion skin', state.onion, 'checkbox', value => { state.onion = value; drawEditor(module, canvas); }); timelineTools.append(onion, makeButton(state.playing ? 'Stop browser preview' : 'Play browser preview', () => playBrowserPreview(module), 'btn primary'), makeButton('Copy frame', () => copyFrame(module)), makeButton('Paste frame', () => pasteFrame(module)));
    const timeline = document.createElement('div'); timeline.className = 'timeline';
    stage.append(nameRow, templateRow, ...(pixelToolbar ? [pixelToolbar] : []), canvasWrap, ...(pixelToolbar ? [Object.assign(document.createElement('p'), {className: 'pixel-status', textContent: ''})] : []), help, timelineTools, timeline); const layers = document.createElement('div'); const properties = document.createElement('div'); properties.className = 'advanced-section'; sidebar.append(layers, properties); layout.append(stage, sidebar); card.append(layout);
    renderTimeline(module, timeline); renderLayers(module, layers); renderProperties(module, properties); bindCanvasPointerTransforms(module, canvas); drawEditor(module, canvas); if (pixelToolbar) syncPixelToolbar(module); restoreEditorViewState(card, state);
  }

  function renderAdvanced(module, list) { const card = document.createElement('div'); list.append(card); renderAdvancedInto(module, card); }

  window.renderCustomModules = function renderCustomModules() {
    pruneMediaFailureStates();
    const list = document.querySelector('#custom-modules'); list.replaceChildren();
    if (!customModules.length) { const empty = document.createElement('div'); empty.className = 'empty'; empty.textContent = 'No custom modules yet. Add simple text, then convert it into a layered animation when needed.'; list.append(empty); syncCustomModuleUI(); syncPlaylistDurations(); return; }
    for (const module of customModules) module.type === 'canvas' ? renderAdvanced(module, list) : renderSimple(module, list);
    syncCustomModuleUI(); syncPlaylistDurations();
  };

  window.removeCustomModule = function removeCustomModuleAdvanced(id) {
    if (!confirm('Remove ' + customName(id) + ' from the custom modules and playlist?')) return;
    customModules = customModulesFromEditor().filter(item => item.id !== id);
    const previousState = editorState.get(id);
    if (previousState) disposeTimelineState(previousState);
    editorState.delete(id);
    for (const row of document.querySelectorAll('.module-row')) if (row.dataset.module === id) row.closest('.module-item').remove();
    globalThis.markMatrixDirty?.(); renderCustomModules(); globalThis.matrixCustomCanvasDraftRemoved?.(id); setMessage('Custom module removed. Save configuration to apply.');
  };

  mediaRedrawTimer = setInterval(() => { if (customModules.some(module => module.type === 'canvas' && module.layers.some(layer => layer.type === 'time' && layer.visible))) redrawMediaCanvases(); }, 1000);
  renderCustomModules();

  globalThis.matrixCustomCanvasReady = true;
})();
