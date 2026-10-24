/* Front-end for the review downloader.
   Talks to the local Flask server; progress arrives as Server-Sent Events. */

const $ = (id) => document.getElementById(id);

const setup = $('setup'), progress = $('progress'), results = $('results');
let startedAt = 0;
let peakReviews = 0;

/* ---------- helpers ---------- */

function mmss(sec) {
  if (!sec || sec < 0) return '0:00';
  const m = Math.floor(sec / 60), s = sec % 60;
  if (m < 60) return `${m}:${String(s).padStart(2, '0')}`;
  return `${Math.floor(m / 60)}h ${m % 60}m`;
}

function showError(msg) {
  const el = $('form-error');
  el.textContent = msg;
  el.hidden = false;
}

function log(line) {
  const el = $('log');
  el.textContent += line + '\n';
  el.scrollTop = el.scrollHeight;
}

/* Pull ASINs out of whatever was pasted: one per line, comma separated, bare
   codes or full product links. Mirrors parse_asins() on the server. */
function extractAsins(raw) {
  const out = [];
  for (const chunk of (raw || '').trim().split(/[\s,;]+/)) {
    if (!chunk) continue;
    const m = chunk.match(/\/(?:dp|gp\/product|product-reviews)\/([A-Z0-9]{10})/i);
    const candidate = (m ? m[1] : chunk).toUpperCase();
    if (/^[A-Z0-9]{10}$/.test(candidate) && !out.includes(candidate)) out.push(candidate);
  }
  return out;
}

function refreshCount() {
  const n = extractAsins($('asins').value).length;
  $('asin-count').textContent =
    n === 0 ? 'no products yet' : n === 1 ? '1 product' : `${n} products`;
}

$('asins').addEventListener('input', refreshCount);

function selectedStars() {
  return [...document.querySelectorAll('#stars input:checked')].map((c) => +c.value);
}

/* ---------- star quick-picks ---------- */

document.querySelectorAll('[data-pick]').forEach((btn) => {
  btn.addEventListener('click', () => {
    const pick = btn.dataset.pick;
    const want = { all: [5, 4, 3, 2, 1], positive: [5, 4], critical: [3, 2, 1], none: [] }[pick];
    document.querySelectorAll('#stars input').forEach((c) => {
      c.checked = want.includes(+c.value);
    });
  });
});

/* ---------- marketplace detection ---------- */

/* When exactly one product is listed we can set the marketplace immediately,
   which is the common case. Mixed lists use the auto-detect checkbox instead,
   because probing every product here would block the form for too long. */
$('asins').addEventListener('blur', async () => {
  const asins = extractAsins($('asins').value);
  const note = $('detect-note');
  if (asins.length !== 1 || $('auto-detect').checked) { note.hidden = true; return; }

  note.hidden = false;
  note.className = 'note';
  note.textContent = 'Checking which marketplace this product is on…';
  try {
    const r = await fetch('/api/detect?asin=' + encodeURIComponent(asins[0]));
    const d = await r.json();
    if (d.ok) {
      $('domain').value = d.domain;
      note.className = 'note ok';
      note.textContent = `Found on ${d.domain} — marketplace set for you.`;
    } else {
      note.className = 'note warn';
      note.textContent = d.error;
    }
  } catch {
    note.hidden = true;
  }
});

/* ---------- start / stop ---------- */

