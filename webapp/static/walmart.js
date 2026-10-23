/* Walmart page front-end.
   Self-contained on purpose: it shares no state or functions with app.js. */

const $ = (id) => document.getElementById(id);

const setup = $('w-setup');
const progress = $('w-progress');
const results = $('w-results');
const challenge = $('w-challenge');

let startedAt = 0;
let peakReviews = 0;
let challengeSince = 0;
let challengeTimer = null;

/* ---------- helpers ---------- */

function mmss(sec) {
  if (!sec || sec < 0) return '0:00';
  const m = Math.floor(sec / 60), s = sec % 60;
  if (m < 60) return `${m}:${String(s).padStart(2, '0')}`;
  return `${Math.floor(m / 60)}h ${m % 60}m`;
}

function esc(value) {
  return String(value == null ? '' : value)
    .replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('>', '&gt;')
    .replaceAll('"', '&quot;').replaceAll("'", '&#39;');
}

function showError(msg) {
  const el = $('w-error');
  el.textContent = msg;
  el.hidden = false;
}

function log(line) {
  const el = $('w-log');
  el.textContent += line + '\n';
  el.scrollTop = el.scrollHeight;
}

function resetStart() {
  $('w-start').disabled = false;
  $('w-start').textContent = 'Start download';
}

