/*!
 * PMem · 星图 · 深空渲染器  (M9.7b)
 *
 * 纯 WebGL2，无第三方依赖，可离线运行。职责只有一件：
 * 把 starmap_space 算出来的坐标画成一片能看的深空。
 *
 * 层次（由远到近）：
 *   1. 星云      全屏 fbm，暗红 / 粉青，暗尘带 + 中心光核
 *   2. 背景星    9000 颗程序化恒星，微小视差，缓慢闪烁
 *   3. 星尘      事件 / 召回 / 指标 / 反思 / 睡眠，亮度按时间衰减
 *   4. 连线      相似 / 共同提及 / 分组，屏幕空间软边细丝
 *   5. 光晕      每颗星的 halo，加性混合
 *   6. 星体      紧致核心 + 六芒衍射（S/A 才明显）
 *   7. 后期      bloom → ACES → 暗角 → 色散 → 颗粒
 *
 * 对外只暴露 create(canvas, opts)。
 */
(function () {
  'use strict';

  var RM = null;
  try {
    RM = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  } catch (e) { RM = false; }

  // ---------------------------------------------------------------- 小工具

  function clamp(v, a, b) { return v < a ? a : (v > b ? b : v); }
  function lerp(a, b, t) { return a + (b - a) * t; }
  function easeOutCubic(t) { return 1 - Math.pow(1 - t, 3); }
  function easeInOutCubic(t) {
    return t < 0.5 ? 4 * t * t * t : 1 - Math.pow(-2 * t + 2, 3) / 2;
  }
  function easeOutExpo(t) { return t >= 1 ? 1 : 1 - Math.pow(2, -10 * t); }

  function rngFrom(seed) {
    var s = seed >>> 0;
    return function () {
      s = (s + 0x6D2B79F5) >>> 0;
      var t = s;
      t = Math.imul(t ^ (t >>> 15), t | 1);
      t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
      return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
    };
  }

  // 一点点三维向量工具：天空基底要用。
  function v3cross(a, b) {
    return [a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0]];
  }
  function v3norm(a) {
    var l = Math.hypot(a[0], a[1], a[2]);
    if (!l || !isFinite(l)) return [0, 0, 1];
    return [a[0] / l, a[1] / l, a[2] / l];
  }
  function v3dot(a, b) { return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]; }

  // 银道坐标基底：[u, n, v] —— u/v 张成银道面，n 是银极。
  // 世界方向 d 的银道坐标就是 (dot(u,d), dot(n,d), dot(v,d))，
  // 这样天空的银河带就真的挂在天球上，镜头怎么动它都跟着。
  var GAL_TILT = 0.62;
  // 这个偏航角不是随便取的：让银道面在默认机位下横穿画面中心，
  // 一打开就能看见那条带，而不是"要转到某个角度才出现"。
  var GAL_YAW = -0.35;
  var GAL_CORE_LON = 0.55;
  function galacticBasis(tilt, yaw) {
    var n = [0, Math.cos(tilt), Math.sin(tilt)];
    var c = Math.cos(yaw), s = Math.sin(yaw);
    n = v3norm([n[0] * c + n[2] * s, n[1], -n[0] * s + n[2] * c]);
    var ref = Math.abs(n[0]) < 0.9 ? [1, 0, 0] : [0, 0, 1];
    var u = v3norm(v3cross(ref, n));
    var v = v3cross(n, u);
    return { u: u, n: n, v: v };
  }
  var GAL = galacticBasis(GAL_TILT, GAL_YAW);
  // 银道面里的角度 → 世界方向（单位向量）
  function galacticDir(lon, lat) {
    var cl = Math.cos(lat), sl = Math.sin(lat);
    var gx = Math.cos(lon) * cl, gy = sl, gz = Math.sin(lon) * cl;
    return v3norm([
      GAL.u[0] * gx + GAL.n[0] * gy + GAL.v[0] * gz,
      GAL.u[1] * gx + GAL.n[1] * gy + GAL.v[1] * gz,
      GAL.u[2] * gx + GAL.n[2] * gy + GAL.v[2] * gz
    ]);
  }

  // ---- 和 FS_SKY 里逐字相同的一小套噪声 -----------------------------------
  //
  // 为什么要在 JS 里重写一遍：银河带是靠几十万颗颗粒堆出来的，颗粒才是"星空"
  // 的质感；但尘埃带必须同时压暗**底图**和**颗粒**，否则暗缝只盖住雾、盖不住星，
  // 看起来就像两层没对齐。所以同一套 hash/noise/fbm 在这里跑一次，
  // 把"被尘埃挡住多少"直接烘进每颗颗粒的透明度里。
  function fract1(x) { return x - Math.floor(x); }
  function skyHash31(x, y, z) {
    var px = fract1(x * 0.1031), py = fract1(y * 0.1031), pz = fract1(z * 0.1031);
    var d = px * (py + 33.33) + py * (pz + 33.33) + pz * (px + 33.33);
    px += d; py += d; pz += d;
    return fract1((px + py) * pz);
  }
  function skyNoise(x, y, z) {
    var ix = Math.floor(x), iy = Math.floor(y), iz = Math.floor(z);
    var fx = x - ix, fy = y - iy, fz = z - iz;
    fx = fx * fx * (3 - 2 * fx); fy = fy * fy * (3 - 2 * fy); fz = fz * fz * (3 - 2 * fz);
    var n000 = skyHash31(ix, iy, iz);
    var n100 = skyHash31(ix + 1, iy, iz);
    var n010 = skyHash31(ix, iy + 1, iz);
    var n110 = skyHash31(ix + 1, iy + 1, iz);
    var n001 = skyHash31(ix, iy, iz + 1);
    var n101 = skyHash31(ix + 1, iy, iz + 1);
    var n011 = skyHash31(ix, iy + 1, iz + 1);
    var n111 = skyHash31(ix + 1, iy + 1, iz + 1);
    var a = n000 + (n100 - n000) * fx;
    var b = n010 + (n110 - n010) * fx;
    var c = n001 + (n101 - n001) * fx;
    var d = n011 + (n111 - n011) * fx;
    return (a + (b - a) * fy) + ((c + (d - c) * fy) - (a + (b - a) * fy)) * fz;
  }
  function skyFbm(x, y, z, oct) {
    var s = 0, a = 0.5;
    for (var k = 0; k < oct; k++) {
      s += a * skyNoise(x, y, z);
      x = x * 2.07 + 7.3; y = y * 2.07 + 1.9; z = z * 2.07 + 4.1;
      a *= 0.5;
    }
    return s;
  }
  function smoothstep1(e0, e1, x) {
    var t = clamp((x - e0) / (e1 - e0), 0, 1);
    return t * t * (3 - 2 * t);
  }

  // 给定一个世界方向，返回它被银河尘埃挡住多少、以及处在星云团的哪一块。
  // 远离银道面就直接短路——绝大多数背景星都在这里被跳过。
  function skyShade(dir) {
    var x = dir[0], y = dir[1], z = dir[2];
    var gx = GAL.u[0] * x + GAL.u[1] * y + GAL.u[2] * z;
    var gy = GAL.n[0] * x + GAL.n[1] * y + GAL.n[2] * z;
    var gz = GAL.v[0] * x + GAL.v[1] * y + GAL.v[2] * z;
    if (Math.abs(gy) > 0.55) return { dim: 1, mott: 1 };
    var dd = Math.atan2(gz, gx) - GAL_CORE_LON;
    dd = Math.atan2(Math.sin(dd), Math.cos(dd));
    var cq = dd / 1.05;
    var core = Math.exp(-cq * cq);
    var halfW = 0.075 + 0.110 * core;
    var dustN = skyFbm(gx * 9 + 3.1, gy * 9 + 9.2, gz * 9 + 1.7, 3) * 0.50 +
      skyFbm(gx * 19 + 21.4, gy * 19 + 5.5, gz * 19 + 12.9, 3) * 0.32 +
      skyFbm(gx * 41 + 8.3, gy * 41 + 2.7, gz * 41 + 33.1, 2) * 0.18;
    var lane = smoothstep1(0.42, 0.86, 1 - Math.abs(dustN * 2 - 1));
    var lw = gy / (halfW * 0.86);
    var ext = lane * Math.exp(-lw * lw) * (0.55 + 0.45 * core);
    var cloud = skyFbm(gx * 3 + 11.3, gy * 3 + 4.7, gz * 3 + 7.9, 3);
    var grain = skyFbm(gx * 26 + 5.7, gy * 26 + 13.1, gz * 26 + 2.9, 2);
    var mott = (0.10 + 1.30 * cloud) * (0.55 + 1.00 * grain);
    return { dim: Math.max(0.03, 1 - 0.97 * ext), mott: clamp(mott, 0.05, 2.4) };
  }

  function hashStr(text) {
    var h = 2166136261 >>> 0;
    for (var i = 0; i < text.length; i++) {
      h ^= text.charCodeAt(i);
      h = Math.imul(h, 16777619) >>> 0;
    }
    return h >>> 0;
  }

  // ------------------------------------------------------------------ mat4

  var M4 = {
    create: function () {
      return new Float32Array([1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1]);
    },
    perspective: function (out, fovy, aspect, near, far) {
      var f = 1 / Math.tan(fovy / 2);
      var nf = 1 / (near - far);
      out[0] = f / aspect; out[1] = 0; out[2] = 0; out[3] = 0;
      out[4] = 0; out[5] = f; out[6] = 0; out[7] = 0;
      out[8] = 0; out[9] = 0; out[10] = (far + near) * nf; out[11] = -1;
      out[12] = 0; out[13] = 0; out[14] = 2 * far * near * nf; out[15] = 0;
      return out;
    },
    lookAt: function (out, eye, center, up) {
      var zx = eye[0] - center[0], zy = eye[1] - center[1], zz = eye[2] - center[2];
      var len = Math.hypot(zx, zy, zz) || 1;
      zx /= len; zy /= len; zz /= len;
      var xx = up[1] * zz - up[2] * zy, xy = up[2] * zx - up[0] * zz, xz = up[0] * zy - up[1] * zx;
      len = Math.hypot(xx, xy, xz) || 1;
      xx /= len; xy /= len; xz /= len;
      var yx = zy * xz - zz * xy, yy = zz * xx - zx * xz, yz = zx * xy - zy * xx;
      out[0] = xx; out[1] = yx; out[2] = zx; out[3] = 0;
      out[4] = xy; out[5] = yy; out[6] = zy; out[7] = 0;
      out[8] = xz; out[9] = yz; out[10] = zz; out[11] = 0;
      out[12] = -(xx * eye[0] + xy * eye[1] + xz * eye[2]);
      out[13] = -(yx * eye[0] + yy * eye[1] + yz * eye[2]);
      out[14] = -(zx * eye[0] + zy * eye[1] + zz * eye[2]);
      out[15] = 1;
      return out;
    }
  };

  // ------------------------------------------------------------------ 噪声

  // -------------------------------------------------------------- 着色器源码

  var VS_QUAD = [
    '#version 300 es',
    'layout(location=0) in vec2 aClip;',
    'out vec2 vUv;',
    'void main(){ vUv = aClip*0.5+0.5; gl_Position = vec4(aClip,0.0,1.0); }'
  ].join('\n');

  // 躺在盘面上的四边形：现在只给"时间回放的光锋"用。
  var VS_DISK_QUAD = [
    '#version 300 es',
    'layout(location=0) in vec2 aClip;',
    'uniform mat4 uView, uProj;',
    'uniform float uScale;',
    'out vec2 vUv;',
    'void main(){',
    '  vec3 world = vec3(aClip.x * uScale, 0.0, aClip.y * uScale);',
    '  vUv = aClip * 0.5 + 0.5;',
    '  gl_Position = uProj * uView * vec4(world, 1.0);',
    '}'
  ].join('\n');

  // 时间回放的光锋：一道从中心向外扩的亮环，躺在盘面上。
  // 这不是装饰——它每一帧都停在"当前时间"，所以是"记忆长到这里"的可见边界。
  var FS_FRONT = [
    '#version 300 es',
    'precision highp float;',
    'in vec2 vUv;',
    'uniform float uRadius;',
    'uniform float uTime;',
    'uniform float uOpacity;',
    'uniform vec3 uColor;',
    'out vec4 outColor;',
    'void main(){',
    '  vec2 p = vUv * 2.0 - 1.0;',
    '  float r = length(p);',
    '  float ring = smoothstep(uRadius - 0.014, uRadius, r)',
    '             * (1.0 - smoothstep(uRadius, uRadius + 0.014, r));',
    '  if (ring < 0.0015) discard;',
    '  float ang = atan(p.y, p.x);',
    '  float beam = 0.45 + 0.55 * pow(abs(sin(ang * 1.5 - uTime * 0.7)), 3.0);',
    '  outColor = vec4(uColor * ring * beam * uOpacity, 1.0);',
    '}'
  ].join('\n');

  var VS_POINT = [
    '#version 300 es',
    'precision highp float;',
    'layout(location=0) in vec3 aPos;',
    'layout(location=1) in vec4 aColor;',
    'layout(location=2) in float aSize;',
    'layout(location=3) in float aSpike;',
    'layout(location=4) in float aSeed;',
    'uniform mat4 uView, uProj;',
    'uniform float uPix;',
    'uniform float uSizeMul;',
    'uniform float uMinSize;',
    'uniform float uAlphaMul;',
    'uniform float uTime;',
    'uniform float uTwinkle;',
    'out vec4 vColor;',
    'out float vSpike;',
    'out float vSeed;',
    'out float vGrow;',
    'void main(){',
    '  vec4 mv = uView * vec4(aPos, 1.0);',
    '  gl_Position = uProj * mv;',
    '  float depth = max(0.03, -mv.z);',
    '  float px = aSize * uSizeMul * uPix / depth;',
    '  vGrow = clamp(px / max(uMinSize, 0.001), 0.0, 1.0);',
    '  gl_PointSize = clamp(max(px, uMinSize), 1.0, 180.0);',
    '  float phase = uTime * (1.05 + aSeed * 2.1) + aSeed * 6.2831;',
    '  float tw = 1.0 - uTwinkle * (0.5 + 0.5 * sin(phase));',
    '  vColor = vec4(aColor.rgb, aColor.a * uAlphaMul * tw * (0.28 + 0.72 * vGrow));',
    '  vSpike = aSpike;',
    '  vSeed = aSeed;',
    '}'
  ].join('\n');

  var FS_POINT = [
    '#version 300 es',
    'precision highp float;',
    'in vec4 vColor;',
    'in float vSpike;',
    'in float vSeed;',
    'in float vGrow;',
    'uniform float uMode;',   // 0 = 核心+衍射, 1 = 光晕
    'uniform float uGain;',
    'out vec4 outColor;',
    'void main(){',
    '  vec2 p = gl_PointCoord * 2.0 - 1.0;',
    '  float d2 = dot(p, p);',
    '  if (d2 > 1.0) discard;',
    '  float d = sqrt(d2);',
    '  float a;',
    '  if (uMode > 0.5) {',
    '    a = exp(-d2 * 2.2) * 0.40 + exp(-d2 * 7.5) * 0.60;',
    '    a *= (1.0 - smoothstep(0.78, 1.0, d)) * 0.52;',
    '  } else {',
    '    float core = exp(-d2 * 20.0);',
    '    float ang = atan(p.y, p.x);',
    '    float six = pow(abs(cos(ang * 3.0)), 46.0);',
    '    float four = pow(abs(cos(ang * 2.0)), 70.0);',
    '    float spike = (six * 0.90 + four * 0.45) * exp(-d * 2.2) * vSpike;',
    '    a = (core + spike) * (1.0 - smoothstep(0.70, 1.0, d));',
    '  }',
    '  a = clamp(a, 0.0, 1.0) * vColor.a;',
    '  outColor = vec4(vColor.rgb * a * uGain, a);',
    '}'
  ].join('\n');

  var VS_LINE = [
    '#version 300 es',
    'precision highp float;',
    'layout(location=0) in vec3 aA;',
    'layout(location=1) in vec3 aB;',
    'layout(location=2) in vec4 aColor;',
    'layout(location=3) in vec4 aGeom;',   // x=宽度(px) y=t0 z=t1 w=亮度
    'layout(location=4) in float aCorner;',// 0..3 → 四边形四个角
    'uniform mat4 uView, uProj;',
    'uniform vec2 uViewport;',
    'out vec2 vUv;',
    'out vec4 vColor;',
    'void main(){',
    '  vec4 ca = uProj * uView * vec4(aA, 1.0);',
    '  vec4 cb = uProj * uView * vec4(aB, 1.0);',
    '  float wa = max(ca.w, 0.0008);',
    '  float wb = max(cb.w, 0.0008);',
    '  vec2 ha = ca.xy / wa * uViewport * 0.5;',
    '  vec2 hb = cb.xy / wb * uViewport * 0.5;',
    '  vec2 dir = hb - ha;',
    '  float len = length(dir);',
    '  dir = len > 0.0001 ? dir / len : vec2(1.0, 0.0);',
    '  vec2 nrm = vec2(-dir.y, dir.x);',
    '  bool useB = aCorner >= 2.5;',
    '  float side = (aCorner == 0.0 || aCorner == 2.0) ? -1.0 : 1.0;',
    '  vec4 clip = useB ? cb : ca;',
    '  float w = max(aGeom.x * 0.5, 0.35);',
    '  vec2 offsetPx = nrm * side * w;',
    // 关节补口：A 端往回、B 端往前各伸 0.75 个线宽，让相邻段在接缝处叠上。
    // 不补的话每段两头各留一条 1px 缝，长连线看起来就是一串虚线。
    '  vec2 capPx = dir * (useB ? 0.75 : -0.75) * w;',
    '  clip.xy += ((offsetPx + capPx) / (uViewport * 0.5)) * clip.w;',
    '  gl_Position = clip;',
    '  float t01 = useB ? 1.0 : 0.0;',
    '  vUv = vec2(mix(aGeom.y, aGeom.z, t01), side * 0.5 + 0.5);',
    '  vColor = vec4(aColor.rgb, aColor.a * aGeom.w);',
    '}'
  ].join('\n');

  var FS_LINE = [
    '#version 300 es',
    'precision highp float;',
    'in vec2 vUv;',
    'in vec4 vColor;',
    'uniform float uAlphaMul;',
    'out vec4 outColor;',
    'void main(){',
    '  float edge = 1.0 - abs(vUv.y * 2.0 - 1.0);',
    '  float soft = edge * edge;',
    // 实线：只在两端各留 2% 收口，中段不衰减。之前 14% 的淡出会让细线看着像虚线。
    '  float fade = smoothstep(0.0, 0.02, vUv.x) * (1.0 - smoothstep(0.98, 1.0, vUv.x));',
    '  float a = soft * fade * vColor.a * uAlphaMul;',
    '  if (a < 0.0015) discard;',
    '  outColor = vec4(vColor.rgb * a, a);',
    '}'
  ].join('\n');

  // ---------------------------------------------------------------- 星空底
  //
  // 这不是"一张贴图"，也不是全屏噪声：每个像素反推出一条世界方向，再把它
  // 换算成银道坐标，才去算银河带。所以带子会随镜头透视、会转到背面、
  // 边缘会变窄——它挂在天球上。
  //
  // 一条真的银河有这些层次，缺一层就会变成一块黑布：
  //   · 盘面（profile）：中间厚、两缘薄，宽窄随经度变
  //   · 核球（bulge）  ：朝银心那一段又宽又亮，颜色偏暖
  //   · 星云团（mottle）：低频 fbm 把带子切成亮块和暗块
  //   · 尘埃带（lane） ：高频 fbm 抠出暗缝，越靠盘面越黑——这是"三层"的关键
  //   · 弥散光（halo） ：盘面外一层极淡的整体辉光，负责"不是纯黑"
  var FS_SKY = [
    '#version 300 es',
    'precision highp float;',
    'in vec2 vUv;',
    'uniform vec3 uRight;',
    'uniform vec3 uUp;',
    'uniform vec3 uFwd;',
    'uniform mat3 uGal;',
    'uniform float uTanFov;',
    'uniform float uAspect;',
    'uniform float uSky;',
    'uniform float uCoreLon;',
    'out vec4 outColor;',
    'float h31(vec3 p){',
    '  p = fract(p * 0.1031);',
    '  p += dot(p, p.yzx + 33.33);',
    '  return fract((p.x + p.y) * p.z);',
    '}',
    'float vnoise(vec3 x){',
    '  vec3 i = floor(x);',
    '  vec3 f = fract(x);',
    '  f = f * f * (3.0 - 2.0 * f);',
    '  float n000 = h31(i);',
    '  float n100 = h31(i + vec3(1.0, 0.0, 0.0));',
    '  float n010 = h31(i + vec3(0.0, 1.0, 0.0));',
    '  float n110 = h31(i + vec3(1.0, 1.0, 0.0));',
    '  float n001 = h31(i + vec3(0.0, 0.0, 1.0));',
    '  float n101 = h31(i + vec3(1.0, 0.0, 1.0));',
    '  float n011 = h31(i + vec3(0.0, 1.0, 1.0));',
    '  float n111 = h31(i + vec3(1.0, 1.0, 1.0));',
    '  float a = mix(n000, n100, f.x);',
    '  float b = mix(n010, n110, f.x);',
    '  float c = mix(n001, n101, f.x);',
    '  float d = mix(n011, n111, f.x);',
    '  return mix(mix(a, b, f.y), mix(c, d, f.y), f.z);',
    '}',
    'float fbm(vec3 p){',
    '  float s = 0.0;',
    '  float a = 0.5;',
    '  for (int k = 0; k < 4; k++){',
    '    s += a * vnoise(p);',
    '    p = p * 2.07 + vec3(7.3, 1.9, 4.1);',
    '    a *= 0.5;',
    '  }',
    '  return s;',
    '}',
    'void main(){',
    '  vec2 ndc = vUv * 2.0 - 1.0;',
    '  vec3 dir = normalize(uFwd + uRight * (ndc.x * uTanFov * uAspect) + uUp * (ndc.y * uTanFov));',
    '  vec3 g = uGal * dir;',
    '  float lat = clamp(g.y, -1.0, 1.0);',
    '  float lon = atan(g.z, g.x);',
    '  float dd = lon - uCoreLon;',
    '  dd = atan(sin(dd), cos(dd));',
    '  float cq = dd / 1.05;',
    '  float core = exp(-cq * cq);',
    '  float halfW = mix(0.075, 0.185, core);',
    '  float bq = lat / halfW;',
    '  float band = exp(-bq * bq * 1.20);',
    '  float rq = lat / 0.045;',
    '  float ridge = exp(-rq * rq);',
    '  float cloud = fbm(g * 3.0 + vec3(11.3, 4.7, 7.9));',
    '  float fine = fbm(g * 7.4 + vec3(2.6, 8.1, 5.3));',
    '  float grain = fbm(g * 26.0 + vec3(5.7, 13.1, 2.9));',
    '  float mott = (0.10 + 1.30 * cloud + 0.55 * fine) * (0.42 + 1.15 * grain);',
    '  float dustA = fbm(g * 9.0 + vec3(3.1, 9.2, 1.7));',
    '  float dustB = fbm(g * 19.0 + vec3(21.4, 5.5, 12.9));',
    '  float dustC = fbm(g * 41.0 + vec3(8.3, 2.7, 33.1));',
    '  float dustN = dustA * 0.50 + dustB * 0.32 + dustC * 0.18;',
    '  float filaments = 1.0 - abs(dustN * 2.0 - 1.0);',
    '  float lane = smoothstep(0.42, 0.86, filaments);',
    '  float lw = lat / (halfW * 0.86);',
    '  float laneW = exp(-lw * lw);',
    '  float ext = lane * laneW * (0.55 + 0.45 * core);',
    '  float diskLum = band * mott * (0.52 + 0.86 * ridge) * (1.0 - 0.97 * ext);',
    '  float hq = lat / 0.55;',
    '  float halo = exp(-hq * hq) * 0.11;',
    '  float wide = fbm(g * 1.7 + vec3(31.0, 6.0, 23.0));',
    '  vec3 warm = vec3(1.00, 0.87, 0.71);',
    '  vec3 cool = vec3(0.58, 0.72, 1.00);',
    '  float wk = clamp(core * 1.25 + (cloud - 0.5) * 0.55 + (fine - 0.5) * 0.30, 0.0, 1.0);',
    '  vec3 tint = mix(cool, warm, wk);',
    '  float ha = smoothstep(0.60, 0.88, fbm(g * 9.3 + vec3(41.0, 3.0, 17.0)));',
    '  tint += vec3(0.30, 0.04, 0.09) * ha * 0.55;',
    '  float lum = (diskLum + halo * (0.50 + 0.80 * wide)) * uSky;',
    '  vec3 col = tint * lum;',
    '  col += vec3(0.0007, 0.0010, 0.0022) * (0.55 + 0.45 * (1.0 - abs(dir.y)));',
    '  outColor = vec4(col, 1.0);',
    '}'
  ].join('\n');

  // --------------------------------------------------------------- 远景星系
  //
  // 点精灵画不出旋臂，只会是一个圆。所以星系改成世界里的一片四边形：
  // 指数盘 + 核球 + 两条对数螺旋，正面看是旋涡，侧面看就自然压成一条线。
  var VS_GAL = [
    '#version 300 es',
    'precision highp float;',
    'layout(location=0) in vec3 aPos;',
    'layout(location=1) in vec2 aUv;',
    'layout(location=2) in vec4 aColor;',
    'layout(location=3) in vec3 aShape;',
    'uniform mat4 uView, uProj;',
    'out vec2 vUv;',
    'out vec4 vColor;',
    'out vec3 vShape;',
    'void main(){',
    '  vUv = aUv;',
    '  vColor = aColor;',
    '  vShape = aShape;',
    '  gl_Position = uProj * uView * vec4(aPos, 1.0);',
    '}'
  ].join('\n');

  var FS_GAL = [
    '#version 300 es',
    'precision highp float;',
    'in vec2 vUv;',
    'in vec4 vColor;',
    'in vec3 vShape;',
    'uniform float uGain;',
    'out vec4 outColor;',
    'void main(){',
    '  float r = length(vUv);',
    '  if (r > 1.0) discard;',
    '  float ang = atan(vUv.y, vUv.x);',
    '  float disk = exp(-r * 3.1);',
    '  float bq = r * 3.4;',
    '  float bulge = exp(-bq * bq);',
    '  float arm = sin(ang * 2.0 + log(max(r, 0.06)) * vShape.y + vShape.z);',
    '  float arms = pow(0.5 + 0.5 * arm, 3.5) * exp(-r * 2.2) * vShape.x;',
    '  float a = disk * 0.55 + bulge * 1.25 + arms * 0.85;',
    '  a *= 1.0 - smoothstep(0.70, 1.0, r);',
    '  a *= vColor.a * uGain;',
    '  outColor = vec4(vColor.rgb * a, a);',
    '}'
  ].join('\n');

  var VS_SCREEN = [
    '#version 300 es',
    'layout(location=0) in vec2 aClip;',
    'out vec2 vUv;',
    'void main(){ vUv = aClip*0.5+0.5; gl_Position = vec4(aClip,0.0,1.0); }'
  ].join('\n');

  var FS_BRIGHT = [
    '#version 300 es',
    'precision highp float;',
    'in vec2 vUv;',
    'uniform sampler2D uTex;',
    'uniform float uThreshold;',
    'out vec4 outColor;',
    'void main(){',
    '  vec3 c = texture(uTex, vUv).rgb;',
    '  float l = max(c.r, max(c.g, c.b));',
    '  float k = max(l - uThreshold, 0.0) / max(l, 0.0001);',
    '  outColor = vec4(c * k, 1.0);',
    '}'
  ].join('\n');

  var FS_BLUR = [
    '#version 300 es',
    'precision highp float;',
    'in vec2 vUv;',
    'uniform sampler2D uTex;',
    'uniform vec2 uDir;',
    'out vec4 outColor;',
    'void main(){',
    '  float w0 = 0.2270270270;',
    '  float w1 = 0.1945945946;',
    '  float w2 = 0.1216216216;',
    '  float w3 = 0.0540540541;',
    '  float w4 = 0.0162162162;',
    '  vec3 c = texture(uTex, vUv).rgb * w0;',
    '  c += texture(uTex, vUv + uDir*1.0).rgb * w1;',
    '  c += texture(uTex, vUv - uDir*1.0).rgb * w1;',
    '  c += texture(uTex, vUv + uDir*2.0).rgb * w2;',
    '  c += texture(uTex, vUv - uDir*2.0).rgb * w2;',
    '  c += texture(uTex, vUv + uDir*3.0).rgb * w3;',
    '  c += texture(uTex, vUv - uDir*3.0).rgb * w3;',
    '  c += texture(uTex, vUv + uDir*4.0).rgb * w4;',
    '  c += texture(uTex, vUv - uDir*4.0).rgb * w4;',
    '  outColor = vec4(c, 1.0);',
    '}'
  ].join('\n');

  var FS_ADD = [
    '#version 300 es',
    'precision highp float;',
    'in vec2 vUv;',
    'uniform sampler2D uTex;',
    'uniform float uWeight;',
    'out vec4 outColor;',
    'void main(){ outColor = vec4(texture(uTex, vUv).rgb * uWeight, 1.0); }'
  ].join('\n');

  var FS_COMPOSITE = [
    '#version 300 es',
    'precision highp float;',
    'in vec2 vUv;',
    'uniform sampler2D uScene;',
    'uniform sampler2D uBloom;',
    'uniform float uStrength;',
    'uniform float uExposure;',
    'uniform vec2 uRes;',
    'uniform float uTime;',
    'uniform float uVignette;',
    'uniform float uGrain;',
    'out vec4 outColor;',
    'vec3 aces(vec3 x){',
    '  const float a=2.51, b=0.03, c=2.43, d=0.59, e=0.14;',
    '  return clamp((x*(a*x+b))/(x*(c*x+d)+e), 0.0, 1.0);',
    '}',
    'float hash(vec2 p){',
    '  return fract(sin(dot(p, vec2(127.1, 311.7))) * 43758.5453123);',
    '}',
    'void main(){',
    '  vec2 uv = vUv;',
    '  vec2 c = uv - 0.5;',
    '  float r2 = dot(c, c);',
    '  // 极轻的横向色散：边缘才看得出来',
    '  float ca = r2 * 0.0022;',
    '  vec3 col;',
    '  col.r = texture(uScene, uv + c * ca).r;',
    '  col.g = texture(uScene, uv).g;',
    '  col.b = texture(uScene, uv - c * ca).b;',
    '  vec2 buv = uv + c * ca * 1.6;',
    '  col += texture(uBloom, buv).rgb * uStrength;',
    '  col *= uExposure;',
    '  col = aces(col);',
    '  float vig = 1.0 - smoothstep(0.30, 0.95, r2 * 1.9) * uVignette;',
    '  col *= vig;',
    '  col = pow(max(col, 0.0), vec3(0.4545));',
    '  // 颗粒必须放在显示空间：放在色调映射之前，黑背景会被抬成一片灰。',
    '  float g = hash(uv * uRes + fract(uTime) * 91.7) - 0.5;',
    '  col += g * uGrain;',
    '  // 底色不在这里加：夜空是一整张有结构的银河底图（见 FS_SKY）。',
    '  // 以前这里加过一层常数辉光，那正是"黑布"感的来源——它只会把黑抬灰。',
    '  outColor = vec4(col, 1.0);',
    '}'
  ].join('\n');

  // ------------------------------------------------------------- GL 小工具

  function compile(gl, type, src) {
    var sh = gl.createShader(type);
    gl.shaderSource(sh, src);
    gl.compileShader(sh);
    if (!gl.getShaderParameter(sh, gl.COMPILE_STATUS)) {
      var log = gl.getShaderInfoLog(sh);
      gl.deleteShader(sh);
      throw new Error('shader: ' + log);
    }
    return sh;
  }

  function program(gl, vsSrc, fsSrc) {
    var vs = compile(gl, gl.VERTEX_SHADER, vsSrc);
    var fs = compile(gl, gl.FRAGMENT_SHADER, fsSrc);
    var p = gl.createProgram();
    gl.attachShader(p, vs);
    gl.attachShader(p, fs);
    gl.linkProgram(p);
    gl.deleteShader(vs);
    gl.deleteShader(fs);
    if (!gl.getProgramParameter(p, gl.LINK_STATUS)) {
      throw new Error('program: ' + gl.getProgramInfoLog(p));
    }
    return p;
  }

  function uniforms(gl, prog, names) {
    var out = {};
    for (var i = 0; i < names.length; i++) {
      out[names[i]] = gl.getUniformLocation(prog, names[i]);
    }
    return out;
  }

  // --------------------------------------------------------------- 颜色表

  var GRADE_COLOR = {
    S: [1.00, 0.93, 0.76],
    A: [1.00, 0.88, 0.68],
    B: [0.70, 0.87, 1.00],
    C: [0.79, 0.71, 1.00],
    D: [0.52, 0.61, 0.77]
  };
  var KIND_COLOR = {
    episodic: [0.55, 0.78, 1.00],
    semantic: [1.00, 0.88, 0.68],
    person: [1.00, 0.72, 0.84],
    relation: [0.62, 0.96, 0.86],
    experience: [1.00, 0.78, 0.52],
    goal: [0.66, 0.96, 0.72],
    meta: [0.80, 0.68, 1.00]
  };
  // 来源色（#e9b872 琥珀）：日记派生的记忆和普通记忆同等级也不同色，
  // 否则「这句是从那天日记里长出来的」在图上一眼看不出来。
  var JOURNAL_COLOR = [0.9137, 0.7216, 0.4471];
  var ENTITY_COLOR = {
    person: [1.00, 0.74, 0.86],
    place: [0.66, 0.96, 0.74],
    org: [1.00, 0.86, 0.58],
    time: [0.76, 0.70, 1.00],
    event: [0.62, 0.88, 1.00],
    knowledge: [0.82, 0.70, 1.00],
    tool: [0.64, 0.96, 0.90],
    goal: [0.66, 0.96, 0.72],
    concept: [1.00, 0.92, 0.76],
    trait: [0.82, 0.70, 1.00],
    cognition: [0.82, 0.70, 1.00]
  };
  var DUST_COLOR = {
    event: [0.50, 0.72, 1.00],
    recall: [1.00, 0.82, 0.52],
    metric: [0.60, 0.95, 0.76],
    reflect: [0.78, 0.66, 1.00],
    sleep: [1.00, 0.66, 0.80]
  };
  var LINK_COLOR = {
    similar: [0.55, 0.90, 0.95],
    co_mention: [0.45, 0.70, 1.00],
    mention: [1.00, 0.72, 0.86],
    group: [1.00, 0.82, 0.52],
    relation: [0.62, 0.96, 0.80],
    evidence: [0.80, 0.68, 1.00],
    group_chain: [0.72, 0.78, 0.95]
  };
  var LINK_ALPHA = {
    similar: 0.62,
    co_mention: 0.40,
    mention: 0.48,
    group: 0.28,
    relation: 0.66,
    evidence: 0.52,
    group_chain: 0.20
  };
  // 线宽整体抬一档：0.7~2.0px 的细线在高 DPI 屏上会被抗锯齿切成一段一段，
  // 看起来就像虚线。实线的第一要求是「连续」，所以先把它画粗。
  var LINK_WIDTH = {
    similar: 2.4,
    co_mention: 1.5,
    mention: 1.8,
    group: 1.3,
    relation: 3.0,
    evidence: 1.9,
    group_chain: 1.1
  };

  function colorOf(node) {
    if (node.type === 'group') return [1.00, 0.85, 0.60];
    if (node.type === 'trait') return [0.82, 0.70, 1.00];
    if (node.type === 'core') return [1.00, 0.96, 0.88];
    if (node.type === 'entity') return ENTITY_COLOR[node.entity_type] || [0.72, 0.80, 0.95];
    // 来源优先于等级：日记派生的记忆哪怕定过级，也先亮成琥珀色。
    // 老 JSON 没有 source_kind 字段，这里按 undefined 走普通上色，不会崩。
    if (node.type === 'memory' && node.source_kind === 'journal') return JOURNAL_COLOR;
    if (node.grade && GRADE_COLOR[node.grade]) return GRADE_COLOR[node.grade];
    return KIND_COLOR[node.kind] || [0.70, 0.80, 0.96];
  }

  // ------------------------------------------------------------------ 创建

  function create(canvas, opts) {
    opts = opts || {};
    var gl = canvas.getContext('webgl2', {
      alpha: false,
      antialias: false,
      depth: false,
      stencil: false,
      premultipliedAlpha: false,
      preserveDrawingBuffer: true,
      powerPreference: 'high-performance'
    });
    if (!gl) {
      var err = new Error('no-webgl2');
      err.code = 'no-webgl2';
      throw err;
    }

    var floatOK = !!gl.getExtension('EXT_color_buffer_float');
    if (floatOK) {
      // EXT 声明了不代表真能渲染到浮点：拿 1×1 试一次。
      var probeTex = gl.createTexture();
      gl.bindTexture(gl.TEXTURE_2D, probeTex);
      gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA16F, 1, 1, 0, gl.RGBA, gl.HALF_FLOAT, null);
      var probeFb = gl.createFramebuffer();
      gl.bindFramebuffer(gl.FRAMEBUFFER, probeFb);
      gl.framebufferTexture2D(gl.FRAMEBUFFER, gl.COLOR_ATTACHMENT0, gl.TEXTURE_2D, probeTex, 0);
      floatOK = gl.checkFramebufferStatus(gl.FRAMEBUFFER) === gl.FRAMEBUFFER_COMPLETE;
      gl.bindFramebuffer(gl.FRAMEBUFFER, null);
      gl.deleteFramebuffer(probeFb);
      gl.deleteTexture(probeTex);
    }
    var hdrFormat = floatOK ? gl.RGBA16F : gl.RGBA8;
    var hdrType = floatOK ? gl.HALF_FLOAT : gl.UNSIGNED_BYTE;

    var progFront = program(gl, VS_DISK_QUAD, FS_FRONT);
    var progPoint = program(gl, VS_POINT, FS_POINT);
    var progLine = program(gl, VS_LINE, FS_LINE);
    var progBright = program(gl, VS_SCREEN, FS_BRIGHT);
    var progBlur = program(gl, VS_SCREEN, FS_BLUR);
    var progAdd = program(gl, VS_SCREEN, FS_ADD);
    var progComposite = program(gl, VS_SCREEN, FS_COMPOSITE);
    var progSky = program(gl, VS_SCREEN, FS_SKY);
    var progGal = program(gl, VS_GAL, FS_GAL);

    var uFront = uniforms(gl, progFront, ['uView', 'uProj', 'uScale', 'uRadius', 'uTime', 'uOpacity', 'uColor']);
    var uSky = uniforms(gl, progSky,
      ['uRight', 'uUp', 'uFwd', 'uGal', 'uTanFov', 'uAspect', 'uSky', 'uCoreLon']);
    var uGalaxy = uniforms(gl, progGal, ['uView', 'uProj', 'uGain']);
    // 银道基底 → mat3（列优先：三列分别是 u / n / v）
    var galMat3 = new Float32Array([
      GAL.u[0], GAL.u[1], GAL.u[2],
      GAL.n[0], GAL.n[1], GAL.n[2],
      GAL.v[0], GAL.v[1], GAL.v[2]
    ]);
    // 天空底图里"银河整体亮度"。它只该是一个背景，不该抢记忆星。
    var SKY_GAIN = 0.0155;

    var uPoint = uniforms(gl, progPoint,
      ['uView', 'uProj', 'uPix', 'uSizeMul', 'uMinSize', 'uAlphaMul', 'uTime', 'uTwinkle', 'uMode', 'uGain']);
    var uLine = uniforms(gl, progLine,
      ['uView', 'uProj', 'uViewport', 'uAlphaMul']);
    var uBright = uniforms(gl, progBright, ['uTex', 'uThreshold']);
    var uBlur = uniforms(gl, progBlur, ['uTex', 'uDir']);
    var uAdd = uniforms(gl, progAdd, ['uTex', 'uWeight']);
    var uComposite = uniforms(gl, progComposite,
      ['uScene', 'uBloom', 'uStrength', 'uExposure', 'uRes', 'uTime', 'uVignette', 'uGrain']);

    // 全屏三角带
    var quadBuf = gl.createBuffer();
    gl.bindBuffer(gl.ARRAY_BUFFER, quadBuf);
    gl.bufferData(gl.ARRAY_BUFFER,
      new Float32Array([-1, -1, 1, -1, -1, 1, 1, 1]), gl.STATIC_DRAW);
    var quadVao = gl.createVertexArray();
    gl.bindVertexArray(quadVao);
    gl.bindBuffer(gl.ARRAY_BUFFER, quadBuf);
    gl.enableVertexAttribArray(0);
    gl.vertexAttribPointer(0, 2, gl.FLOAT, false, 0, 0);
    gl.bindVertexArray(null);


    // ---- 渲染目标 ----
    var targets = { w: 0, h: 0, scene: null, levels: [], bloom: null, sky: null };
    var maxLevels = 4;
    // 天空底图只要 1/4 分辨率：它是低频的（带子、尘埃、星云团），
    // 双线性放大之后看不出，但 4 次 fbm 的开销直接除以 16。
    var SKY_DIV = 2;

    function makeTex(w, h) {
      var tex = gl.createTexture();
      gl.bindTexture(gl.TEXTURE_2D, tex);
      gl.texImage2D(gl.TEXTURE_2D, 0, hdrFormat, Math.max(1, w | 0), Math.max(1, h | 0),
        0, gl.RGBA, hdrType, null);
      gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.LINEAR);
      gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.LINEAR);
      gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE);
      gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);
      return tex;
    }
    function makeTarget(w, h) {
      w = Math.max(1, w | 0);
      h = Math.max(1, h | 0);
      var tex = makeTex(w, h);
      var fb = gl.createFramebuffer();
      gl.bindFramebuffer(gl.FRAMEBUFFER, fb);
      gl.framebufferTexture2D(gl.FRAMEBUFFER, gl.COLOR_ATTACHMENT0, gl.TEXTURE_2D, tex, 0);
      var ok = gl.checkFramebufferStatus(gl.FRAMEBUFFER) === gl.FRAMEBUFFER_COMPLETE;
      gl.bindFramebuffer(gl.FRAMEBUFFER, null);
      return { tex: tex, fb: fb, w: w, h: h, ok: ok };
    }
    function destroyTarget(t) {
      if (!t) return;
      try { gl.deleteFramebuffer(t.fb); } catch (e) {}
      try { gl.deleteTexture(t.tex); } catch (e) {}
    }

    function ensureTargets(w, h) {
      if (targets.w === w && targets.h === h && targets.scene) return;
      destroyTarget(targets.scene);
      destroyTarget(targets.bloom);
      destroyTarget(targets.sky);
      targets.levels.forEach(function (lv) { destroyTarget(lv.a); destroyTarget(lv.b); });
      targets.w = w; targets.h = h;
      targets.scene = makeTarget(w, h);
      if (!targets.scene.ok) {
        // 浮点帧缓冲不被支持时退回 RGBA8
        destroyTarget(targets.scene);
        hdrFormat = gl.RGBA8; hdrType = gl.UNSIGNED_BYTE;
        targets.scene = makeTarget(w, h);
      }
      targets.bloom = makeTarget(Math.max(1, w >> 1), Math.max(1, h >> 1));
      targets.sky = makeTarget(Math.max(1, Math.round(w / SKY_DIV)), Math.max(1, Math.round(h / SKY_DIV)));
      targets.levels = [];
      for (var i = 0; i < maxLevels; i++) {
        var lw = Math.max(1, w >> (i + 1));
        var lh = Math.max(1, h >> (i + 1));
        targets.levels.push({ a: makeTarget(lw, lh), b: makeTarget(lw, lh), w: lw, h: lh });
      }
    }

    // ---- 场景缓冲 ----
    var scene = {
      nodes: [], byId: new Map(),
      starBuf: null, starCount: 0,
      dustBuf: null, dustCount: 0,
      bgBuf: null, bgCount: 0,
      brightBuf: null, brightCount: 0,
      lineBuf: null, lineVerts: 0,
      coreBuf: null,
      pickables: [],
      maxT: 1
    };
    var starVao = gl.createVertexArray();
    var dustVao = gl.createVertexArray();
    var bgVao = gl.createVertexArray();
    var brightVao = gl.createVertexArray();
    var bandVao = gl.createVertexArray();
    var galVao = gl.createVertexArray();
    var lineVao = gl.createVertexArray();
    var coreVao = gl.createVertexArray();
    var meteorVao = gl.createVertexArray();
    var meteorBuf = gl.createBuffer();
    var METEOR_MAX = 3;
    var METEOR_SPARKS = 16;
    var meteorData = new Float32Array(METEOR_MAX * METEOR_SPARKS * 10);
    var meteors = [];
    var nextMeteor = 5000;

    var POINT_STRIDE = (3 + 4 + 1 + 1 + 1) * 4;
    // starmap_space 给的 size 是"视觉大小"（2~6），不是世界半径。
    // 这里统一换算：size 3 在默认视距下约 4 像素。
    var SIZE_UNIT = 0.0052;

    function bindPointAttribs(vao, buf) {
      gl.bindVertexArray(vao);
      gl.bindBuffer(gl.ARRAY_BUFFER, buf);
      var stride = POINT_STRIDE;
      gl.enableVertexAttribArray(0);
      gl.vertexAttribPointer(0, 3, gl.FLOAT, false, stride, 0);
      gl.enableVertexAttribArray(1);
      gl.vertexAttribPointer(1, 4, gl.FLOAT, false, stride, 12);
      gl.enableVertexAttribArray(2);
      gl.vertexAttribPointer(2, 1, gl.FLOAT, false, stride, 28);
      gl.enableVertexAttribArray(3);
      gl.vertexAttribPointer(3, 1, gl.FLOAT, false, stride, 32);
      gl.enableVertexAttribArray(4);
      gl.vertexAttribPointer(4, 1, gl.FLOAT, false, stride, 36);
      gl.bindVertexArray(null);
    }

    function bindLineAttribs(vao, buf) {
      gl.bindVertexArray(vao);
      gl.bindBuffer(gl.ARRAY_BUFFER, buf);
      var stride = (3 + 3 + 4 + 4 + 1) * 4;
      gl.enableVertexAttribArray(0);
      gl.vertexAttribPointer(0, 3, gl.FLOAT, false, stride, 0);
      gl.enableVertexAttribArray(1);
      gl.vertexAttribPointer(1, 3, gl.FLOAT, false, stride, 12);
      gl.enableVertexAttribArray(2);
      gl.vertexAttribPointer(2, 4, gl.FLOAT, false, stride, 24);
      gl.enableVertexAttribArray(3);
      gl.vertexAttribPointer(3, 4, gl.FLOAT, false, stride, 40);
      gl.enableVertexAttribArray(4);
      gl.vertexAttribPointer(4, 1, gl.FLOAT, false, stride, 56);
      gl.bindVertexArray(null);
    }

    // 星系四边形的布局：aPos(3) aUv(2) aColor(4) aShape(3)
    var GAL_STRIDE = (3 + 2 + 4 + 3) * 4;
    function bindGalaxyAttribs(vao, buf) {
      gl.bindVertexArray(vao);
      gl.bindBuffer(gl.ARRAY_BUFFER, buf);
      gl.enableVertexAttribArray(0);
      gl.vertexAttribPointer(0, 3, gl.FLOAT, false, GAL_STRIDE, 0);
      gl.enableVertexAttribArray(1);
      gl.vertexAttribPointer(1, 2, gl.FLOAT, false, GAL_STRIDE, 12);
      gl.enableVertexAttribArray(2);
      gl.vertexAttribPointer(2, 4, gl.FLOAT, false, GAL_STRIDE, 20);
      gl.enableVertexAttribArray(3);
      gl.vertexAttribPointer(3, 3, gl.FLOAT, false, GAL_STRIDE, 36);
      gl.bindVertexArray(null);
    }

    // ------------------------------------------------------------- 相机
    var cam = {
      target: [0, 0, 0],
      goalTarget: [0, 0, 0],
      dist: 6.2,
      goalDist: 3.05,
      theta: -Math.PI / 2 + 0.35,
      goalTheta: -Math.PI / 2 + 0.35,
      // 机位略高：0.62 时几乎贴盘面，星系被压成一条横线（「横向有点薄」）。
      // 0.98 是斜俯视，盘面能张开，又不至于变成正上方的俯视图。
      phi: 0.98,
      goalPhi: 0.98,
      fov: 42 * Math.PI / 180,
      near: 0.02,
      far: 140
    };
    var viewMat = M4.create();
    var projMat = M4.create();
    var camPos = [0, 0, 0];

    var mode = {
      autoCruise: opts.autoCruise !== false,
      idleMs: 20000,
      lastInput: performance.now(),
      cursor: 1,
      playUntil: 0,
      playFrom: 0,
      playStart: 0,
      playing: false,
      visible: true,
      paused: false,
      hoverId: null,
      selectId: null,
      neighborSet: null,
      intro: performance.now(),
      introDone: false,
      strength: 0.74,
      exposure: 1.00,
      filters: null,        // {grade:[], kind:[], group:[], type:[]} 或 null
      flight: null
    };

    var viewport = { w: 2, h: 2, dpr: 1 };

    function resize() {
      var rect = canvas.getBoundingClientRect();
      var dpr = Math.min(window.devicePixelRatio || 1, 2);
      var w = Math.max(2, Math.round(rect.width * dpr));
      var h = Math.max(2, Math.round(rect.height * dpr));
      if (canvas.width !== w || canvas.height !== h) {
        canvas.width = w;
        canvas.height = h;
      }
      viewport.w = w; viewport.h = h; viewport.dpr = dpr;
      ensureTargets(w, h);
    }

    // ------------------------------------------------------------- 数据

    function setData(data) {
      data = data || {};
      var nodes = (data.nodes || []).filter(function (n) { return n && n.id && n.x3 !== undefined; });
      var links = data.links || [];
      var dust = data.dust || [];

      var byId = new Map();
      var maxT = 1;
      nodes.forEach(function (n) {
        if (typeof n.t !== 'number') n.t = 1;
        n.grade = n.grade || null;
        n.size = n.size || 3;
        n.spike = n.spike || 0;
        n.color = colorOf(n);
        n.flash = 0;
        byId.set(n.id, n);
        if (n.t > maxT) maxT = n.t;
      });
      scene.maxT = maxT;

      // 相邻表：悬停时只留下自己和邻居
      var adj = new Map();
      links.forEach(function (l) {
        var a = l.source, b = l.target;
        if (!byId.has(a) || !byId.has(b)) return;
        if (!adj.has(a)) adj.set(a, new Set());
        if (!adj.has(b)) adj.set(b, new Set());
        adj.get(a).add(b);
        adj.get(b).add(a);
      });
      scene.adj = adj;

      // ---- 星体缓冲 ----
      var floats = new Float32Array(nodes.length * 10);
      var prevMap = scene.byId;
      var hadData = Boolean(prevMap && prevMap.size > 0);
      var newIds = [];
      nodes.forEach(function (n, i) {
        if (hadData && !prevMap.has(n.id)) newIds.push(n.id);
        n.seenAt = performance.now();
        var o = i * 10;
        floats[o] = n.x3; floats[o + 1] = n.y3; floats[o + 2] = n.z3;
        floats[o + 3] = n.color[0]; floats[o + 4] = n.color[1]; floats[o + 5] = n.color[2];
        var alpha = n.type === 'core' ? 0.55 : (n.status === 'dormant' ? 0.45 : 0.95);
        floats[o + 6] = alpha;
        floats[o + 7] = n.size * SIZE_UNIT;
        floats[o + 8] = n.spike;
        floats[o + 9] = (hashStr(n.id) % 1000) / 1000;
        n._o = o;
      });

      if (!scene.starBuf) scene.starBuf = gl.createBuffer();
      gl.bindBuffer(gl.ARRAY_BUFFER, scene.starBuf);
      gl.bufferData(gl.ARRAY_BUFFER, floats, gl.DYNAMIC_DRAW);
      bindPointAttribs(starVao, scene.starBuf);
      scene.starData = floats;
      scene.nodes = nodes;
      scene.byId = byId;
      scene.starCount = nodes.length;

      // ---- 星尘缓冲 ----
      // 每条日志不是画一个点，而是散成一小撮尘埃：一撮的位置仍由那条日志
      // 决定，所以"活动密集的地方更亮"这件事没有被抹掉，但盘面有了颗粒。
      var DUST_SPREAD = 16;
      var stamps = [];
      dust.forEach(function (d) { stamps.push(Date.parse(d.t || '') || 0); });
      var tMin = Infinity, tMax = -Infinity;
      stamps.forEach(function (t) { if (t > 0) { if (t < tMin) tMin = t; if (t > tMax) tMax = t; } });
      if (!isFinite(tMin)) { tMin = 0; tMax = 1; }
      scene.dustTime = { min: tMin, max: tMax };

      var totalParticles = dust.length * DUST_SPREAD;
      var dfloats = new Float32Array(totalParticles * 10);
      var dustAlpha = new Array(totalParticles);
      var render = new Array(totalParticles);
      var ptr = 0;

      dust.forEach(function (d, i) {
        var col = DUST_COLOR[d.k] || [0.6, 0.7, 0.9];
        var norm = tMax > tMin ? (stamps[i] - tMin) / (tMax - tMin) : 0.5;
        norm = clamp(norm, 0, 1);
        var bright = 0.14 + 0.86 * Math.pow(norm, 1.5);
        var rand = rngFrom(hashStr(d.id || String(i)) + 7);
        var big = (d.k === 'reflect' || d.k === 'sleep');
        var core = d.k === 'recall' || d.k === 'event';
        var spread = (core ? 0.055 : 0.080) * (0.55 + rand() * 0.9);
        var bx = d.x3 || 0, by = d.y3 || 0, bz = d.z3 || 0;
        for (var k = 0; k < DUST_SPREAD; k++) {
          var a = rand() * Math.PI * 2;
          var rr = Math.pow(rand(), 0.65) * spread;
          var o = ptr * 10;
          dfloats[o] = bx + Math.cos(a) * rr;
          dfloats[o + 1] = by + (rand() - 0.5) * spread * 0.8;
          dfloats[o + 2] = bz + Math.sin(a) * rr;
          var tint = 0.86 + rand() * 0.28;
          dfloats[o + 3] = col[0] * tint;
          dfloats[o + 4] = col[1] * tint;
          dfloats[o + 5] = col[2] * tint;
          var alpha = bright * (big ? 0.44 : 0.24) * (0.5 + rand() * 0.9);
          dfloats[o + 6] = alpha;
          dfloats[o + 7] = (big ? 1.55 + rand() * 0.9 : 0.62 + rand() * 0.72) * SIZE_UNIT;
          dfloats[o + 8] = 0.0;
          dfloats[o + 9] = rand();
          dustAlpha[ptr] = alpha;
          render[ptr] = { tn: norm, src: d };
          ptr += 1;
        }
      });

      if (!scene.dustBuf) scene.dustBuf = gl.createBuffer();
      gl.bindBuffer(gl.ARRAY_BUFFER, scene.dustBuf);
      gl.bufferData(gl.ARRAY_BUFFER, dfloats, gl.DYNAMIC_DRAW);
      bindPointAttribs(dustVao, scene.dustBuf);
      scene.dustData = dfloats;
      scene.dust = render;
      scene.dustSource = dust;
      scene.dustCount = totalParticles;
      scene.dustAlpha = dustAlpha;

      // ---- 背景星 ----
      buildBackgroundStars();

      // ---- 连线 ----
      buildLinks(nodes, links, adj);

      // ---- 拾取表 ----
      var pickables = [];
      nodes.forEach(function (n) {
        if (n.type === 'core') return;
        pickables.push({ kind: 'node', ref: n, p: [n.x3, n.y3, n.z3], sx: 0, sy: 0, r: Math.max(7, n.size * 1.5) });
      });
      dust.forEach(function (d) {
        pickables.push({ kind: 'dust', ref: d, p: [d.x3, d.y3, d.z3], sx: 0, sy: 0, r: 5 });
      });
      scene.dustPickCount = dust.length;
      scene.pickables = pickables;

      if (opts.onData) opts.onData(data, newIds);
    }

    // ------------------------------------------------------------ 夜空的三层星
    //
    // 以前所有背景星都是同一个像素大小、同一档亮度，加性叠起来就是一块"布"。
    // 真的星空是幂律的：绝大多数星暗到几乎看不见，极少几颗亮到带星芒。
    // 所以每颗星先抽一个"视觉亮度" u，再由 u 同时决定它的尺寸和透明度；
    // 再按距离铺三层，近层随镜头平移得明显、远层几乎不动——这才是深度。
    var BG_LAYERS = [
      // n       rMin  rMax  bias   alpha  spike
      { n: 22000, rMin: 18.0, rMax: 54, bias: 3.30, alpha: 0.62, spike: 0.00 },
      { n: 7000, rMin: 10.0, rMax: 18, bias: 2.30, alpha: 0.85, spike: 0.14 },
      // 近场：要分两遍画（光晕 + 核心），所以单独一个缓冲
      { n: 520, rMin: 7.5, rMax: 14.0, bias: 1.35, alpha: 0.92, spike: 0.95, bright: true }
    ];
    // 尺寸按"1080p 下多少像素"来定，再反推世界尺寸：px = size * PIX_REF / depth
    var PIX_REF = 1407;
    var BG_CLUSTERS = 6;
    var BG_CLUSTER_STARS = 240;

    function buildBackgroundStars() {
      var rand = rngFrom(20261104);
      var total = BG_CLUSTERS * BG_CLUSTER_STARS;
      for (var L = 0; L < BG_LAYERS.length; L++) total += BG_LAYERS[L].n;
      var arr = new Float32Array(total * 10);
      var nar = new Float32Array((BG_LAYERS[2].n + 8) * 10);
      var p = 0, np = 0;

      function sphere() {
        var z = rand() * 2 - 1;
        var s = Math.sqrt(Math.max(0, 1 - z * z));
        var a = rand() * Math.PI * 2;
        return v3norm([Math.cos(a) * s, z * 0.86, Math.sin(a) * s]);
      }

      // 一颗星的全部属性都由它的"视觉亮度" u 决定：亮星既更大也更亮，
      // 不是一整片同样大小的白点——那正是"布"的来源。
      function emit(out, at, dir, dist, u, alphaMul, spikeAmt) {
        // 距离越远，看起来越小：这条让层次能被眼睛直接读出来。
        var distScale = clamp(12 / Math.max(dist, 0.5), 0.55, 1.25);
        var px = (0.60 + 8.4 * Math.pow(u, 2.2)) * distScale;
        var o = at * 10;
        out[o] = dir[0] * dist;
        out[o + 1] = dir[1] * dist;
        out[o + 2] = dir[2] * dist;
        var t = rand();
        var c;
        if (t < 0.14) c = [0.69, 0.82, 1.00];
        else if (t < 0.40) c = [0.90, 0.94, 1.00];
        else if (t < 0.70) c = [1.00, 0.97, 0.92];
        else if (t < 0.91) c = [1.00, 0.89, 0.75];
        else c = [1.00, 0.75, 0.60];
        // 亮星更两极化：红巨星和蓝超巨星都出现在这一档
        if (u > 0.62) c = rand() < 0.30 ? [1.00, 0.74, 0.58] : [0.68, 0.80, 1.00];
        out[o + 3] = c[0];
        out[o + 4] = c[1];
        out[o + 5] = c[2];
        out[o + 6] = (0.10 + 0.90 * Math.pow(u, 1.25)) * alphaMul * skyShade(dir).dim;
        out[o + 7] = px * dist / PIX_REF;
        out[o + 8] = spikeAmt * (u > 0.55 ? 1 : u * 0.4);
        out[o + 9] = rand();
        return at + 1;
      }

      for (var i = 0; i < BG_LAYERS.length; i++) {
        var lay = BG_LAYERS[i];
        for (var k = 0; k < lay.n; k++) {
          var u = Math.pow(rand(), lay.bias);
          var r = lay.rMin + Math.pow(rand(), 0.7) * (lay.rMax - lay.rMin);
          var d = sphere();
          if (lay.bright) np = emit(nar, np, d, r, u, lay.alpha, lay.spike);
          else p = emit(arr, p, d, r, u, lay.alpha, lay.spike);
        }
      }

      // 疏散星团：一小片天区里挤一撮亮度接近的星。
      // 有它们，天区才不是均匀噪声，而会有"这里密、那里空"。
      for (var ci = 0; ci < BG_CLUSTERS; ci++) {
        var cdir = sphere();
        var cr = 18 + rand() * 20;
        var spread = 0.05 + rand() * 0.06;
        var base = 0.22 + rand() * 0.38;
        for (var m = 0; m < BG_CLUSTER_STARS; m++) {
          var ang = rand() * Math.PI * 2;
          var rr = Math.pow(rand(), 0.6) * spread;
          var dd = v3norm([
            cdir[0] + Math.cos(ang) * rr,
            cdir[1] + (rand() - 0.5) * rr * 0.9,
            cdir[2] + Math.sin(ang) * rr
          ]);
          p = emit(arr, p, dd, cr * (0.85 + rand() * 0.3),
            clamp(base + (rand() - 0.5) * 0.55, 0.02, 1), 0.85, 0.0);
        }
      }

      if (!scene.bgBuf) scene.bgBuf = gl.createBuffer();
      gl.bindBuffer(gl.ARRAY_BUFFER, scene.bgBuf);
      gl.bufferData(gl.ARRAY_BUFFER, arr.subarray(0, p * 10), gl.STATIC_DRAW);
      bindPointAttribs(bgVao, scene.bgBuf);
      scene.bgCount = p;

      if (!scene.brightBuf) scene.brightBuf = gl.createBuffer();
      gl.bindBuffer(gl.ARRAY_BUFFER, scene.brightBuf);
      gl.bufferData(gl.ARRAY_BUFFER, nar.subarray(0, np * 10), gl.STATIC_DRAW);
      bindPointAttribs(brightVao, scene.brightBuf);
      scene.brightCount = np;

      buildMilkyWay();
      buildFarGalaxies();
    }

    // 银河带的颗粒。位置在银道坐标里生成再转到世界坐标，
    // 所以它和天空底图里的那条带、那些暗缝严格同一个方向，不会各走各的。
    function buildMilkyWay() {
      var count = 120000;
      var arr = new Float32Array(count * 10);
      var rand = rngFrom(880302);
      var CORE = GAL_CORE_LON;
      var p = 0, tries = 0;
      while (p < count && tries < count * 12) {
        tries++;
        var lon = rand() * Math.PI * 2;
        var dlon = Math.atan2(Math.sin(lon - CORE), Math.cos(lon - CORE));
        var q = dlon * dlon;
        // 朝银心那一段密度更高（核球）
        var dens = 0.18 + Math.exp(-q / 0.55);
        if (rand() * 1.18 > dens) continue;
        // 三次均匀相加近似高斯；核心区更厚
        var wide = 0.052 + 0.075 * Math.exp(-q / 0.40);
        var lat = (rand() + rand() + rand() - 1.5) * wide;
        var r = 12 + Math.pow(rand(), 0.5) * 26;
        var dir = galacticDir(lon, lat);
        var o = p * 10;
        arr[o] = dir[0] * r;
        arr[o + 1] = dir[1] * r;
        arr[o + 2] = dir[2] * r;
        var t = rand();
        arr[o + 3] = 0.64 + t * 0.30;
        arr[o + 4] = 0.74 + t * 0.22;
        arr[o + 5] = 0.92 + t * 0.08;
        var sh = skyShade(dir);
        arr[o + 6] = (0.14 + Math.pow(rand(), 2.0) * 0.70) * sh.dim
          * Math.min(2.2, 0.35 + 0.85 * sh.mott);
        arr[o + 7] = (0.80 + Math.pow(rand(), 3.0) * 3.20) * r / PIX_REF;
        arr[o + 8] = 0;
        arr[o + 9] = rand();
        p += 1;
      }
      if (!scene.bandBuf) scene.bandBuf = gl.createBuffer();
      gl.bindBuffer(gl.ARRAY_BUFFER, scene.bandBuf);
      gl.bufferData(gl.ARRAY_BUFFER, arr.subarray(0, p * 10), gl.STATIC_DRAW);
      bindPointAttribs(bandVao, scene.bandBuf);
      scene.bandCount = p;
    }

    // 远景星系：世界里的一片四边形，不是点精灵。
    // 点精灵只能画出一个圆；四边形才能有核球、指数盘和两条旋臂，
    // 从侧面看还会自然压成一条线——这比五个圆斑像"远处"得多。
    var GALAXIES = [
      { lon: 0.34, lat: 0.26, r: 33, size: 5.0, aspect: 0.30, twist: 2.3, arm: 0.85, spin: 0.61, c: [0.84, 0.89, 1.00], a: 0.075 },
      { lon: 2.20, lat: -0.31, r: 40, size: 9.4, aspect: 0.11, twist: 1.6, arm: 0.25, spin: 0.18, c: [0.96, 0.93, 0.86], a: 0.070 },
      { lon: 3.86, lat: 0.44, r: 26, size: 3.1, aspect: 0.62, twist: 2.9, arm: 0.70, spin: 0.83, c: [0.80, 0.76, 1.00], a: 0.060 },
      { lon: 5.10, lat: -0.52, r: 45, size: 7.2, aspect: 0.44, twist: 2.0, arm: 0.60, spin: 0.37, c: [0.72, 0.85, 1.00], a: 0.050 },
      { lon: 1.62, lat: 0.60, r: 52, size: 8.6, aspect: 0.72, twist: 2.6, arm: 0.55, spin: 0.05, c: [0.97, 0.88, 0.76], a: 0.045 },
      { lon: 4.42, lat: 0.12, r: 30, size: 2.2, aspect: 0.24, twist: 2.1, arm: 0.95, spin: 0.94, c: [1.00, 0.86, 0.78], a: 0.058 }
    ];

    function buildFarGalaxies() {
      var verts = GALAXIES.length * 6;
      var arr = new Float32Array(verts * 12);
      var CORNERS = [[-1, -1], [1, -1], [1, 1], [-1, -1], [1, 1], [-1, 1]];
      var o = 0;
      for (var i = 0; i < GALAXIES.length; i++) {
        var g = GALAXIES[i];
        var dir = galacticDir(g.lon, g.lat);
        // 一个与视线大致垂直的方向，用来把星系的盘面斜过来。
        var ref = Math.abs(dir[1]) < 0.9 ? [0, 1, 0] : [1, 0, 0];
        var t1 = v3norm(v3cross(dir, ref));
        var t2 = v3cross(dir, t1);
        var a2 = g.spin * Math.PI * 2;
        var perp = [
          t1[0] * Math.cos(a2) + t2[0] * Math.sin(a2),
          t1[1] * Math.cos(a2) + t2[1] * Math.sin(a2),
          t1[2] * Math.cos(a2) + t2[2] * Math.sin(a2)
        ];
        var nrm = v3norm([dir[0] + perp[0] * 0.95, dir[1] + perp[1] * 0.95, dir[2] + perp[2] * 0.95]);
        var ax = v3norm(v3cross(nrm, dir));
        var ay = v3norm(v3cross(nrm, ax));
        var cx = dir[0] * g.r, cy = dir[1] * g.r, cz = dir[2] * g.r;
        var sx = g.size, sy = g.size * g.aspect;
        for (var v = 0; v < 6; v++) {
          var su = CORNERS[v][0], sv = CORNERS[v][1];
          arr[o] = cx + ax[0] * su * sx + ay[0] * sv * sy;
          arr[o + 1] = cy + ax[1] * su * sx + ay[1] * sv * sy;
          arr[o + 2] = cz + ax[2] * su * sx + ay[2] * sv * sy;
          arr[o + 3] = su;
          arr[o + 4] = sv;
          arr[o + 5] = g.c[0];
          arr[o + 6] = g.c[1];
          arr[o + 7] = g.c[2];
          arr[o + 8] = g.a;
          arr[o + 9] = g.arm;
          arr[o + 10] = g.twist;
          arr[o + 11] = g.spin * 6.2831;
          o += 12;
        }
      }
      if (!scene.galBuf) scene.galBuf = gl.createBuffer();
      gl.bindBuffer(gl.ARRAY_BUFFER, scene.galBuf);
      gl.bufferData(gl.ARRAY_BUFFER, arr, gl.STATIC_DRAW);
      bindGalaxyAttribs(galVao, scene.galBuf);
      scene.galVerts = verts;
    }

    function buildLinks(nodes, links, adj) {
      var segs = 10;
      var list = [];
      var byId = new Map();
      nodes.forEach(function (n) { byId.set(n.id, n); });

      links.forEach(function (link, li) {
        var a = byId.get(link.source);
        var b = byId.get(link.target);
        if (!a || !b) return;
        var type = link.type || 'relation';
        var col = LINK_COLOR[type] || [0.6, 0.7, 0.9];
        var alpha = (LINK_ALPHA[type] || 0.25) * clamp((link.weight || 0.3) * 1.6 + 0.25, 0, 1.4);
        var width = LINK_WIDTH[type] || 1.0;
        // 弯曲：让连线像纤维，不像图论边
        var dx = b.x3 - a.x3, dy = b.y3 - a.y3, dz = b.z3 - a.z3;
        var len = Math.sqrt(dx * dx + dy * dy + dz * dz) || 1;
        var bend = 0.22 + ((hashStr(a.id + b.id) % 100) / 100) * 0.30;
        var nx = -dz / (Math.sqrt(dx * dx + dz * dz) || 1);
        var nz = dx / (Math.sqrt(dx * dx + dz * dz) || 1);
        var mx = (a.x3 + b.x3) / 2 + nx * len * bend * 0.14;
        var my = (a.y3 + b.y3) / 2 + len * bend * 0.10;
        var mz = (a.z3 + b.z3) / 2 + nz * len * bend * 0.14;
        var prev = [a.x3, a.y3, a.z3];
        for (var s = 1; s <= segs; s++) {
          var t = s / segs;
          var it = 1 - t;
          var cx = it * it * a.x3 + 2 * it * t * mx + t * t * b.x3;
          var cy = it * it * a.y3 + 2 * it * t * my + t * t * b.y3;
          var cz = it * it * a.z3 + 2 * it * t * mz + t * t * b.z3;
          list.push({ a: prev, b: [cx, cy, cz], t0: (s - 1) / segs, t1: t, col: col, alpha: alpha, width: width, type: type, src: link.source, dst: link.target });
          prev = [cx, cy, cz];
        }
      });

      var floats = new Float32Array(list.length * 6 * 15);
      var ptr = 0;
      var CORNERS = [0, 1, 2, 1, 3, 2];
      for (var i = 0; i < list.length; i++) {
        var seg = list[i];
        for (var v = 0; v < 6; v++) {
          var corner = CORNERS[v];
          var o = ptr;
          floats[o] = seg.a[0]; floats[o + 1] = seg.a[1]; floats[o + 2] = seg.a[2];
          floats[o + 3] = seg.b[0]; floats[o + 4] = seg.b[1]; floats[o + 5] = seg.b[2];
          floats[o + 6] = seg.col[0]; floats[o + 7] = seg.col[1]; floats[o + 8] = seg.col[2];
          floats[o + 9] = seg.alpha;
          floats[o + 10] = seg.width; floats[o + 11] = seg.t0; floats[o + 12] = seg.t1;
          floats[o + 13] = 1.0;
          floats[o + 14] = corner;
          ptr += 15;
        }
      }
      if (!scene.lineBuf) scene.lineBuf = gl.createBuffer();
      gl.bindBuffer(gl.ARRAY_BUFFER, scene.lineBuf);
      gl.bufferData(gl.ARRAY_BUFFER, floats, gl.DYNAMIC_DRAW);
      bindLineAttribs(lineVao, scene.lineBuf);
      scene.lineVerts = list.length * 6;
      scene.linkCount = links.length;
      scene.linkSegs = list;
    }

    // ------------------------------------------------------------- 交互状态

    function setCursor(t) {
      mode.cursor = clamp(t, 0, 1);
      mode.playing = false;
      refreshAlpha(true);
    }

    function setFilter(filt) {
      mode.filters = filt;
      applyFilter();
    }

    function nodeVisible(n) {
      var f = mode.filters;
      if (!f) return true;
      if (f.type && f.type.length && f.type.indexOf(n.type) < 0) return false;
      if (n.type === 'memory') {
        if (f.grade && f.grade.length) {
          var g = n.grade || 'none';
          if (f.grade.indexOf(g) < 0) return false;
        }
        if (f.kind && f.kind.length && f.kind.indexOf(n.kind) < 0) return false;
      }
      if (f.arm && f.arm.length && n.type === 'memory') {
        if (f.arm.indexOf(n.arm || '') < 0) return false;
      }
      return true;
    }

    // 一颗星现在该有多亮：隐藏、时间游标、悬停焦点，三层相乘。
    function alphaOf(n) {
      var base = n.type === 'core' ? 0.55 : (n.status === 'dormant' ? 0.45 : 0.95);
      if (n.hidden) return 0;
      if (mode.cursor < 0.995 && n.t > mode.cursor + 0.0008) return 0;
      var now = performance.now();
      // 入场不是"啪"地全亮：从中心往外，按 t 依次点亮。
      var intro = clamp((now - mode.intro - n.t * 720) / 900, 0, 1);
      // 时间回放扫过时，刚被让到的那颗再淡入一下。
      var born = n.born ? clamp((now - n.born) / 420, 0, 1) : 1;
      var value = base * (0.10 + 0.90 * intro) * (0.28 + 0.72 * born);
      var focus = mode.neighborSet || mode.selectSet || null;
      if (focus) value = focus.has(n.id) ? Math.min(1.5, value * 1.5) : value * 0.15;
      return value;
    }

    function dustAlphaOf(d, i) {
      var base = scene.dustAlpha ? scene.dustAlpha[i] : 0.3;
      if (mode.cursor < 0.995 && d.tn > mode.cursor + 0.0008) return 0;
      var focus = mode.neighborSet || mode.selectSet || null;
      return base * (focus ? 0.20 : 1);
    }

    var alphaDirty = false;
    var lastAlpha = 0;
    function refreshAlpha(force) {
      if (!scene.starData) return;
      var now = performance.now();
      if (!force && now - lastAlpha < 55) { alphaDirty = true; return; }
      lastAlpha = now;
      alphaDirty = false;
      var data = scene.starData;
      var now = performance.now();
      for (var i = 0; i < scene.nodes.length; i++) {
        var n = scene.nodes[i];
        var vis = !n.hidden && !(mode.cursor < 0.995 && n.t > mode.cursor + 0.0008);
        if (vis && !n._wasVis) n.born = now;
        n._wasVis = vis;
        data[n._o + 6] = alphaOf(n);
      }
      gl.bindBuffer(gl.ARRAY_BUFFER, scene.starBuf);
      gl.bufferSubData(gl.ARRAY_BUFFER, 0, data);
      if (scene.dustData && scene.dust) {
        var dd = scene.dustData;
        for (var j = 0; j < scene.dust.length; j++) {
          var d = scene.dust[j];
          dd[d._o + 6] = dustAlphaOf(d, j);
        }
        gl.bindBuffer(gl.ARRAY_BUFFER, scene.dustBuf);
        gl.bufferSubData(gl.ARRAY_BUFFER, 0, dd);
      }
    }

    function applyFilter() {
      if (!scene.starData) return;
      scene.nodes.forEach(function (n) { n.hidden = !nodeVisible(n); });
      refreshAlpha(true);
    }

    function neighborsOf(id) {
      if (!scene.adj || !scene.adj.has(id)) return new Set();
      return scene.adj.get(id);
    }

    function setHover(id) {
      if (mode.hoverId === id) return;
      mode.hoverId = id;
      var set = id ? neighborsOf(id) : null;
      if (id) set.add(id);
      mode.neighborSet = set;
      updateEmphasis();
    }

    function setSelect(id) {
      mode.selectId = id;
      var set = id ? neighborsOf(id) : null;
      if (id) set.add(id);
      mode.selectSet = set;
      updateEmphasis();
    }

    function updateEmphasis() {
      refreshAlpha(true);
    }

    // ------------------------------------------------------------- 相机控制

    function orbitTo(theta, phi) {
      cam.goalTheta = theta;
      cam.goalPhi = clamp(phi, -1.35, 1.35);
      mode.lastInput = performance.now();
      mode.introDone = true;
    }

    function dolly(delta) {
      cam.goalDist = clamp(cam.goalDist * delta, 0.42, 18);
      mode.lastInput = performance.now();
      mode.introDone = true;
    }

    function pan(dx, dy) {
      var scale = cam.goalDist * 0.0016;
      var right = [Math.cos(cam.theta), 0, Math.sin(cam.theta)];
      var fwd = [-Math.sin(cam.theta), 0, Math.cos(cam.theta)];
      cam.goalTarget[0] += (-right[0] * dx + fwd[0] * dy) * scale;
      cam.goalTarget[2] += (-right[2] * dx + fwd[2] * dy) * scale;
      mode.lastInput = performance.now();
      mode.introDone = true;
    }

    function flyTo(node, distance) {
      var d = distance || Math.max(1.0, Math.min(2.2, cam.goalDist * 0.55));
      mode.flight = {
        fromTarget: cam.goalTarget.slice(),
        toTarget: [node.x3, node.y3, node.z3],
        fromDist: cam.goalDist,
        toDist: d,
        fromTheta: cam.goalTheta,
        toTheta: cam.goalTheta,
        fromPhi: cam.goalPhi,
        toPhi: clamp(cam.goalPhi * 0.75 + 0.28, -1.0, 1.2),
        start: performance.now(),
        dur: RM ? 1 : 1000
      };
      mode.introDone = true;
    }

    function resetView() {
      mode.flight = {
        fromTarget: cam.goalTarget.slice(),
        toTarget: [0, 0, 0],
        fromDist: cam.goalDist,
        toDist: 3.05,
        fromTheta: cam.goalTheta,
        toTheta: -Math.PI / 2 + 0.35,
        fromPhi: cam.goalPhi,
        toPhi: 0.62,
        start: performance.now(),
        dur: RM ? 1 : 1100
      };
    }

    function updateCamera(dt) {
      if (mode.flight) {
        var f = mode.flight;
        var t = clamp((performance.now() - f.start) / f.dur, 0, 1);
        var e = easeInOutCubic(t);
        cam.goalTarget[0] = lerp(f.fromTarget[0], f.toTarget[0], e);
        cam.goalTarget[1] = lerp(f.fromTarget[1], f.toTarget[1], e);
        cam.goalTarget[2] = lerp(f.fromTarget[2], f.toTarget[2], e);
        cam.goalDist = lerp(f.fromDist, f.toDist, e);
        cam.goalTheta = lerp(f.fromTheta, f.toTheta, e);
        cam.goalPhi = lerp(f.fromPhi, f.toPhi, e);
        if (t >= 1) mode.flight = null;
      }

      if (!mode.introDone && !mode.flight) {
        var it = clamp((performance.now() - mode.intro) / 2600, 0, 1);
        var ie = easeOutExpo(it);
        cam.goalDist = lerp(6.2, 3.05, ie);
        cam.goalTheta = lerp(-Math.PI / 2 + 0.35, -Math.PI / 2 + 0.35, ie);
        if (it >= 1) mode.introDone = true;
      }

      if (mode.autoCruise && !mode.flight) {
        var idle = performance.now() - mode.lastInput;
        if (idle > mode.idleMs) {
          cam.goalTheta += (RM ? 0 : 0.00035) * dt * clamp((idle - mode.idleMs) / 4000, 0, 1);
        }
      }

      if (mode.playing) {
        var now = performance.now();
        var p = clamp((now - mode.playStart) / 24000, 0, 1);
        mode.cursor = easeInOutCubic(p) * 1.0;
        if (p >= 1) { mode.playing = false; mode.cursor = 1; }
        refreshAlpha(false);
        if (opts.onCursor) opts.onCursor(mode.cursor, mode.playing);
      }

      var k = 1 - Math.pow(0.0018, dt / 1000);
      cam.target[0] = lerp(cam.target[0], cam.goalTarget[0], k);
      cam.target[1] = lerp(cam.target[1], cam.goalTarget[1], k);
      cam.target[2] = lerp(cam.target[2], cam.goalTarget[2], k);
      cam.dist = lerp(cam.dist, cam.goalDist, k);
      cam.theta = lerp(cam.theta, cam.goalTheta, k);
      cam.phi = lerp(cam.phi, cam.goalPhi, k);

      // 闲置一会儿之后，镜头开始极轻地呼吸——不是转，是像被托着。
      var idleFor = performance.now() - mode.lastInput;
      var breathe = RM ? 0 : Math.sin(performance.now() * 0.00019) * 0.009
        * clamp((idleFor - 3500) / 6000, 0, 1);
      var phiNow = cam.phi + breathe;
      var cp = Math.cos(phiNow), sp = Math.sin(phiNow);
      camPos[0] = cam.target[0] + cam.dist * cp * Math.cos(cam.theta);
      camPos[1] = cam.target[1] + cam.dist * sp;
      camPos[2] = cam.target[2] + cam.dist * cp * Math.sin(cam.theta);

      M4.lookAt(viewMat, camPos, cam.target, [0, 1, 0]);
    }

    // ------------------------------------------------------------- 绘制

    function drawQuad() {
      gl.bindVertexArray(quadVao);
      gl.drawArrays(gl.TRIANGLE_STRIP, 0, 4);
      gl.bindVertexArray(null);
    }

    function bindTarget(t) {
      gl.bindFramebuffer(gl.FRAMEBUFFER, t ? t.fb : null);
      gl.viewport(0, 0, t ? t.w : viewport.w, t ? t.h : viewport.h);
    }

    function pixScale() {
      var f = 1 / Math.tan(cam.fov / 2);
      return viewport.h * 0.5 * f;
    }

    // ---- 顺手的小动效：光锋 / 流星 / 选中环 --------------------------------

    // 时间回放的光锋：停在"现在亮到哪了"那个半径上，随游标往外推。
    function drawFront(now) {
      if (mode.cursor >= 0.995) return;
      var edge = 0;
      for (var i = 0; i < scene.nodes.length; i++) {
        var n = scene.nodes[i];
        if (n.type === 'core' || n.hidden) continue;
        if (n.t > mode.cursor + 0.0008) continue;
        var r = Math.sqrt(n.x3 * n.x3 + n.z3 * n.z3);
        if (r > edge) edge = r;
      }
      if (edge <= 0.02) return;
      var scale = 1.30;
      gl.useProgram(progFront);
      gl.uniformMatrix4fv(uFront.uView, false, viewMat);
      gl.uniformMatrix4fv(uFront.uProj, false, projMat);
      gl.uniform1f(uFront.uScale, scale);
      gl.uniform1f(uFront.uRadius, clamp(edge / scale, 0.02, 0.985));
      gl.uniform1f(uFront.uTime, RM ? 0 : now * 0.001);
      gl.uniform1f(uFront.uOpacity, 0.30);
      gl.uniform3f(uFront.uColor, 1.0, 0.80, 0.52);
      drawQuad();
    }

    function spawnMeteor(now) {
      var rand = Math.random;
      var angle = rand() * Math.PI * 2;
      var radius = 0.30 + rand() * 0.78;
      var span = 0.26 + rand() * 0.32;
      var lift = (rand() - 0.5) * 0.05;
      meteors.push({
        ax: Math.cos(angle) * radius,
        ay: lift,
        az: Math.sin(angle) * radius,
        bx: Math.cos(angle + span) * radius * (1.0 + rand() * 0.12),
        by: lift * (rand() - 0.5),
        bz: Math.sin(angle + span) * radius * (1.0 + rand() * 0.12),
        born: now,
        life: 1500 + rand() * 900
      });
      if (meteors.length > METEOR_MAX) meteors.shift();
    }

    // 流星不是一条线，是一串逐渐变小变暗的火花——这样才有速度感。
    function drawMeteors(now) {
      if (RM) return;
      if (now > nextMeteor && meteors.length < METEOR_MAX) {
        spawnMeteor(now);
        nextMeteor = now + 6000 + Math.random() * 9000;
      }
      meteors = meteors.filter(function (m) { return now - m.born < m.life; });
      if (!meteors.length) return;

      var ptr = 0;
      for (var i = 0; i < meteors.length; i++) {
        var m = meteors[i];
        var k = clamp((now - m.born) / m.life, 0, 1);
        var head = easeOutCubic(k);
        var fadeOut = 1 - Math.pow(k, 2.4);
        if (head <= 0.001) continue;
        for (var j = 0; j < METEOR_SPARKS; j++) {
          var back = j / (METEOR_SPARKS - 1);
          var tpos = clamp(head - back * 0.44, 0, 1);
          var o = ptr * 10;
          meteorData[o] = lerp(m.ax, m.bx, tpos);
          meteorData[o + 1] = lerp(m.ay, m.by, tpos);
          meteorData[o + 2] = lerp(m.az, m.bz, tpos);
          var warm = 1.0 - back * 0.42;
          meteorData[o + 3] = 1.0;
          meteorData[o + 4] = 0.90 * warm + 0.10;
          meteorData[o + 5] = 0.70 * warm + 0.12;
          meteorData[o + 6] = Math.pow(1 - back, 1.5) * fadeOut * 0.95;
          meteorData[o + 7] = (1.1 - back * 0.78) * SIZE_UNIT * 3.2;
          meteorData[o + 8] = 0;
          meteorData[o + 9] = back;
          ptr += 1;
        }
      }
      if (ptr === 0) return;

      gl.useProgram(progPoint);
      gl.uniformMatrix4fv(uPoint.uView, false, viewMat);
      gl.uniformMatrix4fv(uPoint.uProj, false, projMat);
      gl.uniform1f(uPoint.uPix, pixScale());
      gl.uniform1f(uPoint.uTime, RM ? 0 : now * 0.001);
      gl.uniform1f(uPoint.uSizeMul, 1.0);
      gl.uniform1f(uPoint.uMinSize, 1.2);
      gl.uniform1f(uPoint.uAlphaMul, 1.0);
      gl.uniform1f(uPoint.uTwinkle, 0.0);
      gl.uniform1f(uPoint.uMode, 0.0);
      gl.uniform1f(uPoint.uGain, 1.6);
      gl.bindBuffer(gl.ARRAY_BUFFER, meteorBuf);
      gl.bufferSubData(gl.ARRAY_BUFFER, 0, meteorData.subarray(0, ptr * 10));
      gl.bindVertexArray(meteorVao);
      gl.drawArrays(gl.POINTS, 0, ptr);
      gl.bindVertexArray(null);
    }

    function render(now) {
      var dt = now - (render._last || now);
      render._last = now;
      updateCamera(dt);

      var aspect = viewport.w / viewport.h;
      M4.perspective(projMat, cam.fov, aspect, cam.near, cam.far);

      var t = now * 0.001;
      var pix = pixScale();

      gl.disable(gl.BLEND);
      gl.disable(gl.DEPTH_TEST);

      // 0) 夜空底：银河带 / 核球 / 尘埃带 / 弥散光，全部画在 1/4 分辨率里。
      //    每个像素反推一条世界方向，所以这张图挂在天球上——镜头一转它跟着转、
      //    带子会被透视压窄，而不是贴在屏幕上的一块渐变。
      var skyT = targets.sky;
      if (skyT && skyT.ok) {
        var fwd = [cam.target[0] - camPos[0], cam.target[1] - camPos[1], cam.target[2] - camPos[2]];
        var fl = Math.hypot(fwd[0], fwd[1], fwd[2]) || 1;
        fwd = [fwd[0] / fl, fwd[1] / fl, fwd[2] / fl];
        var right = v3norm(v3cross(fwd, [0, 1, 0]));
        var upv = v3cross(right, fwd);
        bindTarget(skyT);
        gl.useProgram(progSky);
        gl.uniform3f(uSky.uRight, right[0], right[1], right[2]);
        gl.uniform3f(uSky.uUp, upv[0], upv[1], upv[2]);
        gl.uniform3f(uSky.uFwd, fwd[0], fwd[1], fwd[2]);
        gl.uniformMatrix3fv(uSky.uGal, false, galMat3);
        gl.uniform1f(uSky.uTanFov, Math.tan(cam.fov / 2));
        gl.uniform1f(uSky.uAspect, aspect);
        gl.uniform1f(uSky.uSky, SKY_GAIN);
        gl.uniform1f(uSky.uCoreLon, GAL_CORE_LON);
        drawQuad();
      }

      gl.bindFramebuffer(gl.FRAMEBUFFER, targets.scene.fb);
      gl.viewport(0, 0, viewport.w, viewport.h);
      gl.clearColor(0, 0, 0, 1);
      gl.clear(gl.COLOR_BUFFER_BIT);

      gl.enable(gl.BLEND);
      gl.blendFunc(gl.ONE, gl.ONE);

      // 把银河底图加进来。双线性放大：它是低频内容，1/4 分辨率看不出来。
      if (skyT && skyT.ok) {
        gl.useProgram(progAdd);
        gl.activeTexture(gl.TEXTURE0);
        gl.bindTexture(gl.TEXTURE_2D, skyT.tex);
        gl.uniform1i(uAdd.uTex, 0);
        gl.uniform1f(uAdd.uWeight, 1.0);
        drawQuad();
      }

      // 时间回放的光锋
      drawFront(now);

      // 2) 背景星：三层，尺寸和亮度都由星等幂律决定
      gl.useProgram(progPoint);
      gl.uniformMatrix4fv(uPoint.uView, false, viewMat);
      gl.uniformMatrix4fv(uPoint.uProj, false, projMat);
      gl.uniform1f(uPoint.uPix, pix);
      gl.uniform1f(uPoint.uTime, RM ? 0 : t);
      gl.uniform1f(uPoint.uSizeMul, 1.0);
      gl.uniform1f(uPoint.uMinSize, 0.7);
      gl.uniform1f(uPoint.uAlphaMul, 0.62);
      gl.uniform1f(uPoint.uTwinkle, 0.22);
      gl.uniform1f(uPoint.uMode, 0.0);
      gl.uniform1f(uPoint.uGain, 0.75);
      gl.bindVertexArray(bgVao);
      gl.drawArrays(gl.POINTS, 0, scene.bgCount);

      // 银河带：极暗的密集微星，靠加性叠加自己积出一条带
      gl.uniform1f(uPoint.uAlphaMul, 0.85);
      gl.uniform1f(uPoint.uTwinkle, 0.04);
      gl.uniform1f(uPoint.uGain, 0.85);
      gl.uniform1f(uPoint.uMinSize, 0.7);
      gl.bindVertexArray(bandVao);
      gl.drawArrays(gl.POINTS, 0, scene.bandCount);
      // 近场亮星：先铺一层大光晕，再点核心。它们随镜头平移最明显，
      // 是画面上唯一能证明"这些星在近处"的证据。
      if (scene.brightCount > 0) {
        gl.uniform1f(uPoint.uSizeMul, 2.6);
        gl.uniform1f(uPoint.uMinSize, 1.6);
        gl.uniform1f(uPoint.uAlphaMul, 0.22);
        gl.uniform1f(uPoint.uTwinkle, 0.26);
        gl.uniform1f(uPoint.uMode, 1.0);
        gl.uniform1f(uPoint.uGain, 1.05);
        gl.bindVertexArray(brightVao);
        gl.drawArrays(gl.POINTS, 0, scene.brightCount);
        gl.uniform1f(uPoint.uSizeMul, 1.0);
        gl.uniform1f(uPoint.uMinSize, 0.8);
        gl.uniform1f(uPoint.uAlphaMul, 0.92);
        gl.uniform1f(uPoint.uTwinkle, 0.16);
        gl.uniform1f(uPoint.uMode, 0.0);
        gl.uniform1f(uPoint.uGain, 1.70);
        gl.drawArrays(gl.POINTS, 0, scene.brightCount);
      }

      // 远景星系：不是圆斑，是带旋臂的盘
      if (scene.galVerts > 0) {
        gl.useProgram(progGal);
        gl.uniformMatrix4fv(uGalaxy.uView, false, viewMat);
        gl.uniformMatrix4fv(uGalaxy.uProj, false, projMat);
        gl.uniform1f(uGalaxy.uGain, 1.0);
        gl.bindVertexArray(galVao);
        gl.drawArrays(gl.TRIANGLES, 0, scene.galVerts);
        gl.useProgram(progPoint);
      }

      // 3) 连线
      if (scene.lineVerts > 0) {
        gl.useProgram(progLine);
        gl.uniformMatrix4fv(uLine.uView, false, viewMat);
        gl.uniformMatrix4fv(uLine.uProj, false, projMat);
        gl.uniform2f(uLine.uViewport, viewport.w, viewport.h);
        gl.uniform1f(uLine.uAlphaMul, 2.0);
        gl.bindVertexArray(lineVao);
        gl.drawArrays(gl.TRIANGLES, 0, scene.lineVerts);
      }

      // 4) 星尘
      if (scene.dustCount > 0) {
        gl.useProgram(progPoint);
        gl.uniform1f(uPoint.uSizeMul, 1.0);
        gl.uniform1f(uPoint.uMinSize, 1.1);
        gl.uniform1f(uPoint.uAlphaMul, 0.66);
        gl.uniform1f(uPoint.uTwinkle, 0.10);
        gl.uniform1f(uPoint.uMode, 0.0);
        gl.uniform1f(uPoint.uGain, 0.7);
        gl.bindVertexArray(dustVao);
        gl.drawArrays(gl.POINTS, 0, scene.dustCount);
      }

      // 偶尔一颗流星
      drawMeteors(now);

      // 5) 光晕
      gl.uniform1f(uPoint.uSizeMul, 2.35);
      gl.uniform1f(uPoint.uMinSize, 3.0);
      gl.uniform1f(uPoint.uAlphaMul, 0.62);
      gl.uniform1f(uPoint.uTwinkle, 0.26);
      gl.uniform1f(uPoint.uMode, 1.0);
      gl.uniform1f(uPoint.uGain, 0.64);
      gl.bindVertexArray(starVao);
      gl.drawArrays(gl.POINTS, 0, scene.starCount);

      // 6) 核心 + 衍射
      gl.uniform1f(uPoint.uSizeMul, 1.0);
      gl.uniform1f(uPoint.uMinSize, 1.6);
      gl.uniform1f(uPoint.uAlphaMul, 1.0);
      gl.uniform1f(uPoint.uTwinkle, 0.30);
      gl.uniform1f(uPoint.uMode, 0.0);
      gl.uniform1f(uPoint.uGain, 2.15);
      gl.drawArrays(gl.POINTS, 0, scene.starCount);
      gl.bindVertexArray(null);

      // ---- bloom ----
      var levels = targets.levels;
      gl.disable(gl.BLEND);
      gl.useProgram(progBright);
      gl.activeTexture(gl.TEXTURE0);
      gl.bindTexture(gl.TEXTURE_2D, targets.scene.tex);
      gl.uniform1i(uBright.uTex, 0);
      gl.uniform1f(uBright.uThreshold, 1.22);
      bindTarget(levels[0].a);
      drawQuad();

      gl.useProgram(progBlur);
      gl.uniform1i(uBlur.uTex, 0);
      for (var i = 0; i < maxLevels; i++) {
        var lv = levels[i];
        if (i > 0) {
          // 下采样：把上一级的结果缩到本级
          gl.useProgram(progAdd);
          gl.bindFramebuffer(gl.FRAMEBUFFER, lv.a.fb);
          gl.viewport(0, 0, lv.w, lv.h);
          gl.activeTexture(gl.TEXTURE0);
          gl.bindTexture(gl.TEXTURE_2D, levels[i - 1].a.tex);
          gl.uniform1i(uAdd.uTex, 0);
          gl.uniform1f(uAdd.uWeight, 1.0);
          drawQuad();
          gl.useProgram(progBlur);
          gl.uniform1i(uBlur.uTex, 0);
        }
        // 横向
        gl.bindFramebuffer(gl.FRAMEBUFFER, lv.b.fb);
        gl.viewport(0, 0, lv.w, lv.h);
        gl.activeTexture(gl.TEXTURE0);
        gl.bindTexture(gl.TEXTURE_2D, lv.a.tex);
        gl.uniform2f(uBlur.uDir, 1 / lv.w, 0);
        drawQuad();
        // 纵向
        gl.bindFramebuffer(gl.FRAMEBUFFER, lv.a.fb);
        gl.viewport(0, 0, lv.w, lv.h);
        gl.bindTexture(gl.TEXTURE_2D, lv.b.tex);
        gl.uniform2f(uBlur.uDir, 0, 1 / lv.h);
        drawQuad();
      }

      // 合成 bloom
      gl.bindFramebuffer(gl.FRAMEBUFFER, targets.bloom.fb);
      gl.viewport(0, 0, targets.bloom.w, targets.bloom.h);
      gl.clearColor(0, 0, 0, 1);
      gl.clear(gl.COLOR_BUFFER_BIT);
      gl.enable(gl.BLEND);
      gl.blendFunc(gl.ONE, gl.ONE);
      gl.useProgram(progAdd);
      gl.uniform1i(uAdd.uTex, 0);
      var weights = [0.26, 0.28, 0.30, 0.33];
      for (var j = 0; j < maxLevels; j++) {
        gl.activeTexture(gl.TEXTURE0);
        gl.bindTexture(gl.TEXTURE_2D, levels[j].a.tex);
        gl.uniform1f(uAdd.uWeight, weights[j]);
        drawQuad();
      }
      gl.disable(gl.BLEND);

      // ---- 后期 ----
      gl.bindFramebuffer(gl.FRAMEBUFFER, null);
      gl.viewport(0, 0, viewport.w, viewport.h);
      gl.useProgram(progComposite);
      gl.activeTexture(gl.TEXTURE0);
      gl.bindTexture(gl.TEXTURE_2D, targets.scene.tex);
      gl.uniform1i(uComposite.uScene, 0);
      gl.activeTexture(gl.TEXTURE1);
      gl.bindTexture(gl.TEXTURE_2D, targets.bloom.tex);
      gl.uniform1i(uComposite.uBloom, 1);
      gl.uniform1f(uComposite.uStrength, mode.strength);
      gl.uniform1f(uComposite.uExposure, mode.exposure);
      gl.uniform2f(uComposite.uRes, viewport.w, viewport.h);
      gl.uniform1f(uComposite.uTime, RM ? 0 : t);
      gl.uniform1f(uComposite.uVignette, 0.85);
      gl.uniform1f(uComposite.uGrain, 0.013);
      drawQuad();
      gl.activeTexture(gl.TEXTURE0);

      // ---- 屏幕坐标缓存（拾取 + 标签）----
      projectPickables();

      if (opts.onFrame) opts.onFrame(now);
    }

    function projectPoint(p, out) {
      var x = p[0], y = p[1], z = p[2];
      var vx = viewMat[0] * x + viewMat[4] * y + viewMat[8] * z + viewMat[12];
      var vy = viewMat[1] * x + viewMat[5] * y + viewMat[9] * z + viewMat[13];
      var vz = viewMat[2] * x + viewMat[6] * y + viewMat[10] * z + viewMat[14];
      var cx = projMat[0] * vx + projMat[8] * vz;
      var cy = projMat[5] * vy + projMat[9] * vz;
      var cw = -vz;
      if (cw <= 0.001) { out.sx = -99999; out.sy = -99999; out.depth = cw; return false; }
      out.sx = (cx / cw * 0.5 + 0.5) * viewport.w / viewport.dpr;
      out.sy = (1 - (cy / cw * 0.5 + 0.5)) * viewport.h / viewport.dpr;
      out.depth = cw;
      return true;
    }

    var projOut = { sx: 0, sy: 0, depth: 0 };
    function projectPickables() {
      var list = scene.pickables;
      for (var i = 0; i < list.length; i++) {
        var item = list[i];
        if (projectPoint(item.p, projOut)) {
          item.sx = projOut.sx;
          item.sy = projOut.sy;
          item.depth = projOut.depth;
        } else {
          item.sx = -99999; item.sy = -99999; item.depth = 9999;
        }
      }
    }

    function pick(px, py) {
      var best = null, bestScore = Infinity;
      var list = scene.pickables;
      for (var i = 0; i < list.length; i++) {
        var item = list[i];
        if (item.sx < -9000) continue;
        var dx = item.sx - px, dy = item.sy - py;
        var d2 = dx * dx + dy * dy;
        var r = item.r + 4;
        if (d2 > r * r) continue;
        var score = d2 / (r * r) + item.depth * 0.0004;
        if (score < bestScore) { bestScore = score; best = item; }
      }
      return best;
    }

    function screenOf(id) {
      var n = scene.byId.get(id);
      if (!n) return null;
      if (!projectPoint([n.x3, n.y3, n.z3], projOut)) return null;
      return { x: projOut.sx, y: projOut.sy, depth: projOut.depth };
    }

    // ------------------------------------------------------------- 主循环

    var raf = 0;
    var running = false;
    function loop(now) {
      if (!running) return;
      raf = requestAnimationFrame(loop);
      if (!mode.visible) return;
      render(now);
    }
    function start() {
      if (running) return;
      running = true;
      raf = requestAnimationFrame(loop);
    }
    function stop() {
      running = false;
      if (raf) cancelAnimationFrame(raf);
      raf = 0;
    }

    // 选中的环、流星的拖尾都是动态小缓冲，建一次反复用。
    gl.bindBuffer(gl.ARRAY_BUFFER, meteorBuf);
    gl.bufferData(gl.ARRAY_BUFFER, meteorData.byteLength, gl.DYNAMIC_DRAW);
    bindPointAttribs(meteorVao, meteorBuf);

    resize();
    setData(opts.data || {});

    var onResize = function () { resize(); };
    window.addEventListener('resize', onResize);

    var onVis = function () {
      mode.visible = !document.hidden;
    };
    document.addEventListener('visibilitychange', onVis);

    // ------------------------------------------------------------- 导出

    function snapshot() {
      render(performance.now());
      return canvas.toDataURL('image/png');
    }

    var api = {
      gl: gl,
      canvas: canvas,
      scene: scene,
      cam: cam,
      mode: mode,
      setData: setData,
      setCursor: setCursor,
      setFilter: setFilter,
      setHover: setHover,
      setSelect: setSelect,
      flyTo: function (id, dist) {
        var n = scene.byId.get(id);
        if (n) flyTo(n, dist);
      },
      orbitTo: orbitTo,
      dolly: dolly,
      pan: pan,
      resetView: resetView,
      pick: pick,
      screenOf: screenOf,
      start: start,
      stop: stop,
      resize: resize,
      snapshot: snapshot,
      play: function () {
        mode.playing = true;
        mode.playStart = performance.now();
        mode.playFrom = 0;
        mode.cursor = 0;
        refreshAlpha(true);
      },
      refreshAlpha: function () { refreshAlpha(true); },
      isPlaying: function () { return mode.playing; },
      setStrength: function (v) { mode.strength = v; },
      stats: function () {
        return {
          nodes: scene.starCount,
          dust: scene.dustCount,
          links: scene.linkCount,
          hdr: floatOK
        };
      },
      destroy: function () {
        stop();
        window.removeEventListener('resize', onResize);
        document.removeEventListener('visibilitychange', onVis);
      }
    };
    return api;
  }

  window.PMemStar = {
    create: create,
    colorOf: colorOf,
    GRADE_COLOR: GRADE_COLOR,
    KIND_COLOR: KIND_COLOR,
    JOURNAL_COLOR: JOURNAL_COLOR,
    DUST_COLOR: DUST_COLOR
  };
})();