$('start').addEventListener('click', async () => {
  const asins = extractAsins($('asins').value);
  if (!asins.length) {
    showError('No products found. An ASIN is 10 characters, e.g. B08H2Y91P1 — one per line.');
    return;
  }

  const stars = selectedStars();
  if (!stars.length) { showError('Pick at least one star rating to download.'); return; }
  $('form-error').hidden = true;

  const body = {
    asins: asins.join('\n'),
    domain: $('domain').value,
    auto_detect: $('auto-detect').checked,
    stars,
    coverage: $('coverage').value,
    formats: ['csv', 'json', 'xlsx'],
  };
  buildQueue(asins);

  $('start').disabled = true;
  $('start').textContent = 'Starting…';

  try {
    const r = await fetch('/api/start', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    const d = await r.json();
    if (!d.ok) { showError(d.error || 'Could not start.'); resetStart(); return; }

    startedAt = Date.now();
    peakReviews = 0;
    $('log').textContent = '';
    setup.hidden = true;
    results.hidden = true;
    progress.hidden = false;
  } catch (e) {
    showError('Could not reach the app. Is the black window still open?');
    resetStart();
  }
});

function resetStart() {
  $('start').disabled = false;
  $('start').textContent = 'Start download';
}

$('stop').addEventListener('click', () => {
  $('stop').disabled = true;
  $('stop').textContent = 'Stopping…';
  fetch('/api/stop', { method: 'POST' });
});

$('again').addEventListener('click', () => {
  results.hidden = true;
  progress.hidden = true;
  setup.hidden = false;
  resetStart();
});

/* ---------- live progress ---------- */

/* ---------- sign-in gate ---------- */

const signin = $('signin');
let signinSince = 0;
let signinTimer = null;

function showSignin() {
  signinSince = Date.now();
  signin.hidden = false;
  progress.hidden = true;
  clearInterval(signinTimer);
  signinTimer = setInterval(() => {
    const s = Math.floor((Date.now() - signinSince) / 1000);
    $('signin-timer').textContent = `waiting ${mmss(s)} — take your time`;
  }, 1000);
}

function hideSignin() {
  clearInterval(signinTimer);
  signinTimer = null;
  signin.hidden = true;
  progress.hidden = false;
}

$('signin-cancel').addEventListener('click', () => {
  $('signin-cancel').disabled = true;
  $('signin-cancel').textContent = 'Cancelling…';
  fetch('/api/stop', { method: 'POST' });
});

const STAGE_TEXT = {
  starting: 'Getting ready…',
  signin: 'Waiting for you to sign in',
  signin_waiting: 'Waiting for you to sign in',
  signin_done: 'Signed in',
  scraping: 'Downloading reviews',
  product_start: 'Downloading reviews',
  product_done: 'Downloading reviews',
  exporting: 'Saving your files',
  done: 'Finished',
  error: 'Something went wrong',
};

/* The queue list gives the batch a visible shape: what is done, what is
   running, and what is still waiting. */
function buildQueue(asins) {
  $('queue').innerHTML = asins
    .map((a, i) => `<li class="q-item" data-asin="${a}">
        <span class="q-mark" id="qm-${i}">•</span>
        <span class="q-asin">${a}</span>
        <span class="q-note" id="qn-${i}"></span>
      </li>`)
    .join('');
}

function markQueue(index, state, note) {
  const mark = $(`qm-${index}`), noteEl = $(`qn-${index}`);
  if (!mark) return;
  const li = mark.closest('.q-item');
  li.classList.remove('running', 'done', 'failed');
  li.classList.add(state);
  mark.textContent = state === 'done' ? '✓' : state === 'failed' ? '!' : '›';
  if (note !== undefined) noteEl.textContent = note;
}

const es = new EventSource('/api/events');

es.onmessage = (ev) => {
  let d;
  try { d = JSON.parse(ev.data); } catch { return; }

  // The sign-in gate takes over the whole screen: nothing is downloading, and
  // showing a stalled progress bar behind it just looks broken.
  if (d.stage === 'signin') showSignin();
  if (d.stage === 'signin_done' || d.stage === 'product_start') {
    if (!signin.hidden) hideSignin();
  }
  if (d.stage === 'error' && !signin.hidden) hideSignin();

  if (d.stage && STAGE_TEXT[d.stage]) $('stage').textContent = STAGE_TEXT[d.stage];
  if (d.message) {
    if (d.stage === 'log') log(d.message);
    else { $('message').textContent = d.message; log(d.message); }
  }

  /* A new product resets the per-product counters; the batch total keeps
     climbing so the headline number never appears to go backwards. */
  if (d.stage === 'product_start') {
    peakReviews = 0;
    $('s-reviews').textContent = '0';
    $('s-total').textContent = '—';
    markQueue(d.product_index - 1, 'running', 'downloading…');
    $('product-bar-label').hidden = false;
    $('product-bar-wrap').hidden = false;
    $('product-name').textContent = `${d.asin} (${d.domain})`;
  }

  if (d.stage === 'product_done') {
    const r = d.result || {};
    markQueue(
      d.product_index - 1,
      r.ok ? 'done' : 'failed',
      r.ok ? `${(r.reviews || 0).toLocaleString()} reviews` : 'problem — see log',
    );
  }

  if (d.product_index && d.product_count) {
    $('batch-count').textContent = `product ${d.product_index} of ${d.product_count}`;
  }
  if (typeof d.total_reviews_all === 'number') {
    const shown = d.total_reviews_all + (d.stage === 'product_done' ? 0 : peakReviews);
    $('s-all').textContent = shown.toLocaleString();
  }

  if (typeof d.reviews === 'number') {
    peakReviews = Math.max(peakReviews, d.reviews);
    $('s-reviews').textContent = peakReviews.toLocaleString();
  }
  if (d.reviews_total) $('s-total').textContent = d.reviews_total.toLocaleString();

  if (typeof d.percent === 'number') {
    $('bar').style.width = Math.max(2, d.percent) + '%';
    $('s-pct').textContent = d.percent + '%';
  }
  if (typeof d.product_percent === 'number') {
    $('product-bar').style.width = Math.max(2, d.product_percent) + '%';
    $('product-pct').textContent = d.product_percent + '%';
  }

  if (d.segment_label) {
    $('current').hidden = false;
    $('current-label').textContent = 'Now: ' + d.segment_label;
  }

  if (typeof d.elapsed === 'number') {
    $('s-elapsed').textContent = mmss(d.elapsed);
    // Straight-line estimate from progress so far. Rough by nature, so it is
    // labelled "est." and hidden until there is enough signal to mean anything.
    if (d.percent > 4 && d.percent < 100) {
      const total = d.elapsed / (d.percent / 100);
      $('s-eta').textContent = mmss(Math.round(total - d.elapsed));
    }
  }

  if (d.stage === 'done') finish(d);
  if (d.stage === 'error') {
    $('current').hidden = true;
    $('message').textContent = d.message;
  }
};

es.onerror = () => { $('stage').textContent = 'Connection lost — is the app still running?'; };

/* ---------- results ---------- */

function dlLinks(files) {
  return (files || [])
    .map((f) => {
      const kind = f.split('.').pop().toUpperCase();
      return `<a class="dl" href="/api/download/${encodeURIComponent(f)}">${kind}</a>`;
    })
    .join(' ');
}

/* Upload sits beside the download links.  The browser calls this local Flask
   route, which forwards to the Java API; the token stays server-side. */
function uploadBtn(asin, domain, combined = false) {
  const label = combined ? 'Upload combined' : 'Upload';
  return `<button class="upload-btn" type="button"
    data-upload-asin="${asin || ''}"
    data-upload-domain="${domain || ''}"
    data-upload-combined="${combined ? '1' : '0'}">${label}</button>`;
}

function showUploadNote(message, ok) {
  const el = $('upload-note');
  if (!el) return;
  el.hidden = false;
  el.textContent = message;
  el.classList.toggle('ok', !!ok);
  el.classList.toggle('warn', !ok);
}

document.addEventListener('click', async (event) => {
  const btn = event.target.closest('.upload-btn');
  if (!btn) return;
  const original = btn.textContent;
  const asin = btn.dataset.uploadAsin || '';
  const domain = btn.dataset.uploadDomain || '';
  const combined = btn.dataset.uploadCombined === '1';

  btn.disabled = true;
  btn.classList.remove('done', 'failed');
  btn.textContent = 'Uploading...';
  showUploadNote('Uploading to the database...', true);

  try {
    const response = await fetch('/api/upload', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ asin, domain, combined }),
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok || !data.ok) {
      throw new Error(data.error || `Upload failed (HTTP ${response.status})`);
    }
    btn.textContent = 'Uploaded';
    btn.classList.add('done');
    showUploadNote(
      `Uploaded ${Number(data.uploaded || 0).toLocaleString()} reviews in ${data.batches} batch(es).`,
      true,
    );
  } catch (err) {
    btn.disabled = false;
    btn.textContent = original;
    btn.classList.add('failed');
    showUploadNote(err.message || 'Upload failed.', false);
  }
});

