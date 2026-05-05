// viz3d.js — Three.js scene reactive to whichever song is playing in the
// <audio id="player"> element. Gold-on-black aesthetic matching the UI:
//   - distant starfield (3000 stars, slow drift)
//   - central icosahedron that pulses with bass FFT energy
//   - three concentric ring meshes that ripple with mid/high frequencies
//   - camera slowly orbits the scene for depth
// Activates only while audio is playing; fades out on pause.

(function () {
  'use strict';

  if (typeof THREE === 'undefined') return;
  const canvas = document.getElementById('viz3d');
  const player = document.getElementById('player');
  if (!canvas || !player) return;

  const GOLD       = 0xe9d28b;
  const GOLD_MID   = 0xd6b06b;
  const GOLD_DARK  = 0xb48c46;
  const BG_FOG     = 0x080810;

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
  scene.fog = new THREE.FogExp2(BG_FOG, 0.018);

  const camera = new THREE.PerspectiveCamera(60, w() / h(), 0.1, 200);
  camera.position.set(0, 0, 26);

  // Starfield
  const starGeom = new THREE.BufferGeometry();
  const starCount = 3000;
  const starPos = new Float32Array(starCount * 3);
  for (let i = 0; i < starCount; i++) {
    starPos[i*3]   = (Math.random() - 0.5) * 200;
    starPos[i*3+1] = (Math.random() - 0.5) * 200;
    starPos[i*3+2] = (Math.random() - 0.5) * 200;
  }
  starGeom.setAttribute('position', new THREE.BufferAttribute(starPos, 3));
  const stars = new THREE.Points(starGeom, new THREE.PointsMaterial({
    color: GOLD, size: 0.18, sizeAttenuation: true, transparent: true, opacity: 0.55
  }));
  scene.add(stars);

  // Central icosahedron — wireframe, pulses with bass
  const coreGeom = new THREE.IcosahedronGeometry(3.2, 1);
  const coreMat = new THREE.MeshBasicMaterial({
    color: GOLD, wireframe: true, transparent: true, opacity: 0.85
  });
  const core = new THREE.Mesh(coreGeom, coreMat);
  scene.add(core);

  // Inner solid core that flashes with kicks
  const innerCore = new THREE.Mesh(
    new THREE.IcosahedronGeometry(2.0, 0),
    new THREE.MeshBasicMaterial({ color: GOLD_DARK, transparent: true, opacity: 0.18 })
  );
  scene.add(innerCore);

  // Three concentric rings — torus geometries that scale with mid/high FFT bands
  const rings = [];
  [{ r: 5.5, c: GOLD,      thick: 0.05 },
   { r: 7.0, c: GOLD_MID,  thick: 0.04 },
   { r: 8.5, c: GOLD_DARK, thick: 0.035 }].forEach((cfg, i) => {
    const g = new THREE.TorusGeometry(cfg.r, cfg.thick, 8, 64);
    const m = new THREE.MeshBasicMaterial({ color: cfg.c, transparent: true, opacity: 0.55 });
    const ring = new THREE.Mesh(g, m);
    ring.rotation.x = Math.PI / 2.2 + (i * 0.15);
    scene.add(ring);
    rings.push(ring);
  });

  // ---------- animation ----------
  const clock = new THREE.Clock();
  let cameraAngle = 0;
  let lastEnergy = 0;

  function frame() {
    const dt = clock.getDelta();
    const t = clock.getElapsedTime();

    // Pull live FFT data
    let bass = 0, mid = 0, high = 0;
    if (analyser && freqData) {
      analyser.getByteFrequencyData(freqData);
      const N = freqData.length;
      // average over 3 bands
      let s1 = 0, s2 = 0, s3 = 0;
      const b1 = Math.floor(N * 0.10), b2 = Math.floor(N * 0.40);
      for (let i = 0;       i < b1; i++) s1 += freqData[i];
      for (let i = b1;      i < b2; i++) s2 += freqData[i];
      for (let i = b2;      i < N;  i++) s3 += freqData[i];
      bass = (s1 / b1) / 255;
      mid  = (s2 / (b2 - b1)) / 255;
      high = (s3 / (N - b2)) / 255;
    }
    const energy = (bass + mid + high) / 3;

    // Core pulses with bass; inner flashes on kick (energy delta)
    const targetCoreScale = 1 + bass * 0.55;
    core.scale.setScalar(core.scale.x + (targetCoreScale - core.scale.x) * 0.18);
    core.rotation.x += dt * 0.18;
    core.rotation.y += dt * 0.22;
    coreMat.opacity = 0.55 + bass * 0.45;

    const flash = Math.max(0, energy - lastEnergy);
    innerCore.material.opacity = 0.12 + flash * 1.5;
    innerCore.scale.setScalar(1 + bass * 0.35);
    innerCore.rotation.x -= dt * 0.10;
    lastEnergy = lastEnergy * 0.92 + energy * 0.08;

    // Rings expand and rotate with mid/high
    rings.forEach((ring, i) => {
      const reactive = i === 0 ? bass : i === 1 ? mid : high;
      const baseR = [5.5, 7.0, 8.5][i];
      const target = 1 + reactive * 0.25;
      ring.scale.setScalar(ring.scale.x + (target - ring.scale.x) * 0.15);
      ring.rotation.z += dt * (0.05 + 0.08 * (i + 1));
      ring.material.opacity = 0.35 + reactive * 0.55;
    });

    // Stars — slow drift toward camera, slight twinkle on energy
    stars.rotation.y += dt * 0.012;
    stars.material.opacity = 0.45 + energy * 0.35;

    // Camera slow orbit
    cameraAngle += dt * 0.06;
    camera.position.x = Math.sin(cameraAngle) * 26;
    camera.position.z = Math.cos(cameraAngle) * 26;
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

  // Activate/deactivate the canvas opacity based on play state
  function setActive(on) { canvas.classList.toggle('active', !!on); }
  player.addEventListener('play',  () => { attachAudio(); if (audioCtx && audioCtx.state === 'suspended') audioCtx.resume(); setActive(true); });
  player.addEventListener('pause', () => setActive(false));
  player.addEventListener('ended', () => setActive(false));

  requestAnimationFrame(frame);
})();
