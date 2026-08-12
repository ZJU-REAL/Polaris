/* shared behaviors: scroll reveal, pill-morph nav, back-to-top */
(function () {
  var obs = new IntersectionObserver(function (entries) {
    entries.forEach(function (e) {
      if (e.isIntersecting) { e.target.classList.add('in'); obs.unobserve(e.target); }
    });
  }, { threshold: 0.15 });
  document.querySelectorAll('.rv').forEach(function (el) { obs.observe(el); });

  var navBar = document.getElementById('nav');
  var btnTop = document.getElementById('topbtn');
  window.addEventListener('scroll', function () {
    var y = window.scrollY;
    if (navBar) navBar.classList.toggle('float', y > 40);
    if (btnTop) btnTop.classList.toggle('show', y > 700);
  }, { passive: true });
  window.__siteJs = 1;
})();