function finish(d) {
  $('current').hidden = true;
  $('upload-note').hidden = true;
  $('bar').style.width = '100%';
  $('product-bar').style.width = '100%';
  $('s-eta').textContent = '—';

  const rows = d.results || [];
  const ok = rows.filter((r) => r.ok);
  const totalReviews = rows.reduce((n, r) => n + (r.reviews || 0), 0);
  const ratings = ok
    .map((r) => r.summary && r.summary.average_rating)
    .filter((x) => typeof x === 'number');

  const cells = [
    ['reviews saved', totalReviews.toLocaleString()],
    ['products done', `${ok.length}/${rows.length}`],
  ];
  if (ratings.length) {
    const avg = ratings.reduce((a, b) => a + b, 0) / ratings.length;
    cells.push(['average rating', avg.toFixed(2)]);
  }
  cells.push([
    'verified',
    ok.reduce((n, r) => n + ((r.summary && r.summary.verified_purchases) || 0), 0).toLocaleString(),
  ]);
  $('result-summary').innerHTML = cells
    .map(([lbl, num]) => `<div class="stat"><span class="num">${num}</span><span class="lbl">${lbl}</span></div>`)
    .join('');

  // Group the failures by cause. A list of 200 identical "problem" badges
  // tells you nothing; "190 not on this marketplace" tells you what to change.
  const fails = rows.filter((r) => !r.ok);
  const causes = { marketplace: 0, signin: 0, none: 0, other: 0 };
  fails.forEach((r) => {
    const e = (r.error || '').toLowerCase();
    if (e.includes('no such product')) causes.marketplace++;
    else if (e.includes('sign in') || e.includes('signed in')) causes.signin++;
    else if (e.includes('no reviews found')) causes.none++;
    else causes.other++;
  });

  const advice = [];
  if (causes.marketplace) {
    advice.push(`<li><strong>${causes.marketplace}</strong> were not on the marketplace you
      chose. ASINs only work on the Amazon site they belong to. Re-run those with
      <em>“Work out the marketplace for each product automatically”</em> ticked.</li>`);
  }
  if (causes.signin) {
    advice.push(`<li><strong>${causes.signin}</strong> needed a sign-in for their
      marketplace. Amazon treats each country's site as a separate login.</li>`);
  }
  if (causes.none) {
    advice.push(`<li><strong>${causes.none}</strong> genuinely have no reviews yet.</li>`);
  }
  if (causes.other) {
    advice.push(`<li><strong>${causes.other}</strong> failed for other reasons — see the
      list below.</li>`);
  }
  const adviceEl = $('fail-advice');
  if (advice.length) {
    adviceEl.hidden = false;
    adviceEl.innerHTML =
      `<strong>${fails.length} product(s) did not download:</strong><ul>${advice.join('')}</ul>`;
  } else {
    adviceEl.hidden = true;
  }

  // Combined file only exists when more than one product returned rows.
  const hasCombined = (d.files || []).length > 0;
  $('combined-head').hidden = !hasCombined;
  $('combined-downloads').innerHTML = hasCombined
    ? dlLinks(d.files) + uploadBtn('', '', true)
    : '';

  $('results-rows').innerHTML = rows
    .map((r) => {
      // Show WHY it failed. "problem — see log" sent people to a collapsed
      // log that had already scrolled past, which is no help at all.
      const status = r.ok
        ? `${(r.reviews || 0).toLocaleString()} reviews`
        : `<span class="bad">problem</span> <span class="muted">${r.error || ''}</span>`;
      const cov = r.summary && r.summary.coverage_pct
        ? `<span class="muted">${r.summary.coverage_pct}% of Amazon’s total</span>`
        : '';
      const canUpload = r.ok && (r.files || []).length > 0;
      return `<tr>
          <td class="mono">${r.asin}</td>
          <td>${status} ${cov}</td>
          <td class="right">${dlLinks(r.files)}${canUpload ? uploadBtn(r.asin, r.domain, false) : ''}</td>
        </tr>`;
    })
    .join('');

  // Be honest when Amazon truncated a pool rather than implying completeness.
  const capped = new Set();
  rows.forEach((r) => ((r.summary && r.summary.capped_segments) || []).forEach((c) => {
    const star = c.split('/')[0];
    if (star.endsWith('_star')) capped.add(star.replace('_star', '')
      .replace('five', '5').replace('four', '4')
      .replace('three', '3').replace('two', '2').replace('one', '1'));
  }));
  const note = $('cap-note');
  if (capped.size) {
    note.hidden = false;
    note.textContent =
      `Note: Amazon limits how deep any single filter can go, and it cut off the `
      + `${[...capped].sort().reverse().join(', ')} star reviews on at least one product. `
      + `Those counts are a floor, not the full total — everything else is complete.`;
  } else {
    note.hidden = true;
  }

  results.hidden = false;
  $('stop').disabled = false;
  $('stop').textContent = 'Stop';
  resetStart();
}
