// GLSL post-process visual styles for the WorldView globe.
//
// Shaders and the stage-registry design are adapted from gods-eye-view
// (https://github.com/bilawalsidhu/gods-eye-view, MIT license, Copyright (c)
// 2026 Bilawal Sidhu).
//
// Each style is one Cesium.PostProcessStage running on the GPU. The page keeps
// its original CSS canvas filters as a fallback, so `supported` is the switch
// the caller reads: true means the GLSL path is live and the CSS filters must
// be switched off, false means the reverse.
//
// Shader portability: only constructs that survive both GLSL ES 1.00 and 3.00
// are used (texture lookups, mix, smoothstep, fract, mod, sin, dot, clamp,
// pow, floor, abs, step). No integer arrays, no bit operations, no loops with
// non-constant bounds. Cesium supplies colorTexture and colorTextureDimensions
// automatically, so neither appears in a stage's uniforms object.

(function (global) {
  'use strict';

  var HELPERS = [
    'float wvHash(vec2 p) {',
    '  vec3 q = fract(vec3(p.xyx) * 0.1031);',
    '  q += dot(q, q.yzx + 33.33);',
    '  return fract((q.x + q.y) * q.z);',
    '}',
    'float wvVignette(vec2 uv, float strength) {',
    '  vec2 d = uv * (1.0 - uv);',
    '  float v = clamp(pow(d.x * d.y * 18.0, 0.28), 0.0, 1.0);',
    '  return mix(1.0, v, clamp(strength, 0.0, 1.0));',
    '}'
  ].join('\n');

  // ---- CRT: barrel curvature, phosphor tint, scanlines, shadow mask -------
  var CRT_SHADER = [
    'uniform sampler2D colorTexture;',
    'uniform vec2 colorTextureDimensions;',
    'uniform float intensity;',
    'uniform float curvature;',
    'uniform float scanlines;',
    'uniform float time;',
    'in vec2 v_textureCoordinates;',
    HELPERS,
    'void main() {',
    '  vec2 uv = v_textureCoordinates;',
    '  vec3 src = texture(colorTexture, uv).rgb;',
    '  vec2 c = uv * 2.0 - 1.0;',
    '  c *= 1.0 + dot(c, c) * curvature * 0.30;',
    '  vec2 duv = c * 0.5 + 0.5;',
    '  if (duv.x < 0.0 || duv.x > 1.0 || duv.y < 0.0 || duv.y > 1.0) {',
    '    out_FragColor = vec4(mix(src, vec3(0.0), intensity), 1.0);',
    '    return;',
    '  }',
    '  vec2 off = (duv - 0.5) * 0.005;',
    '  vec3 col = vec3(',
    '    texture(colorTexture, duv + off).r,',
    '    texture(colorTexture, duv).g,',
    '    texture(colorTexture, duv - off).b);',
    '  float luma = dot(col, vec3(0.299, 0.587, 0.114));',
    '  col = mix(col, vec3(luma * 0.18, luma * 1.10, luma * 0.32), 0.82);',
    '  float line = sin(duv.y * colorTextureDimensions.y + time * 2.5) * 0.5 + 0.5;',
    '  col *= mix(1.0, 0.62 + 0.38 * line, clamp(scanlines, 0.0, 1.0));',
    '  float sub = mod(duv.x * colorTextureDimensions.x, 3.0);',
    '  vec3 mask = vec3(',
    '    1.0 - smoothstep(0.0, 1.0, abs(sub - 0.5)),',
    '    1.0 - smoothstep(0.0, 1.0, abs(sub - 1.5)),',
    '    1.0 - smoothstep(0.0, 1.0, abs(sub - 2.5)));',
    '  col *= mix(vec3(1.0), 0.45 + 1.10 * mask, 0.45);',
    '  col *= 1.0 - 0.03 * (sin(time * 40.0) * 0.5 + 0.5);',
    '  col += (wvHash(duv * colorTextureDimensions + time) - 0.5) * 0.035;',
    '  col *= wvVignette(duv, 0.70);',
    '  out_FragColor = vec4(mix(src, clamp(col, 0.0, 1.0), intensity), 1.0);',
    '}'
  ].join('\n');

  // ---- Night vision: gain, halo bloom, sensor grain, tube vignette --------
  var NVG_SHADER = [
    'uniform sampler2D colorTexture;',
    'uniform vec2 colorTextureDimensions;',
    'uniform float intensity;',
    'uniform float gain;',
    'uniform float grain;',
    'uniform float time;',
    'in vec2 v_textureCoordinates;',
    HELPERS,
    'void main() {',
    '  vec2 uv = v_textureCoordinates;',
    '  vec3 src = texture(colorTexture, uv).rgb;',
    '  vec2 texel = 1.0 / colorTextureDimensions;',
    '  vec3 grey = vec3(0.3333);',
    '  float luma = dot(src, vec3(0.299, 0.587, 0.114));',
    '  float halo = dot(texture(colorTexture, uv + vec2(2.0, 0.0) * texel).rgb, grey);',
    '  halo += dot(texture(colorTexture, uv - vec2(2.0, 0.0) * texel).rgb, grey);',
    '  halo += dot(texture(colorTexture, uv + vec2(0.0, 2.0) * texel).rgb, grey);',
    '  halo += dot(texture(colorTexture, uv - vec2(0.0, 2.0) * texel).rgb, grey);',
    '  halo += dot(texture(colorTexture, uv + vec2(4.0, 4.0) * texel).rgb, grey);',
    '  halo += dot(texture(colorTexture, uv - vec2(4.0, 4.0) * texel).rgb, grey);',
    '  halo /= 6.0;',
    '  float amp = pow(clamp(luma, 0.0, 1.0), 0.62) * (0.55 + gain);',
    '  amp += max(halo - 0.45, 0.0) * 1.6;',
    '  amp += (wvHash(uv * colorTextureDimensions + time * 37.0) - 0.5) * 0.20 * grain;',
    '  amp = clamp(amp, 0.0, 1.35);',
    '  vec3 col = vec3(0.06, 1.0, 0.24) * amp + vec3(0.0, 0.05, 0.02) * (1.0 - amp);',
    '  col *= wvVignette(uv, 0.85);',
    '  out_FragColor = vec4(mix(src, clamp(col, 0.0, 1.0), intensity), 1.0);',
    '}'
  ].join('\n');

  // ---- FLIR: luminance to palette, white hot and black hot ---------------
  var FLIR_SHADER = [
    'uniform sampler2D colorTexture;',
    'uniform vec2 colorTextureDimensions;',
    'uniform float intensity;',
    'uniform float polarity;',
    'uniform float palette;',
    'uniform float sensitivity;',
    'uniform float time;',
    'in vec2 v_textureCoordinates;',
    HELPERS,
    'vec3 wvIronbow(float t) {',
    '  vec3 col = mix(vec3(0.00, 0.00, 0.10), vec3(0.42, 0.00, 0.55), smoothstep(0.00, 0.30, t));',
    '  col = mix(col, vec3(0.90, 0.22, 0.10), smoothstep(0.30, 0.58, t));',
    '  col = mix(col, vec3(1.00, 0.72, 0.05), smoothstep(0.58, 0.82, t));',
    '  col = mix(col, vec3(1.00, 1.00, 0.92), smoothstep(0.82, 1.00, t));',
    '  return col;',
    '}',
    'void main() {',
    '  vec2 uv = v_textureCoordinates;',
    '  vec3 src = texture(colorTexture, uv).rgb;',
    // A thermal sensor reads emitted heat, not reflected colour, so the frame
    // is rebuilt from luminance alone and stretched around mid grey.
    '  float luma = dot(src, vec3(0.299, 0.587, 0.114));',
    '  float t = clamp((luma - 0.5) * (0.8 + sensitivity * 2.2) + 0.5, 0.0, 1.0);',
    '  t = mix(t, 1.0 - t, clamp(polarity, 0.0, 1.0));',
    '  t += (wvHash(uv * colorTextureDimensions + time * 13.0) - 0.5) * 0.03;',
    '  t = clamp(t, 0.0, 1.0);',
    '  vec3 col = mix(vec3(t), wvIronbow(t), clamp(palette, 0.0, 1.0));',
    '  float band = sin(uv.y * colorTextureDimensions.y * 0.5 - time * 6.0) * 0.5 + 0.5;',
    '  col *= 0.97 + 0.03 * band;',
    '  out_FragColor = vec4(mix(src, clamp(col, 0.0, 1.0), intensity), 1.0);',
    '}'
  ].join('\n');

  // ---- Noir: desaturate, contrast, film grain, vignette ------------------
  var NOIR_SHADER = [
    'uniform sampler2D colorTexture;',
    'uniform vec2 colorTextureDimensions;',
    'uniform float intensity;',
    'uniform float contrast;',
    'uniform float grain;',
    'uniform float time;',
    'in vec2 v_textureCoordinates;',
    HELPERS,
    'void main() {',
    '  vec2 uv = v_textureCoordinates;',
    '  vec3 src = texture(colorTexture, uv).rgb;',
    '  float luma = dot(src, vec3(0.2126, 0.7152, 0.0722));',
    '  float t = clamp((luma - 0.5) * (1.0 + contrast) + 0.48, 0.0, 1.0);',
    '  vec3 col = vec3(pow(t, 1.08)) * vec3(1.00, 0.99, 0.96);',
    '  col += (wvHash(uv * colorTextureDimensions + time * 91.0) - 0.5) * 0.10 * grain;',
    '  col *= wvVignette(uv, 0.75);',
    '  out_FragColor = vec4(mix(src, clamp(col, 0.0, 1.0), intensity), 1.0);',
    '}'
  ].join('\n');

  // ---- Snow: broadcast interference, tearing, rolling bar ----------------
  var SNOW_SHADER = [
    'uniform sampler2D colorTexture;',
    'uniform vec2 colorTextureDimensions;',
    'uniform float intensity;',
    'uniform float snow;',
    'uniform float rolling;',
    'uniform float time;',
    'in vec2 v_textureCoordinates;',
    HELPERS,
    'void main() {',
    '  vec2 uv = v_textureCoordinates;',
    '  vec3 src = texture(colorTexture, uv).rgb;',
    // Whole scanlines slip sideways for a few frames, the way a weak signal
    // tears on an analogue set.
    '  float row = floor(uv.y * colorTextureDimensions.y * 0.5);',
    '  float jitter = wvHash(vec2(row, floor(time * 12.0)));',
    '  float tear = step(0.93, jitter) * (jitter - 0.93) * 1.6;',
    '  vec2 duv = vec2(clamp(uv.x + tear * 0.35, 0.0, 1.0), uv.y);',
    '  vec3 col = vec3(',
    '    texture(colorTexture, duv + vec2(0.0025 * tear, 0.0)).r,',
    '    texture(colorTexture, duv).g,',
    '    texture(colorTexture, duv - vec2(0.0025 * tear, 0.0)).b);',
    '  float bar = fract(uv.y + time * 0.12);',
    '  col *= 1.0 + clamp(rolling, 0.0, 1.0) * 0.35 * smoothstep(0.97, 1.0, bar);',
    '  float n = wvHash(uv * colorTextureDimensions + vec2(time * 61.0, time * 29.0));',
    '  col = mix(col, vec3(n), clamp(snow, 0.0, 1.0) * (0.25 + 0.55 * step(0.90, n)));',
    '  col *= 0.92 + 0.08 * wvHash(vec2(floor(time * 20.0), 3.0));',
    '  out_FragColor = vec4(mix(src, clamp(col, 0.0, 1.0), intensity), 1.0);',
    '}'
  ].join('\n');

  // The registry. `key` is the keyboard shortcut the page binds, `cssFilter`
  // is the pre-GLSL fallback that runs when post-processing is unavailable,
  // and `uniforms` is both the default set and the live mirror of the values
  // the UI has changed.
  var STYLES = {
    normal: {
      id: 'normal', label: 'NORMAL', key: '1', shader: null, uniforms: null,
      cssFilter: ''
    },
    crt: {
      id: 'crt', label: 'CRT', key: '2', shader: CRT_SHADER,
      uniforms: { intensity: 0.9, curvature: 0.35, scanlines: 0.8, time: 0.0 },
      cssFilter: 'sepia(100%) hue-rotate(70deg) saturate(400%) brightness(0.7) contrast(1.1)'
    },
    nvg: {
      id: 'nvg', label: 'NVG', key: '3', shader: NVG_SHADER,
      uniforms: { intensity: 0.95, gain: 0.8, grain: 0.9, time: 0.0 },
      cssFilter: 'sepia(100%) hue-rotate(70deg) saturate(500%) brightness(1.2) contrast(1.4)'
    },
    flir: {
      id: 'flir', label: 'FLIR', key: '4', shader: FLIR_SHADER,
      uniforms: { intensity: 0.95, polarity: 0.0, palette: 1.0, sensitivity: 0.5, time: 0.0 },
      cssFilter: 'saturate(150%) hue-rotate(190deg) brightness(0.85) contrast(1.4)'
    },
    noir: {
      id: 'noir', label: 'NOIR', key: '5', shader: NOIR_SHADER,
      uniforms: { intensity: 0.92, contrast: 0.55, grain: 0.7, time: 0.0 },
      cssFilter: 'grayscale(100%) contrast(1.35) brightness(0.9)'
    },
    snow: {
      id: 'snow', label: 'SNOW', key: '6', shader: SNOW_SHADER,
      uniforms: { intensity: 0.85, snow: 0.45, rolling: 0.8, time: 0.0 },
      cssFilter: 'grayscale(60%) contrast(1.2) brightness(0.95)'
    }
  };

  var ORDER = ['normal', 'crt', 'nvg', 'flir', 'noir', 'snow'];

  function has(id) { return Object.prototype.hasOwnProperty.call(STYLES, id); }

  function styleForKey(key) {
    for (var i = 0; i < ORDER.length; i++) {
      if (STYLES[ORDER[i]].key === key) return ORDER[i];
    }
    return null;
  }

  function cssFilterFor(id) {
    return has(id) ? STYLES[id].cssFilter : '';
  }

  // Build the stages against a live scene. The returned manager's `supported`
  // flag tells the page which rendering path is in force. Every failure mode
  // (no PostProcessStage class, a shader the driver refuses, a render error
  // raised later) ends in the same place: stages off, supported false, and the
  // caller's onFallback callback invoked so the CSS filters come back.
  function create(Cesium, scene, options) {
    var opts = options || {};
    var mgr = {
      supported: false,
      reason: '',
      current: 'normal',
      elapsed: 0,
      stages: {},
      glowOn: false,
      glowStrength: 0
    };

    function fallback(reason) {
      mgr.supported = false;
      mgr.reason = reason;
      if (typeof opts.onFallback === 'function') opts.onFallback(reason);
    }

    mgr.setStyle = function (id) {
      if (!has(id)) id = 'normal';
      mgr.current = id;
      if (!mgr.supported) return id;
      for (var i = 0; i < ORDER.length; i++) {
        var stage = mgr.stages[ORDER[i]];
        if (stage) stage.enabled = (ORDER[i] === id);
      }
      return id;
    };

    mgr.setUniform = function (id, name, value) {
      var stage = mgr.stages[id];
      if (STYLES[id] && STYLES[id].uniforms && (name in STYLES[id].uniforms)) {
        STYLES[id].uniforms[name] = value;
      }
      if (!stage || !stage.uniforms || !(name in stage.uniforms)) return false;
      stage.uniforms[name] = value;
      return true;
    };

    mgr.getUniform = function (id, name) {
      var stage = mgr.stages[id];
      if (stage && stage.uniforms && (name in stage.uniforms)) return stage.uniforms[name];
      if (STYLES[id] && STYLES[id].uniforms) return STYLES[id].uniforms[name];
      return undefined;
    };

    // Called once per frame with the elapsed seconds, so the animated shaders
    // (scanline drift, grain, rolling bar) advance with wall time rather than
    // with the simulation clock, which the user can pause or rewind.
    mgr.tick = function (dtSeconds) {
      if (!mgr.supported) return;
      mgr.elapsed += dtSeconds;
      var stage = mgr.stages[mgr.current];
      if (stage && stage.uniforms && ('time' in stage.uniforms)) {
        stage.uniforms.time = mgr.elapsed;
      }
    };

    // Cesium's own bloom stage, used as the detection glow toggle. It is a
    // built-in, so it stays available even when the custom shaders were
    // refused by the driver.
    mgr.setGlow = function (enabled, strength) {
      mgr.glowOn = !!enabled;
      mgr.glowStrength = Math.max(0, Math.min(1, strength == null ? 0.5 : strength));
      if (!scene || !scene.postProcessStages) return false;
      var bloom = scene.postProcessStages.bloom;
      if (!bloom) return false;
      try {
        bloom.enabled = mgr.glowOn;
        if (bloom.uniforms) {
          bloom.uniforms.contrast = 128;
          bloom.uniforms.brightness = -0.6 + 0.7 * mgr.glowStrength;
          bloom.uniforms.glowOnly = false;
          bloom.uniforms.stepSize = 1.0 + 4.0 * mgr.glowStrength;
        }
        return true;
      } catch (e) {
        return false;
      }
    };

    mgr.destroy = function () {
      for (var i = 0; i < ORDER.length; i++) {
        var stage = mgr.stages[ORDER[i]];
        if (!stage) continue;
        try { stage.enabled = false; } catch (e) { /* already destroyed */ }
        try { scene.postProcessStages.remove(stage); } catch (e) { /* already gone */ }
        mgr.stages[ORDER[i]] = null;
      }
    };

    if (!Cesium || typeof Cesium.PostProcessStage !== 'function') {
      mgr.reason = 'Cesium.PostProcessStage unavailable';
      return mgr;
    }
    if (!scene || !scene.postProcessStages) {
      mgr.reason = 'scene has no post-process pipeline';
      return mgr;
    }

    try {
      for (var i = 0; i < ORDER.length; i++) {
        var def = STYLES[ORDER[i]];
        if (!def.shader) continue;
        var uniforms = {};
        for (var name in def.uniforms) uniforms[name] = def.uniforms[name];
        var stage = new Cesium.PostProcessStage({
          name: 'worldviewStyle_' + def.id,
          fragmentShader: def.shader,
          uniforms: uniforms
        });
        stage.enabled = false;
        scene.postProcessStages.add(stage);
        mgr.stages[def.id] = stage;
      }
      mgr.supported = true;
    } catch (err) {
      mgr.destroy();
      mgr.reason = (err && err.message) || 'post-process stage rejected';
      return mgr;
    }

    // A shader that compiles on one driver can still throw inside render on
    // another. Cesium raises renderError rather than tearing the page down, so
    // that is where the last-resort fallback lives.
    if (scene.renderError && scene.renderError.addEventListener) {
      scene.renderError.addEventListener(function () {
        if (!mgr.supported) return;
        mgr.destroy();
        fallback('render error with post-processing enabled');
      });
    }

    return mgr;
  }

  global.WorldViewStyles = {
    STYLES: STYLES,
    ORDER: ORDER,
    has: has,
    styleForKey: styleForKey,
    cssFilterFor: cssFilterFor,
    create: create
  };
})(this);
