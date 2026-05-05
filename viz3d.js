// viz3d.js — Three.js scene reactive to whichever song is playing in the
// <audio id="player"> element.
//
// Visual layers (back-to-front):
//   - distant 3000-star drift field (warm, twinkles on energy)
//   - 24 floating musical-note sprites (♩ ♪ ♫ ♬ 🎵 🎶) in rotating colors —
//     orbit the center, spin, pulse opacity with the beat
//   - 3 concentric torus rings, hue cycling, expand with mid/high bands
//   - central wireframe icosahedron — pulses with bass, hue follows bass band
//   - inner solid core flashes on kick transients
//   - particle bursts on strong bass kicks (rainbow palette)
// Camera slowly orbits for depth.

(function () {
  'use strict';

  if (typeof THREE === 'undefined') return;
  const canvas = document.getElementById('viz3d');
  const player = document.getElementById('player');
  if (!canvas || !player) return;

  // ---------- Web Audio analyser hooked to the <audio> element ----------
  let audioCtx = null, analyser = null, freqData = null, source = null;
  function attachAudio() {
    if (analyser) return;
    try {
      audioCtx = new (window.AudioContext || window.webkitAudioContext)();
      source = audioCtx.createMediaElementSource(player);
      analyser = audioCtx.createAnalyser();
      analyser.fftSize = 512;
      analyser.smoothingTimeConstant = 0.78;
      source.connect(analyser);
      analyser.connect(audioCtx.destination);
      freqData = new Uint8Array(analyser.frequencyBinCount);
    } catch (e) { console.warn('[viz3d] audio attach failed', e); }
  }

  // ---------- Three.js scene ----------
  const w = () => window.innerWidth;
  const h = () => window.innerHeight;

  const renderer = new THREE.WebGLRenderer({ canvas, antialias: true, alpha: true });
  renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
  renderer.setSize(w(), h(), false);

  const scene = new THREE.Scene();
  scene.fog = new THREE.FogExp2(0x080810, 0.014);

  const camera = new THREE.PerspectiveCamera(60, w() / h(), 0.1, 200);
  camera.position.set(0, 0, 28);

  // ---------- helper: draw a glyph onto a canvas → texture (used for sprites) ----------
  function glyphTexture(glyph, color) {
    const sz = 256;
    const c = document.createElement('canvas');
    c.width = c.height = sz;
    const g = c.getContext('2d');
    g.clearRect(0, 0, sz, sz);
    // soft glow underlay
    g.shadowColor = color;
    g.shadowBlur = 30;
    g.fillStyle = color;
    g.font = 'bold 180px "Apple Color Emoji", "Segoe UI Symbol", "Symbola", serif';
    g.textAlign = 'center';
    g.textBaseline = 'middle';
    g.fillText(glyph, sz/2, sz/2);
    // second pass without glow for sharper edge
    g.shadowBlur = 0;
    g.fillText(glyph, sz/2, sz/2);
    const tex = new THREE.CanvasTexture(c);
    tex.minFilter = THREE.LinearFilter;
    return tex;
  }

  // ---------- Starfield (warm gold + scatter cool stars) ----------
  const starGeom = new THREE.BufferGeometry();
  const starCount = 3000;
  const starPos = new Float32Array(starCount * 3);
  const starCol = new Float32Array(starCount * 3);
  for (let i = 0; i < starCount; i++) {
    starPos[i*3]   = (Math.random() - 0.5) * 220;
    starPos[i*3+1] = (Math.random() - 0.5) * 220;
    starPos[i*3+2] = (Math.random() - 0.5) * 220;
    // 80% warm gold, 20% cool blue/purple sprinkle
    const c = new THREE.Color();
    if (Math.random() < 0.8) c.setHSL(0.13, 0.55, 0.6 + Math.random() * 0.3);
    else                    c.setHSL(0.55 + Math.random() * 0.25, 0.6, 0.6);
    starCol[i*3] = c.r; starCol[i*3+1] = c.g; starCol[i*3+2] = c.b;
  }
  starGeom.setAttribute('position', new THREE.BufferAttribute(starPos, 3));
  starGeom.setAttribute('color', new THREE.BufferAttribute(starCol, 3));
  const stars = new THREE.Points(starGeom, new THREE.PointsMaterial({
    size: 0.22, sizeAttenuation: true, vertexColors: true, transparent: true, opacity: 0.7
  }));
  scene.add(stars);

  // ---------- Floating musical notes ----------
  const noteGlyphs = ['♩', '♪', '♫', '♬', '🎵', '🎶'];
  const notePalette = [
    '#e9d28b', // gold
    '#ff7a8a', // pink
    '#7adcff', // cyan
    '#a78bff', // violet
    '#7fff9f', // mint
    '#ffb56b', // orange
    '#ff5acd', // magenta
    '#ffe75a', // yellow
  ];
  const NOTE_COUNT = 24;
  const notes = [];
  for (let i = 0; i < NOTE_COUNT; i++) {
    const glyph = noteGlyphs[i % noteGlyphs.length];
    const color = notePalette[i % notePalette.length];
    const tex = glyphTexture(glyph, color);
    const mat = new THREE.SpriteMaterial({ map: tex, transparent: true, opacity: 0.85,
                                           blending: THREE.AdditiveBlending });
    const sp = new THREE.Sprite(mat);
    sp.userData = {
      angle: Math.random() * Math.PI * 2,
      radius: 7 + Math.random() * 12,        // distance from center
      speed:  0.05 + Math.random() * 0.18,   // orbit speed
      tilt:   Math.random() * 1.2 - 0.6,     // y bobbing offset
      bobSpd: 0.5 + Math.random() * 1.5,
      scale:  0.9 + Math.random() * 1.4,
      hue:    Math.random(),
    };
    sp.scale.setScalar(sp.userData.scale);
    scene.add(sp);
    notes.push(sp);
  }

  // ---------- Central wireframe icosahedron + inner solid core ----------
  const coreGeom = new THREE.IcosahedronGeometry(3.4, 1);
  const coreMat  = new THREE.MeshBasicMaterial({ color: 0xe9d28b, wireframe: true,
                                                 transparent: true, opacity: 0.85 });
  const core = new THREE.Mesh(coreGeom, coreMat);
  scene.add(core);

  const innerCore = new THREE.Mesh(
    new THREE.IcosahedronGeometry(2.2, 0),
    new THREE.MeshBasicMaterial({ color: 0xffd28a, transparent: true, opacity: 0.20 })
  );
  scene.add(innerCore);

  // ---------- Three concentric torus rings ----------
  const rings = [];
  [{ r: 5.5, c: 0xff7a8a, thick: 0.06 },
   { r: 7.0, c: 0x7adcff, thick: 0.05 },
   { r: 8.5, c: 0xa78bff, thick: 0.045 }].forEach((cfg, i) => {
    const g = new THREE.TorusGeometry(cfg.r, cfg.thick, 10, 80);
    const m = new THREE.MeshBasicMaterial({ color: cfg.c, transparent: true, opacity: 0.65 });
    const ring = new THREE.Mesh(g, m);
    ring.rotation.x = Math.PI / 2.2 + (i * 0.18);
    scene.add(ring);
    rings.push(ring);
  });

  // ---------- Particle burst pool (rainbow sparks on kick) ----------
  const PARTICLE_POOL = 200;
  const partGeom = new THREE.BufferGeometry();
  const partPos = new Float32Array(PARTICLE_POOL * 3);
  const partCol = new Float32Array(PARTICLE_POOL * 3);
  for (let i = 0; i < PARTICLE_POOL; i++) {
    partPos[i*3] = partPos[i*3+1] = partPos[i*3+2] = -1000;  // off-screen
    partCol[i*3] = partCol[i*3+1] = partCol[i*3+2] = 1;
  }
  partGeom.setAttribute('position', new THREE.BufferAttribute(partPos, 3));
  partGeom.setAttribute('color', new THREE.BufferAttribute(partCol, 3));
  const particles = new THREE.Points(partGeom, new THREE.PointsMaterial({
    size: 0.45, vertexColors: true, transparent: true, opacity: 0.95,
    blending: THREE.AdditiveBlending, sizeAttenuation: true
  }));
  scene.add(particles);

  // particle state — velocity + life per slot
  const partVel = new Float32Array(PARTICLE_POOL * 3);
  const partLife = new Float32Array(PARTICLE_POOL);
  let partIdx = 0;
  function spawnBurst(count, intensity) {
    for (let n = 0; n < count; n++) {
      const i = partIdx;
      partIdx = (partIdx + 1) % PARTICLE_POOL;
      partPos[i*3] = (Math.random() - 0.5) * 0.4;
      partPos[i*3+1] = (Math.random() - 0.5) * 0.4;
      partPos[i*3+2] = (Math.random() - 0.5) * 0.4;
      const theta = Math.random() * Math.PI * 2;
      const phi   = Math.random() * Math.PI;
      const sp = (1.5 + Math.random() * 4) * intensity;
      partVel[i*3]   = Math.sin(phi) * Math.cos(theta) * sp;
      partVel[i*3+1] = Math.cos(phi) * sp;
      partVel[i*3+2] = Math.sin(phi) * Math.sin(theta) * sp;
      const c = new THREE.Color().setHSL(Math.random(), 0.85, 0.65);
      partCol[i*3] = c.r; partCol[i*3+1] = c.g; partCol[i*3+2] = c.b;
      partLife[i] = 1.0;
    }
    partGeom.attributes.position.needsUpdate = true;
    partGeom.attributes.color.needsUpdate = true;
  }

  // ---------- animation loop ----------
  const clock = new THREE.Clock();
  let cameraAngle = 0;
  let lastEnergy = 0;
  let kickCooldown = 0;

  function frame() {
    const dt = Math.min(0.05, clock.getDelta());
    const t = clock.getElapsedTime();

    // Pull live FFT data
    let bass = 0, mid = 0, high = 0;
    if (analyser && freqData) {
      analyser.getByteFrequencyData(freqData);
      const N = freqData.length;
      let s1 = 0, s2 = 0, s3 = 0;
      const b1 = Math.floor(N * 0.10), b2 = Math.floor(N * 0.40);
      for (let i = 0;  i < b1; i++) s1 += freqData[i];
      for (let i = b1; i < b2; i++) s2 += freqData[i];
      for (let i = b2; i < N;  i++) s3 += freqData[i];
      bass = (s1 / b1) / 255;
      mid  = (s2 / (b2 - b1)) / 255;
      high = (s3 / (N - b2)) / 255;
    }
    const energy = (bass + mid + high) / 3;

    // Kick detection — sharp upward energy spike triggers a particle burst
    const flash = Math.max(0, energy - lastEnergy);
    kickCooldown -= dt;
    if (flash > 0.10 && kickCooldown <= 0 && energy > 0.22) {
      spawnBurst(20, 1.0 + flash * 5);
      kickCooldown = 0.08;
    }
    lastEnergy = lastEnergy * 0.88 + energy * 0.12;

    // Core: pulses + hue shifts with bass
    core.scale.setScalar(core.scale.x + ((1 + bass * 0.65) - core.scale.x) * 0.20);
    core.rotation.x += dt * 0.20;
    core.rotation.y += dt * 0.26;
    coreMat.color.setHSL(0.13 - bass * 0.10, 0.7, 0.55 + bass * 0.30);
    coreMat.opacity = 0.55 + bass * 0.45;
    innerCore.material.opacity = 0.16 + flash * 1.6;
    innerCore.scale.setScalar(1 + bass * 0.42);

    // Rings: hue cycle slowly + scale + rotate, react to mid/high
    rings.forEach((ring, i) => {
      const reactive = i === 0 ? bass : i === 1 ? mid : high;
      const target = 1 + reactive * 0.30;
      ring.scale.setScalar(ring.scale.x + (target - ring.scale.x) * 0.16);
      ring.rotation.z += dt * (0.06 + 0.10 * (i + 1));
      // hue drift
      const h = ((t * 0.04) + i * 0.33) % 1;
      ring.material.color.setHSL(h, 0.7, 0.6);
      ring.material.opacity = 0.45 + reactive * 0.55;
    });

    // Notes: orbit + bob + pulse
    notes.forEach((sp, i) => {
      const u = sp.userData;
      u.angle += dt * u.speed * (1 + energy * 0.5);
      const r = u.radius * (1 + bass * 0.10);
      sp.position.x = Math.cos(u.angle) * r;
      sp.position.z = Math.sin(u.angle) * r;
      sp.position.y = u.tilt + Math.sin(t * u.bobSpd + i) * 1.5;
      // pulse scale + opacity with energy
      const s = u.scale * (1 + energy * 0.30);
      sp.scale.setScalar(s);
      sp.material.opacity = 0.65 + energy * 0.35;
    });

    // Stars
    stars.rotation.y += dt * 0.013;
    stars.material.opacity = 0.55 + energy * 0.35;

    // Particles — integrate velocity, fade life, write to geometry
    let needUpdate = false;
    for (let i = 0; i < PARTICLE_POOL; i++) {
      if (partLife[i] <= 0) continue;
      partLife[i] -= dt * 0.9;
      if (partLife[i] <= 0) {
        partPos[i*3] = partPos[i*3+1] = partPos[i*3+2] = -1000;
        needUpdate = true; continue;
      }
      partPos[i*3]   += partVel[i*3]   * dt;
      partPos[i*3+1] += partVel[i*3+1] * dt;
      partPos[i*3+2] += partVel[i*3+2] * dt;
      // gravity-like falloff
      partVel[i*3+1] -= dt * 0.6;
      // dim color over life
      const k = partLife[i];
      partCol[i*3]   *= 0.998;
      partCol[i*3+1] *= 0.998;
      partCol[i*3+2] *= 0.998;
      needUpdate = true;
    }
    if (needUpdate) {
      partGeom.attributes.position.needsUpdate = true;
      partGeom.attributes.color.needsUpdate = true;
    }

    // Camera slow orbit
    cameraAngle += dt * 0.05;
    camera.position.x = Math.sin(cameraAngle) * 28;
    camera.position.z = Math.cos(cameraAngle) * 28;
    camera.position.y = Math.sin(cameraAngle * 0.4) * 4;
    camera.lookAt(0, 0, 0);

    renderer.render(scene, camera);
    requestAnimationFrame(frame);
  }

  // Resize
  window.addEventListener('resize', () => {
    renderer.setSize(w(), h(), false);
    camera.aspect = w() / h();
    camera.updateProjectionMatrix();
  });

  // Activate brighter when audio plays
  function setActive(on) { canvas.classList.toggle('active', !!on); }
  player.addEventListener('play',  () => { attachAudio(); if (audioCtx && audioCtx.state === 'suspended') audioCtx.resume(); setActive(true); });
  player.addEventListener('pause', () => setActive(false));
  player.addEventListener('ended', () => setActive(false));

  requestAnimationFrame(frame);
})();
