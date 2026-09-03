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
    if (type === 'pixels') return {present: true, points: [[32, 16]], rotation: 0, colour: '#ffffff'};
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

  function spinningSquare(module) {
    const square = newLayer('rectangle', 'Spinning square');
    square.keyframes = [
      {frame: 0, easing: 'linear', properties: {rotation: 0}},
      {frame: 23, easing: 'ease_in_out', properties: {rotation: 360}},
    ];
    Object.assign(module, {background: '#000000', frame_count: 24, fps: 12, playback: 'loop', layers: [square], overrides: []});
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
    const yaw = angle * Math.PI / 180, tilt = 24 * Math.PI / 180;
    return cubeVertices.map(([x, y, z]) => {
      const rotatedX = x * Math.cos(yaw) + z * Math.sin(yaw);
      const rotatedZ = -x * Math.sin(yaw) + z * Math.cos(yaw);
      const tiltedY = y * Math.cos(tilt) - rotatedZ * Math.sin(tilt);
      const depth = y * Math.sin(tilt) + rotatedZ * Math.cos(tilt);
      const perspective = 3.2 / (4 + depth);
      return [Math.round((32 + rotatedX * 11 * perspective) * 100) / 100, Math.round((16 + tiltedY * 11 * perspective) * 100) / 100];
    });
  }

  function spinningRgbCube(module) {
    const stops = [[0, 0], [8, 90], [16, 180], [24, 270], [31, 360]];
    const layers = cubeEdges.map(([start, end, colour], index) => {
      const initial = projectedCubeVertices(0), layer = newLayer('line', 'Cube edge ' + (index + 1));
      Object.assign(layer.properties, {x1: initial[start][0], y1: initial[start][1], x2: initial[end][0], y2: initial[end][1], width: 1, colour});
      layer.keyframes = stops.map(([frame, angle]) => {
        const points = projectedCubeVertices(angle);
        return {frame, easing: 'linear', properties: {x1: points[start][0], y1: points[start][1], x2: points[end][0], y2: points[end][1], present: true}};
      });
      return layer;
    });
    Object.assign(module, {background: '#000000', frame_count: 32, fps: 16, playback: 'loop', layers, overrides: []});
  }

  function linesAndCircle(module) {
    const labels = ['LINE ONE', 'LINE TWO', 'LINE THREE'].map((text, index) => ({
      id: safeId('layer_'), name: 'Text ' + (index + 1), type: 'text', visible: true,
      properties: {x: 1, y: 7 + index * 9, text, font: '4x6', colour: ['#ffffff', '#00ffff', '#ff71ce'][index]}, keyframes: [],
    }));
    const circle = newLayer('circle', 'Circle');
    Object.assign(circle.properties, {x: 55, y: 16, radius: 7, colour: '#ffb300'});
    Object.assign(module, {background: '#000000', frame_count: 1, fps: 10, playback: 'hold', layers: [...labels, circle], overrides: []});
  }

  function matrixRain(module) {
    const offsets = [-28, -12, 4, 20, -20, -4, 12, -24, -8, 8, 21, -16];
    const glyphs = ['0', '1', '+', '*'];
    const colours = ['#aaffcc', '#00ff66', '#00bb44', '#005522'];
    const layers = [];
    for (let column = 0; column < offsets.length; column += 1) {
      for (let trail = 0; trail < 4; trail += 1) {
        const layer = newLayer('text', `Rain ${column + 1}.${trail + 1}`);
        const startY = offsets[column] - trail * 7;
        Object.assign(layer.properties, {
          x: 1 + column * 5, y: startY,
          text: glyphs[(column * 3 + trail) % glyphs.length],
          font: '4x6', colour: colours[trail],
        });
        layer.keyframes = [
          {frame: 0, easing: 'linear', properties: {y: startY}},
          {frame: 47, easing: 'linear', properties: {y: startY + 64}},
        ];
        layers.push(layer);
      }
    }
    Object.assign(module, {background: '#000300', frame_count: 48, fps: 20, playback: 'loop', layers, overrides: []});
  }

  function rainbowPulse(module) {
    const colours = ['#ff004c', '#ff7a00', '#ffe600', '#20e060', '#00d9ff', '#3973ff', '#a855f7'];
    const layers = colours.map((colour, index) => {
      const layer = newLayer('circle', `Rainbow ring ${index + 1}`);
      const radius = 3 + index * 3;
      Object.assign(layer.properties, {x: 32, y: 16, radius, colour, filled: false});
      layer.keyframes = [
        {frame: 0, easing: 'ease_in_out', properties: {radius, colour}},
        {frame: 17, easing: 'ease_in_out', properties: {radius: radius + 5, colour: colours[(index + 1) % colours.length]}},
        {frame: 34, easing: 'ease_in_out', properties: {radius, colour}},
      ];
      return layer;
    });
    Object.assign(module, {background: '#030008', frame_count: 35, fps: 18, playback: 'loop', layers, overrides: []});
  }

  const canvasStarterExamples = Object.freeze([
    Object.freeze({id: 'spinning_square', label: 'Spinning square'}),
    Object.freeze({id: 'spinning_rgb_cube', label: 'Spinning RGB cube'}),
    Object.freeze({id: 'three_lines_circle', label: 'Three lines and circle'}),
    Object.freeze({id: 'matrix_rain', label: 'Matrix rain'}),
    Object.freeze({id: 'rainbow_pulse', label: 'Rainbow pulse'}),
  ]);
  const canvasTemplateBuilders = Object.freeze({
    spinning_square: spinningSquare,
    spinning_rgb_cube: spinningRgbCube,
    three_lines_circle: linesAndCircle,
    matrix_rain: matrixRain,
    rainbow_pulse: rainbowPulse,
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
    if (type === 'media') return {...identity, type, source: mediaItems.find(item => ['gif', 'mp4'].includes(item.kind))?.name || '', fit: 'contain', x: 0, y: 0, width: 64, height: 32, loops: 1, playback_speed_percent: 100.0};
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
      replaceBasicSubtype(module, event.target.value);
      renderCustomModules();
      document.querySelector(`.custom-module-card[data-id="${CSS.escape(module.id)}"] [data-field="type"]`)?.focus();
    });
    if (module.type === 'media') {
      fields.append(name, mode,
        customControl('Media file', 'source', 'select', module.source, mediaItems.filter(item => ['gif', 'mp4'].includes(item.kind)).map(item => item.name)),
        customControl('Fit', 'fit', 'select', module.fit, ['contain', 'cover', 'stretch', 'manual']),
        customControl('X position', 'x', 'number', module.x), customControl('Y position', 'y', 'number', module.y),
        customControl('Width', 'width', 'number', module.width), customControl('Height', 'height', 'number', module.height),
        customControl('Loops', 'loops', 'number', module.loops ?? 1),
        customControl('Playback speed (%)', 'playback_speed_percent', 'playback_speed', module.playback_speed_percent ?? 100.0));
    } else {
      const text = customControl('Text', 'text', 'text', module.text); text.classList.add('wide');
      fields.append(name, text, mode, customControl('Font', 'font', 'select', module.font, fonts), customControl('Text colour', 'colour', 'color', module.colour), customControl('X position', 'x', 'number', module.x), customControl('Baseline Y', 'y', 'number', module.y), customControl('Scroll direction', 'direction', 'select', module.direction, ['left', 'right']), customControl('Frame delay (seconds)', 'speed', 'number', module.speed), customControl('Scroll loops', 'loops', 'number', module.loops ?? 1));
    }
    for (const control of fields.querySelectorAll('[data-field]')) control.addEventListener('input', syncPlaylistDurations);
    const runtimeValue = module.type === 'text_scroll' ? calculatedScrollDuration(module) : (module.type === 'media' ? calculatedMediaDuration(module) : Number.NaN);
    const runtime = scrollPhysicalTimeReadout(runtimeValue); runtime.hidden = !['text_scroll', 'media'].includes(module.type);
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
    context.save(); context.globalAlpha = alpha; context.strokeStyle = properties.colour; context.fillStyle = properties.colour; context.lineWidth = Math.max(.12, properties.width || .12);
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
    if (selected) {
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
    canvas.addEventListener('pointerdown', event => {
      const layer = module.layers.find(item => item.id === state.selected); if (!layer) return;
      canvas.setPointerCapture(event.pointerId); const current = point(event), properties = resolvedProperties(module, layer, state.frame), box = bounds(properties, layer.type), centre = {x: (box.minX + box.maxX) / 2, y: (box.minY + box.maxY) / 2};
      let mode = 'move';
      if (supportsRotation(layer.type) && Math.hypot(current.x - centre.x, current.y - (box.minY - 2.5)) < 2) { properties.rotation ??= 0; mode = 'rotate'; }
      else if (layer.type === 'pixels' && !properties.bitmap_rgba) { const points = clone(properties.points); points.push([Math.round(current.x), Math.round(current.y)]); saveFrameProperties(module, layer, {points}); mode = 'freehand'; }
      else if (Math.hypot(current.x - box.maxX, current.y - box.maxY) < 2) mode = 'resize';
      state.drag = {mode, start: current, original: clone(properties), centre}; drawEditor(module, canvas); syncPropertyInputs(module);
    });
    canvas.addEventListener('pointermove', event => {
      if (!state.drag) return; const layer = module.layers.find(item => item.id === state.selected); if (!layer) return;
      const current = point(event), drag = state.drag, properties = clone(drag.original), dx = current.x - drag.start.x, dy = current.y - drag.start.y;
      if (drag.mode === 'move') moveProperties(properties, layer.type, dx, dy);
      else if (drag.mode === 'freehand') { const live = resolvedProperties(module, layer, state.frame); const last = live.points.at(-1); if (!last || Math.round(current.x) !== last[0] || Math.round(current.y) !== last[1]) saveFrameProperties(module, layer, {points: [...live.points, [Math.round(current.x), Math.round(current.y)]]}); drawEditor(module, canvas); return; }
      else if (drag.mode === 'rotate' && Object.hasOwn(properties, 'rotation')) { const rawRotation = Math.atan2(current.y - drag.centre.y, current.x - drag.centre.x) * 180 / Math.PI + 90; properties.rotation = snapRotation(rawRotation, state.rotationSnap); }
      else if (drag.mode === 'resize') resizeProperties(properties, layer.type, current, dx, dy);
      const saved = layer.type === 'pixels' && properties.bitmap_rgba
        ? Object.fromEntries(['offset_x', 'offset_y', 'rotation', 'present', 'z_index'].filter(key => Object.hasOwn(properties, key)).map(key => [key, properties[key]]))
        : properties;
      saveFrameProperties(module, layer, saved); drawEditor(module, canvas); syncPropertyInputs(module);
    });
    const end = () => { if (state.drag) { state.drag = null; refreshAdvanced(module.id, true); } };
    canvas.addEventListener('pointerup', end); canvas.addEventListener('pointercancel', end);
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
    pixels: [['rotation', 'Rotation', 'number'], ['colour', 'Colour', 'color']],
    line: [['x1', 'Start X', 'number'], ['y1', 'Start Y', 'number'], ['x2', 'End X', 'number'], ['y2', 'End Y', 'number'], ['rotation', 'Rotation', 'number'], ['width', 'Width', 'number'], ['colour', 'Colour', 'color']],
    rectangle: [['x', 'X', 'number'], ['y', 'Y', 'number'], ['width', 'Width', 'number'], ['height', 'Height', 'number'], ['rotation', 'Rotation', 'number'], ['colour', 'Colour', 'color'], ['filled', 'Filled', 'checkbox']],
    polygon: [['points', 'Points (x,y per line)', 'points'], ['rotation', 'Rotation', 'number'], ['colour', 'Colour', 'color'], ['filled', 'Filled', 'checkbox']],
    circle: [['x', 'Centre X', 'number'], ['y', 'Centre Y', 'number'], ['radius', 'Radius', 'number'], ['colour', 'Colour', 'color'], ['filled', 'Filled', 'checkbox']],
    ellipse: [['x', 'Centre X', 'number'], ['y', 'Centre Y', 'number'], ['radius_x', 'Radius X', 'number'], ['radius_y', 'Radius Y', 'number'], ['rotation', 'Rotation', 'number'], ['colour', 'Colour', 'color'], ['filled', 'Filled', 'checkbox']],
    media: [['source', 'Media file', 'media-select'], ['playback_speed_percent', 'Playback speed (%)', 'playback-speed'], ['x', 'X', 'number'], ['y', 'Y', 'number'], ['width', 'Width', 'number'], ['height', 'Height', 'number'], ['rotation', 'Rotation', 'number'], ['keep_aspect', 'Keep aspect ratio', 'checkbox']],
  };

  function parsePoints(value) { return value.split(/\n|;/).map(line => line.trim()).filter(Boolean).map(line => line.split(',').map(Number)).filter(point => point.length === 2 && point.every(Number.isFinite)); }
  function formatPoints(points) { return points.map(point => point.map(value => Math.round(value * 100) / 100).join(',')).join('\n'); }

  function renderProperties(module, container) {
    container.replaceChildren(); const state = stateFor(module), layer = module.layers.find(item => item.id === state.selected); if (!layer) return;
    const values = resolvedProperties(module, layer, state.frame); const title = document.createElement('h4'); title.textContent = layer.name + ' · frame ' + (state.frame + 1); container.append(title);
    const grid = document.createElement('div'); grid.className = 'property-grid';
    const name = moduleControl('Layer name', layer.name, 'text', value => { layer.name = value; refreshAdvanced(module.id, true); }); name.className = 'wide'; grid.append(name);
    const editableFields = [['present', 'Present at this frame', 'checkbox'], ['z_index', 'Depth at this frame', 'number'], ...fieldDefinitions[layer.type]];
    if (!Object.hasOwn(values, 'present')) values.present = true;
    if (!Object.hasOwn(values, 'z_index')) values.z_index = module.layers.indexOf(layer);
    if (supportsRotation(layer.type) && !Object.hasOwn(values, 'rotation')) values.rotation = 0;
    for (const [field, labelText, type] of editableFields) {
      let label;
      if (type === 'playback-speed') label = globalThis.playbackSpeedControl(labelText, values[field], value => { saveFrameProperties(module, layer, {[field]: value}); drawEditor(module, container.closest('.advanced-card').querySelector('canvas')); }, control => control.dataset.property = field);
      else if (type === 'select' || type === 'media-select' || type === 'time-format') label = moduleControl(labelText, values[field], 'select', value => { saveFrameProperties(module, layer, {[field]: value}); if (type === 'media-select') pruneMediaFailureStates(); drawEditor(module, container.closest('.advanced-card').querySelector('canvas')); }, type === 'media-select' ? mediaItems.map(item => item.name) : (type === 'time-format' ? ['24h', '24h_seconds', '12h', '12h_seconds'] : (field === 'font' ? fonts : [])));
      else if (type === 'points') { label = document.createElement('label'); label.textContent = labelText; label.className = 'wide'; const textarea = document.createElement('textarea'); textarea.rows = 4; textarea.dataset.property = field; textarea.value = formatPoints(values[field]); textarea.addEventListener('change', () => { const points = parsePoints(textarea.value); if (points.length) { saveFrameProperties(module, layer, {points}); refreshAdvanced(module.id, true); } }); label.append(textarea); }
      else label = moduleControl(labelText, values[field], type, value => { const nextValue = field === 'z_index' ? Math.max(-64, Math.min(64, Math.round(value))) : value; saveFrameProperties(module, layer, {[field]: nextValue}); drawEditor(module, container.closest('.advanced-card').querySelector('canvas')); }, null);
      for (const control of label.querySelectorAll('input,select,textarea')) control.dataset.property = field; grid.append(label);
    }
    container.append(grid);
    const tools = document.createElement('div'); tools.className = 'advanced-section'; const toolsTitle = document.createElement('h4'); toolsTitle.textContent = 'Keyframes and frame override';
    const easing = moduleControl('Easing into this keyframe', state.easing, 'select', value => { state.easing = value; }, easings);
    const row = document.createElement('div'); row.className = 'row'; row.append(makeButton('Add/update keyframe', () => addCanvasKeyframe(module.id), 'btn primary'), makeButton('Show from this frame', () => setLayerPresence(module.id, true)), makeButton('Hide from this frame', () => setLayerPresence(module.id, false), 'btn danger'), makeButton('Save frame override', () => { saveFrameProperties(module, layer, resolvedProperties(module, layer, state.frame)); refreshAdvanced(module.id, true); }), makeButton('Clear frame override', () => { module.overrides = module.overrides.filter(item => item.frame !== state.frame || item.layer_id !== layer.id); refreshAdvanced(module.id, true); }, 'btn danger'));
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
      row.append(visible, name, forward, backward, remove); list.append(row);
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
    context.append(summary, actions, status); return context;
  }

  function renderLayerTimeline(module, container) {
    const state = stateFor(module); cancelTimelineDrag(state); container.replaceChildren();
    disposeTimelineResize(state);
    state.timelineContainer = container;
    const title = document.createElement('div'); title.className = 'layer-timeline-title'; title.textContent = 'Layer timeline · top rows are in front';
    const legend = document.createElement('span'); legend.className = 'layer-timeline-legend'; legend.textContent = '◆ keyframe  ● frame edit  · present  ○ hidden'; title.append(legend);
    const context = renderTimelineContext(module, state);
    container.addEventListener('pointerdown', event => {
      const drag = state.timelineDrag;
      if (drag && event.pointerId !== drag.pointerId) cancelTimelineDrag(state);
    }, true);
    container.addEventListener('touchstart', event => {
      if (event.touches.length > 1) cancelTimelineDrag(state);
    }, {capture: true, passive: true});
    const scroll = document.createElement('div'); scroll.className = 'aseprite-timeline-scroll';
    const grid = document.createElement('div'); grid.className = 'aseprite-timeline'; grid.style.gridTemplateColumns = `var(--timeline-label-size) repeat(${module.frame_count},var(--timeline-frame-size))`;
    const corner = document.createElement('div'); corner.className = 'layer-track-corner'; corner.textContent = 'Layer / frame'; grid.append(corner);
    for (let frame = 0; frame < module.frame_count; frame++) {
      const header = document.createElement('button'); header.type = 'button'; header.className = 'frame-chip' + (frame === state.frame ? ' active' : ''); header.dataset.frame = frame; header.textContent = frame + 1; header.title = 'Select frame ' + (frame + 1); wireTimelineAction(header, () => { state.frame = frame; refreshAdvanced(module.id); }); grid.append(header);
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
        if (activeRect.right > scrollRect.right) scroll.scrollLeft += activeRect.right - scrollRect.right + 4;
        else if (activeRect.left < labelRect.right) scroll.scrollLeft -= labelRect.right - activeRect.left + 4;
        const focused = document.activeElement;
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
    for (const header of container.querySelectorAll('.frame-chip')) header.classList.toggle('active', Number(header.dataset.frame) === state.frame);
    for (const cell of container.querySelectorAll('.layer-frame-cell')) {
      const selectedFrame = Number(cell.dataset.frame) === state.frame;
      cell.classList.toggle('selected-frame', selectedFrame);
      cell.classList.toggle('active', selectedFrame && cell.dataset.layerId === state.selected);
    }
  }

  function addCanvasKeyframe(id) {
    const module = byId(id), state = module && stateFor(module), layer = module?.layers.find(item => item.id === state.selected); if (!layer) return;
    const properties = resolvedProperties(module, layer, state.frame); let keyframe = layer.keyframes.find(item => item.frame === state.frame);
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
    return {item, mediaFrames, clipped: mediaFrames < requestedFrames};
  }
  window.expandTimelineForAnimatedMedia = expandTimelineForAnimatedMedia;

  function initializeLayerAtCurrentFrame(module, layer) {
    const state = stateFor(module), initialProperties = clone(layer.properties);
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
      const clipping = expansion.clipped ? ' The 120-frame limit was reached.' : '';
      setMessage(layer.name + ' added; ' + expansion.mediaFrames + ' timeline frames created automatically at ' + module.fps + ' FPS.' + clipping);
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

  function copyFrame(module) { const state = stateFor(module); state.clipboard = module.layers.map(layer => ({layer_id: layer.id, properties: resolvedProperties(module, layer, state.frame)})); setMessage('Frame ' + (state.frame + 1) + ' copied.'); }
  function pasteFrame(module) { const state = stateFor(module); if (!state.clipboard) return; for (const snapshot of state.clipboard) { const layer = module.layers.find(item => item.id === snapshot.layer_id); if (layer) { module.overrides = module.overrides.filter(item => item.frame !== state.frame || item.layer_id !== layer.id); module.overrides.push({frame: state.frame, layer_id: layer.id, properties: clone(snapshot.properties)}); } } refreshAdvanced(module.id, true); }
  function duplicateFrame(module) {
    if (module.frame_count >= 120) { setMessage('Animations are limited to 120 frames.', true); return; }
    const state = stateFor(module), insert = state.frame + 1, snapshots = module.layers.map(layer => ({layer_id: layer.id, properties: resolvedProperties(module, layer, state.frame)}));
    for (const layer of module.layers) for (const keyframe of layer.keyframes) if (keyframe.frame >= insert) keyframe.frame++;
    for (const override of module.overrides) if (override.frame >= insert) override.frame++;
    module.frame_count++; state.frame = insert; for (const snapshot of snapshots) module.overrides.push({frame: insert, layer_id: snapshot.layer_id, properties: snapshot.properties}); refreshAdvanced(module.id, true);
  }

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
    const state = stateFor(module); card.replaceChildren(); card.className = 'custom-module-card advanced-card'; card.dataset.id = module.id; card.dataset.editorType = 'canvas'; headerFor(module, card, true);
    const layout = document.createElement('div'); layout.className = 'advanced-layout'; const stage = document.createElement('div'); stage.className = 'canvas-stage'; const sidebar = document.createElement('div'); sidebar.className = 'advanced-sidebar';
    const nameRow = document.createElement('div'); nameRow.className = 'canvas-toolbar'; nameRow.append(moduleControl('Name', module.name, 'text', value => { module.name = value; syncCustomModuleUI(); }), moduleControl('Background', module.background, 'color', value => { module.background = value; drawEditor(module, canvas); }), moduleControl('Frames', module.frame_count, 'number', value => { module.frame_count = Math.max(1, Math.min(120, Math.round(value))); module.overrides = module.overrides.filter(item => item.frame < module.frame_count); for (const layer of module.layers) layer.keyframes = layer.keyframes.filter(item => item.frame < module.frame_count); refreshAdvanced(module.id); }), moduleControl('FPS', module.fps, 'number', value => { module.fps = Math.max(1, Math.min(60, value)); }), moduleControl('Playback', module.playback, 'select', value => { module.playback = value; }, ['loop', 'ping_pong', 'hold']), moduleControl('Rotation snap', state.rotationSnap || 'off', 'select', value => { state.rotationSnap = value === 'off' ? 0 : Number(value); }, ['off', '15', '30', '45', '90']));
    const templateRow = document.createElement('div'); templateRow.className = 'template-row'; const template = document.createElement('select'); [...canvasStarterExamples, {id: 'blank', label: 'Blank canvas'}].forEach(({id, label}) => { const option = document.createElement('option'); option.value = id; option.textContent = label; template.append(option); }); const templateLabel = document.createElement('label'); templateLabel.textContent = 'Starter example'; templateLabel.append(template); templateRow.append(templateLabel, makeButton('Load example', () => applyTemplate(module.id, template.value)));
    const canvasWrap = document.createElement('div'); canvasWrap.className = 'editor-canvas-wrap'; const canvas = document.createElement('canvas'); canvas.className = 'editor-canvas'; canvas.width = 640; canvas.height = 320; canvas.setAttribute('aria-label', 'Direct 64 by 32 custom module canvas editor'); canvasWrap.append(canvas);
    const help = document.createElement('p'); help.className = 'canvas-help'; help.textContent = 'Drag a layer to move it. Drag the square handle to resize, the round handle to rotate, or draw directly when a Pixels layer is selected. Higher depth values draw in front; keyframe Depth to change the stack on a chosen frame.';
    const timelineTools = document.createElement('div'); timelineTools.className = 'timeline-toolbar'; const onion = moduleControl('Onion skin', state.onion, 'checkbox', value => { state.onion = value; drawEditor(module, canvas); }); timelineTools.append(onion, makeButton(state.playing ? 'Stop browser preview' : 'Play browser preview', () => playBrowserPreview(module), 'btn primary'), makeButton('Copy frame', () => copyFrame(module)), makeButton('Paste frame', () => pasteFrame(module)), makeButton('Duplicate frame', () => duplicateFrame(module)));
    const timeline = document.createElement('div'); timeline.className = 'timeline';
    stage.append(nameRow, templateRow, canvasWrap, help, timelineTools, timeline); const layers = document.createElement('div'); const properties = document.createElement('div'); properties.className = 'advanced-section'; sidebar.append(layers, properties); layout.append(stage, sidebar); card.append(layout);
    renderTimeline(module, timeline); renderLayers(module, layers); renderProperties(module, properties); bindCanvasPointerTransforms(module, canvas); drawEditor(module, canvas); restoreEditorViewState(card, state);
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