/* Walmart item IDs are numeric; Amazon ASINs are deliberately not accepted. */
function extractItemIds(raw) {
  const out = [];
  const urlRe = /\/(?:ip|reviews\/product)\/(?:[^/?#]+\/)?(\d{6,14})(?:[/?#]|$)/i;
  for (const chunk of (raw || '').trim().split(/[\s,;]+/)) {
    if (!chunk) continue;
    const m = chunk.match(urlRe);
    const candidate = (m ? m[1] : chunk.replace(/^\/+|\/+$/g, ''));
    if (/^\d{6,14}$/.test(candidate) && !out.includes(candidate)) out.push(candidate);
  }
  return out;
}

function refreshCount() {
  const n = extractItemIds($('w-items').value).length;
  $('w-count').textContent =
    n === 0 ? 'no products yet' : n === 1 ? '1 product' : `${n} products`;
}

$('w-items').addEventListener('input', refreshCount);

function selectedStars() {
  return [...document.querySelectorAll('#w-stars input:checked')].map((c) => +c.value);
}

document.querySelectorAll('[data-w-pick]').forEach((btn) => {
  btn.addEventListener('click', () => {
    const want = {
      all: [5, 4, 3, 2, 1],
      positive: [5, 4],
      critical: [3, 2, 1],
      none: [],
    }[btn.dataset.wPick];
    document.querySelectorAll('#w-stars input').forEach((c) => {
      c.checked = want.includes(+c.value);
    });
  });
});

/* ---------- challenge gate ---------- */

const STAGE_TEXT = {
  starting: 'Getting ready...',
  product_start: 'Downloading reviews',
  scraping: 'Downloading reviews',
  challenge: 'Waiting for the Walmart check',
  challenge_waiting: 'Waiting for the Walmart check',
  challenge_done: 'Check cleared',
  exporting: 'Saving your files',
  product_done: 'Downloading reviews',
  done: 'Finished',
  error: 'Something went wrong',
};

function showChallenge() {
  challengeSince = Date.now();
  challenge.hidden = false;
  $('w-challenge-cancel').disabled = false;
  $('w-challenge-cancel').textContent = 'Cancel';
  progress.hidden = true;
  clearInterval(challengeTimer);
  $('w-challenge-timer').textContent = 'waiting 0:00 - take your time';
  challengeTimer = setInterval(() => {
    const s = Math.floor((Date.now() - challengeSince) / 1000);
    $('w-challenge-timer').textContent = `waiting ${mmss(s)} - take your time`;
  }, 1000);
}

function hideChallenge() {
  clearInterval(challengeTimer);
  challengeTimer = null;
  challenge.hidden = true;
}

$('w-challenge-cancel').addEventListener('click', () => {
  $('w-challenge-cancel').disabled = true;
  $('w-challenge-cancel').textContent = 'Cancelling...';
  fetch('/walmart/api/stop', { method: 'POST' });
});

/* ---------- start / stop ---------- */

$('w-start').addEventListener('click', async () => {
  const items = extractItemIds($('w-items').value);
  if (!items.length) {
    showError('No products found. Paste a Walmart item ID (numbers only) or a product link.');
    return;
  }
  const stars = selectedStars();
  if (!stars.length) {
    showError('Pick at least one star rating to download.');
    return;
  }
  $('w-error').hidden = true;

  const body = {
    items: items.join('\n'),
    stars,
    max_pages: +$('w-max-pages').value,
    formats: ['csv', 'json', 'xlsx'],
  };

  buildQueue(items);
  $('w-start').disabled = true;
  $('w-start').textContent = 'Starting...';

  try {
    const r = await fetch('/walmart/api/start', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    const d = await r.json();
    if (!d.ok) {
      showError(d.error || 'Could not start.');
      resetStart();
      return;
    }
    startedAt = Date.now();
    peakReviews = 0;
    $('w-log').textContent = '';
    $('w-fatal').hidden = true;
    $('w-reset').hidden = true;
    setup.hidden = true;
    results.hidden = true;
    challenge.hidden = true;
    progress.hidden = false;
    $('w-stop').disabled = false;
    $('w-stop').textContent = 'Stop';
    $('w-challenge-cancel').disabled = false;
    $('w-challenge-cancel').textContent = 'Cancel';
  } catch (e) {
    showError('Could not reach the app. Is the terminal window still open?');
    resetStart();
  }
});

$('w-stop').addEventListener('click', () => {
  $('w-stop').disabled = true;
  $('w-stop').textContent = 'Stopping...';
  fetch('/walmart/api/stop', { method: 'POST' });
});

$('w-again').addEventListener('click', () => {
  results.hidden = true;
  progress.hidden = true;
  setup.hidden = false;
  resetStart();
});

$('w-reset').addEventListener('click', () => {
  progress.hidden = true;
  challenge.hidden = true;
  results.hidden = true;
  setup.hidden = false;
  resetStart();
});

/* ---------- queue ---------- */

function buildQueue(items) {
  $('w-queue').innerHTML = items
    .map((id, i) => `<li class="q-item" data-item="${esc(id)}">
        <span class="q-mark" id="wqm-${i}">&#8226;</span>
        <span class="q-asin">${esc(id)}</span>
        <span class="q-note" id="wqn-${i}"></span>
      </li>`)
    .join('');
}

function markQueue(index, state, note) {
  const mark = $(`wqm-${index}`), noteEl = $(`wqn-${index}`);
  if (!mark) return;
  const li = mark.closest('.q-item');
  li.classList.remove('running', 'done', 'failed');
  li.classList.add(state);
  mark.textContent = state === 'done' ? '\u2713' : state === 'failed' ? '!' : '\u203a';
  if (note !== undefined) noteEl.textContent = note;
}

/* ---------- live progress ---------- */

const es = new EventSource('/walmart/api/events');

es.onmessage = (ev) => {
  let d;
  try { d = JSON.parse(ev.data); } catch { return; }

  if (d.stage === 'challenge') showChallenge();
  if (d.stage === 'challenge_done' || d.stage === 'product_start' || d.stage === 'scraping') {
    if (!challenge.hidden) {
      hideChallenge();
      progress.hidden = false;
      $('w-challenge-cancel').disabled = false;
      $('w-challenge-cancel').textContent = 'Cancel';
    }
  }
  if (d.stage === 'error' && !challenge.hidden) hideChallenge();

  if (d.stage && STAGE_TEXT[d.stage]) $('w-stage').textContent = STAGE_TEXT[d.stage];
  if (d.message) {
    if (d.stage === 'log') log(d.message);
    else { $('w-message').textContent = d.message; log(d.message); }
  }

  if (d.stage === 'product_start') {
    peakReviews = 0;
    $('w-s-reviews').textContent = '0';
    $('w-s-total').textContent = '\u2014';
    $('w-s-eta').textContent = '\u2014';
    markQueue(d.product_index - 1, 'running', 'downloading...');
    $('w-product-label').hidden = false;
    $('w-product-wrap').hidden = false;
    $('w-product-name').textContent = `Walmart ${d.item_id || ''}`;
    $('w-current').hidden = false;
    $('w-current-label').textContent = 'Starting...';
  }

  if (d.stage === 'product_done') {
    const r = d.result || {};
    markQueue(
      d.product_index - 1,
      r.ok ? 'done' : 'failed',
      r.ok ? `${(r.reviews || 0).toLocaleString()} written reviews` : 'problem - see log',
    );
  }

  if (d.product_index && d.product_count) {
    $('w-batch-count').textContent = `product ${d.product_index} of ${d.product_count}`;
  }
  if (typeof d.reviews_all === 'number') {
    const shown = d.reviews_all + (d.stage === 'product_done' ? 0 : peakReviews);
    $('w-s-all').textContent = shown.toLocaleString();
  }

  if (typeof d.reviews === 'number') {
    peakReviews = Math.max(peakReviews, d.reviews);
    $('w-s-reviews').textContent = peakReviews.toLocaleString();
  }
  if (d.reviews_total) $('w-s-total').textContent = d.reviews_total.toLocaleString();

  if (typeof d.percent === 'number') {
    $('w-bar').style.width = Math.max(2, d.percent) + '%';
    $('w-s-pct').textContent = d.percent + '%';
  }
  if (typeof d.product_percent === 'number') {
    $('w-product-bar').style.width = Math.max(2, d.product_percent) + '%';
    $('w-product-pct').textContent = d.product_percent + '%';
  }

  if (typeof d.page === 'number' && d.page > 0) {
    $('w-current').hidden = false;
    $('w-current-label').textContent =
      d.total_pages ? `page ${d.page} of ${d.total_pages}` : `page ${d.page}`;
  }

  if (typeof d.elapsed === 'number') {
    $('w-s-elapsed').textContent = mmss(d.elapsed);
    if (d.percent > 4 && d.percent < 100) {
      const total = d.elapsed / (d.percent / 100);
      $('w-s-eta').textContent = mmss(Math.round(total - d.elapsed));
    }
  }

  if (d.stage === 'done') finish(d);
  if (d.stage === 'error') {
    $('w-current').hidden = true;
    $('w-message').textContent = d.message || 'Something went wrong.';
    $('w-fatal').textContent = d.message || 'Something went wrong.';
    $('w-fatal').hidden = false;
    $('w-reset').hidden = false;
    resetStart();
  }
};

es.onerror = () => {
  $('w-stage').textContent = 'Connection lost - is the app still running?';
};

/* ---------- results ---------- */

function dlLinks(files) {
  return (files || [])
    .map((f) => {
      const kind = f.split('.').pop().toUpperCase();
      return `<a class="dl" href="/walmart/api/download/${encodeURIComponent(f)}">${kind}</a>`;
    })
    .join(' ');
}

function aggregateRatings(rows) {
  const ratings = { 5: 0, 4: 0, 3: 0, 2: 0, 1: 0 };
  const reviews = { 5: 0, 4: 0, 3: 0, 2: 0, 1: 0 };
  rows.forEach((r) => {
    const s = r.summary || {};
    const dr = s.rating_distribution_ratings || {};
    const dv = s.rating_distribution_reviews || {};
    for (const star of [5, 4, 3, 2, 1]) {
      ratings[star] += +(dr[String(star)] || 0);
      reviews[star] += +(dv[String(star)] || 0);
    }
  });
  return { ratings, reviews };
}

function renderRatings(rows) {
  const { ratings, reviews } = aggregateRatings(rows);
  const totalRatings = Object.values(ratings).reduce((a, b) => a + b, 0);
  $('w-ratings').innerHTML = totalRatings
    ? [5, 4, 3, 2, 1].map((star) => `
        <div class="rating-row">
          <span class="stars-label">${star} star</span>
          <span class="rating-nums">${ratings[star].toLocaleString()} ratings &middot; ${reviews[star].toLocaleString()} written</span>
        </div>`).join('')
    : '<p class="muted">No rating summary was reported by Walmart.</p>';
}

function finish(d) {
  hideChallenge();
  $('w-current').hidden = true;
  $('w-bar').style.width = '100%';
  $('w-product-bar').style.width = '100%';
  $('w-s-eta').textContent = '\u2014';

  const rows = d.results || [];
  const ok = rows.filter((r) => r.ok);
  const totalReviews = rows.reduce((n, r) => n + (r.reviews || 0), 0);
  const ratingsTotal = ok.reduce(
    (n, r) => n + (((r.summary || {}).ratings_reported_by_walmart) || 0), 0);

  let avg = null;
  let avgWeight = 0;
  let avgSum = 0;
  ok.forEach((r) => {
    const s = r.summary || {};
    const a = s.average_rating_reported;
    const w = s.ratings_reported_by_walmart || 0;
    if (typeof a === 'number' && w > 0) {
      avgSum += a * w;
      avgWeight += w;
    }
  });
  if (avgWeight) avg = (avgSum / avgWeight).toFixed(2);

  const cells = [
    ['written reviews', totalReviews.toLocaleString()],
    ['items done', `${ok.length}/${rows.length}`],
  ];
  if (ratingsTotal) cells.push(['ratings reported', ratingsTotal.toLocaleString()]);
  if (avg) cells.push(['average rating', avg]);
  $('w-result-summary').innerHTML = cells
    .map(([label, num]) => `<div class="stat"><span class="num">${esc(num)}</span><span class="lbl">${esc(label)}</span></div>`)
    .join('');

  renderRatings(rows);

  const fails = rows.filter((r) => !r.ok);
  const advice = $('w-fail-advice');
  if (fails.length) {
    advice.hidden = false;
    advice.innerHTML = `<strong>${fails.length} item(s) did not download:</strong>
      <ul>${fails.map((r) => `<li>${esc(r.item_id || '')}: ${esc(r.error || 'unknown problem')}</li>`).join('')}</ul>`;
  } else {
    advice.hidden = true;
  }

  const hasCombined = (d.files || []).length > 0;
  $('w-combined-head').hidden = !hasCombined;
  $('w-combined-downloads').innerHTML = hasCombined ? dlLinks(d.files) : '';

  $('w-results-rows').innerHTML = rows
    .map((r) => {
      const s = r.summary || {};
      const status = r.ok
        ? `${(r.reviews || 0).toLocaleString()} written reviews`
        : `<span class="bad">problem</span> <span class="muted">${esc(r.error || '')}</span>`;
      const cov = s.coverage_pct != null
        ? `<span class="muted">${s.coverage_pct}% of Walmart's written reviews</span>` : '';
      const ratings = s.ratings_reported_by_walmart
        ? `<span class="muted">${s.ratings_reported_by_walmart.toLocaleString()} ratings total</span>` : '';
      return `<tr>
          <td class="mono">${esc(r.item_id || '')}</td>
          <td>${status} ${cov} ${ratings}</td>
          <td class="right">${dlLinks(r.files)}</td>
        </tr>`;
    })
    .join('');

  const truncated = rows.filter((r) => (r.summary || {}).page_limit_reached);
  const note = $('w-cap-note');
  if (truncated.length) {
    note.hidden = false;
    note.textContent =
      'Note: the page limit was reached for at least one item. Those counts may be a floor, '
      + 'not the full total. Increase "Maximum pages" and run again to continue.';
  } else {
    note.hidden = true;
  }

  results.hidden = false;
  progress.hidden = true;
  $('w-stop').disabled = false;
  $('w-stop').textContent = 'Stop';
  resetStart();
  $('w-reset').hidden = true;
}
