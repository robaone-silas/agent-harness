(() => {
  const root = document.documentElement;
  const header = document.querySelector('[data-header]');
  const progress = document.querySelector('.page-progress span');
  const reduceMotion = window.matchMedia('(prefers-reduced-motion: reduce)');
  let ticking = false;

  const clamp = (value, min = 0, max = 1) => Math.min(max, Math.max(min, value));

  function renderScroll() {
    const y = window.scrollY;
    const docRange = Math.max(1, document.documentElement.scrollHeight - window.innerHeight);
    const heroProgress = reduceMotion.matches ? 0 : clamp(y / Math.min(760, window.innerHeight * 0.9));

    root.style.setProperty('--hero-progress', heroProgress.toFixed(4));
    progress.style.transform = `scaleX(${clamp(y / docRange).toFixed(4)})`;
    header.classList.toggle('is-scrolled', y > 24);
    ticking = false;
  }

  function requestRender() {
    if (!ticking) {
      ticking = true;
      window.requestAnimationFrame(renderScroll);
    }
  }

  const revealObserver = new IntersectionObserver(
    (entries) => {
      entries.forEach((entry) => {
        if (entry.isIntersecting) {
          entry.target.classList.add('is-visible');
          revealObserver.unobserve(entry.target);
        }
      });
    },
    { threshold: 0.14, rootMargin: '0px 0px -7% 0px' }
  );

  document.querySelectorAll('[data-reveal]').forEach((element) => revealObserver.observe(element));

  const navToggle = document.querySelector('[data-nav-toggle]');
  const navShell = document.querySelector('.nav-shell');

  function setNav(open) {
    if (!navToggle || !navShell) return;
    navShell.classList.toggle('nav-open', open);
    navToggle.setAttribute('aria-expanded', String(open));
    navToggle.setAttribute('aria-label', open ? 'Close menu' : 'Open menu');
  }

  navToggle?.addEventListener('click', () => {
    setNav(navToggle.getAttribute('aria-expanded') !== 'true');
  });

  // Close on link choice, on Escape, and on any click outside the shell.
  navShell?.querySelectorAll('.nav-links a').forEach((link) => link.addEventListener('click', () => setNav(false)));
  document.addEventListener('keydown', (event) => {
    if (event.key !== 'Escape' || navToggle?.getAttribute('aria-expanded') !== 'true') return;
    setNav(false);
    navToggle.focus();
  });
  document.addEventListener('click', (event) => {
    if (navShell && !navShell.contains(event.target)) setNav(false);
  });

  document.querySelectorAll('.faq details').forEach((item) => {
    item.addEventListener('toggle', () => {
      if (!item.open) return;
      document.querySelectorAll('.faq details[open]').forEach((other) => {
        if (other !== item) other.open = false;
      });
    });
  });

  document.querySelectorAll('.choice-row button').forEach((button) => {
    button.addEventListener('click', () => {
      document.querySelectorAll('.choice-row button').forEach((choice) => choice.removeAttribute('aria-pressed'));
      button.setAttribute('aria-pressed', 'true');
    });
  });

  // Optional generated-motion hook for template consumers. Keep exact UI outside
  // the video and skip the download for reduced-motion or data-saver users.
  document.querySelectorAll('video[data-responsive-motion]').forEach((video) => {
    const saveData = navigator.connection?.saveData === true;
    if (reduceMotion.matches || saveData) return;
    const source = document.createElement('source');
    const mobile = window.matchMedia('(max-width: 720px)').matches;
    source.src = mobile ? video.dataset.mobileSrc : video.dataset.desktopSrc;
    source.type = 'video/mp4';
    video.append(source);
    video.play().catch(() => {});
  });

  window.addEventListener('scroll', requestRender, { passive: true });
  window.addEventListener('resize', requestRender, { passive: true });
  reduceMotion.addEventListener?.('change', requestRender);
  renderScroll();
})();
