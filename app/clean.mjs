// Article cleanup: turns a downloaded web page or feed entry into the
// reader's HTML. Finds the article, applies the rules of its source (ads,
// promotions, polls), drops embeds and the header image, and keeps only
// plain tags: text, links, lists, quotes, code, tables and images.
//
// Works on a Document and never loads anything: the page parses with
// DOMParser, the tests with jsdom.

// Where the article is on pages of a known newsletter platform.
const ROOTS = {
  beehiiv: ['.dream-post-content-doc', '#content-blocks'],
  substack: ['.available-content .body', '.body.markup'],
};

// Rules per site, for ads that only that newsletter marks a certain way.
const SITES = {
  // Superhuman AI (beehiiv): sponsor links carry shad=1; own promotions link
  // to its academy and advertising pages.
  'superhuman.ai': (root, ctx) =>
    beehiivSections(root, ctx, {
      isAd: (a) => /[?&]shad=1(&|$)/.test(a.getAttribute('href') || ''),
      isPromo: (a) => /(^|\/\/)(academy|advertise)\.superhuman\.ai\b/i.test(a.getAttribute('href') || ''),
    }),
  // Ben's Bites (Substack): "Ben's Bites is brought to you by X" up to the
  // next divider, and a sponsor note at the end.
  'bensbites.com': (root, ctx) => {
    sponsorBlocks(root, ctx, /is brought to you by/i);
    footerFrom(root, ctx, /sponsors who make this newsletter possible/i);
  },
};

// Rules for every newsletter on a platform.
const PLATFORMS = {
  beehiiv: (root, ctx) => beehiivSections(root, ctx, {}),
  substack: (root, ctx) => {
    for (const el of root.querySelectorAll(
      '.subscription-widget-wrap, .subscription-widget-wrap-editor, .subscription-widget, .button-wrapper, .captioned-button-wrap, .share-dialog',
    )) {
      if (el.isConnected) remove(el, ctx, 'promos');
    }
    // Images link to their full size on Substack's servers; keep the image.
    for (const a of root.querySelectorAll('a.image-link')) a.replaceWith(...a.childNodes);
  },
};

// Embedded posts, videos, polls and other widgets, by class name.
const EMBED_CLASS =
  /(^|[\s_-])(embed|embedded|tweet|twitter|instagram|tiktok|youtube|vimeo|spotify|soundcloud|podcast|poll|apple-podcast|community-chat)([\s_-]|$)/i;
const EMBED_TAGS = 'iframe, frame, object, embed, video, audio, blockquote.twitter-tweet, blockquote.instagram-media';
const DROP_TAGS = new Set([
  'script', 'style', 'noscript', 'template', 'iframe', 'frame', 'object', 'embed', 'video', 'audio', 'source',
  'track', 'canvas', 'svg', 'math', 'form', 'input', 'button', 'select', 'textarea', 'dialog', 'link', 'meta',
  'nav', 'head', 'title',
]);
const KEEP_TAGS = new Set([
  'p', 'br', 'hr', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'ul', 'ol', 'li', 'blockquote', 'pre', 'code', 'em', 'i',
  'strong', 'b', 'u', 's', 'del', 'ins', 'sub', 'sup', 'small', 'mark', 'a', 'figure', 'figcaption', 'table',
  'thead', 'tbody', 'tfoot', 'tr', 'th', 'td', 'caption', 'dl', 'dt', 'dd', 'abbr', 'q', 'cite', 'kbd', 'time',
]);
const BLOCK_TAGS = new Set([
  'div', 'section', 'article', 'main', 'header', 'footer', 'aside', 'center', 'address', 'details', 'summary',
  'picture', ...KEEP_TAGS,
]);
const INLINE_TAGS = new Set(['a', 'em', 'i', 'strong', 'b', 'u', 's', 'del', 'ins', 'sub', 'sup', 'small', 'mark', 'code', 'abbr', 'q', 'cite', 'kbd', 'time', 'span', 'font', 'br', 'img']);

/**
 * Cleans one article.
 * @param {Document} doc the parsed page or feed entry
 * @param {{url: string, site?: string, platform?: string, page?: boolean, title?: string}} options
 *   page: true for a whole web page, false for a feed entry's content
 * @returns {{html: string, removed: Record<string, number>}}
 */
export function cleanArticle(doc, { url, site = '', platform = '', page = true, title = '' }) {
  const ctx = { removed: {} };
  const root = page ? findRoot(doc, platform) : doc.body;

  SITES[site]?.(root, ctx);
  PLATFORMS[platform]?.(root, ctx);
  for (const el of [...root.querySelectorAll('*')]) {
    if (el.isConnected && (el.matches(EMBED_TAGS) || EMBED_CLASS.test(el.getAttribute('class') || ''))) {
      remove(el, ctx, 'embeds');
    }
  }

  const out = doc.createElement('div');
  out.append(sanitize(root, doc, url));
  tidy(out);
  dropDuplicateTitle(out, title);
  dropHeaderImage(out, ctx);
  tidy(out);
  return { html: out.innerHTML, removed: ctx.removed };
}

function count(ctx, what, n = 1) {
  ctx.removed[what] = (ctx.removed[what] || 0) + n;
}

function remove(el, ctx, what) {
  el.remove();
  if (what) count(ctx, what);
}

const text = (el) => (el.textContent || '').replace(/\s+/g, ' ').trim();

// --- finding the article ------------------------------------------------------

function findRoot(doc, platform) {
  for (const selector of ROOTS[platform] || []) {
    const el = doc.querySelector(selector);
    if (el) return el;
  }
  // The article, main or articleBody element with the most paragraph text.
  let best = null;
  let bestScore = 0;
  for (const el of doc.querySelectorAll('article, main, [itemprop="articleBody"], [role="main"]')) {
    const score = paragraphText(el);
    if (score > bestScore) [best, bestScore] = [el, score];
  }
  if (best && bestScore >= 500) return best;
  // Otherwise the element that directly holds the most paragraph text.
  const scores = new Map();
  for (const p of doc.querySelectorAll('p')) {
    const length = text(p).length;
    if (length < 40 || !p.parentElement) continue;
    scores.set(p.parentElement, (scores.get(p.parentElement) || 0) + length);
  }
  for (const [el, score] of scores) {
    if (score > bestScore) [best, bestScore] = [el, score];
  }
  return best || doc.body;
}

function paragraphText(el) {
  let total = 0;
  for (const p of el.querySelectorAll('p')) total += text(p).length;
  return total;
}

// --- source rules -----------------------------------------------------------------

// beehiiv posts are a flat list of blocks. A section starts at a block with
// a heading and runs to the next one; blocks before the first heading are
// judged one by one. A section with an ad link goes as a whole, unless the
// ads are single entries of a list with other entries. A poll's own heading
// does not start a section, so a quiz goes with its poll.
function beehiivSections(root, ctx, { isAd = () => false, isPromo = () => false }) {
  const isPoll = (el) => /Login or Subscribe to participate/i.test(el.textContent || '');
  const sections = [];
  for (const block of [...root.children]) {
    const heading = Boolean(block.querySelector('h1, h2, h3, h4, h5, h6')) && !isPoll(block);
    if (heading || !sections.at(-1)?.headed) sections.push({ headed: heading, blocks: [] });
    sections.at(-1).blocks.push(block);
  }
  const links = (blocks, test) => blocks.flatMap((b) => [...b.querySelectorAll('a[href]')].filter(test));
  for (const { headed, blocks } of sections) {
    const ads = links(blocks, isAd);
    if (ads.length) {
      const entries = new Set(ads.map((a) => a.closest('li')));
      const others = blocks.flatMap((b) => [...b.querySelectorAll('li')]).filter((li) => !entries.has(li) && text(li));
      if (!entries.has(null) && others.length) {
        for (const li of entries) remove(li, ctx, 'ads');
      } else {
        blocks.forEach((b) => b.remove());
        count(ctx, 'ads');
      }
    } else if (links(blocks, isPromo).length) {
      blocks.forEach((b) => b.remove());
      count(ctx, 'promos');
    } else if (blocks.some(isPoll)) {
      blocks.forEach((b) => b.remove());
      count(ctx, 'polls');
    } else if (headed && blocks.every((b) => !textWithoutHeadings(b))) {
      // A section label left with nothing under it but an image.
      blocks.forEach((b) => b.remove());
    }
  }
}

function textWithoutHeadings(el) {
  const copy = el.cloneNode(true);
  for (const h of copy.querySelectorAll('h1, h2, h3, h4, h5, h6')) h.remove();
  return text(copy);
}

// From a line matching `pattern` up to the next divider (<hr>).
function sponsorBlocks(root, ctx, pattern) {
  for (const line of [...root.querySelectorAll('p, h1, h2, h3, h4, h5, h6')]) {
    if (!line.isConnected || !pattern.test(line.textContent || '')) continue;
    let block = topLevel(root, line);
    while (block && !(block.matches('hr') || block.querySelector('hr'))) {
      const next = block.nextElementSibling;
      block.remove();
      block = next;
    }
    count(ctx, 'ads');
  }
}

// From a line matching `pattern` to the end of the post.
function footerFrom(root, ctx, pattern) {
  const line = [...root.querySelectorAll('p, h1, h2, h3, h4, h5, h6')].find((el) => pattern.test(el.textContent || ''));
  if (!line) return;
  let block = topLevel(root, line);
  while (block) {
    const next = block.nextElementSibling;
    block.remove();
    block = next;
  }
  count(ctx, 'promos');
}

function topLevel(root, el) {
  while (el.parentElement && el.parentElement !== root) el = el.parentElement;
  return el;
}

// --- sanitizing ---------------------------------------------------------------------

function sanitize(node, doc, base) {
  const out = doc.createDocumentFragment();
  for (const child of [...node.childNodes]) {
    if (child.nodeType === 3) {
      out.append(doc.createTextNode(child.textContent));
      continue;
    }
    if (child.nodeType !== 1) continue;
    const tag = child.localName;
    if (DROP_TAGS.has(tag) || child.getAttribute('hidden') !== null || child.getAttribute('aria-hidden') === 'true') continue;
    if (tag === 'img') {
      const img = cleanImage(child, doc, base);
      if (img) out.append(img);
      continue;
    }
    const inner = sanitize(child, doc, base);
    if (KEEP_TAGS.has(tag)) {
      const el = doc.createElement(tag);
      if (tag === 'a') {
        const href = absolute(child.getAttribute('href'), base, ['http:', 'https:', 'mailto:']);
        if (href) el.setAttribute('href', href);
      }
      for (const name of ['colspan', 'rowspan', 'start']) {
        const value = child.getAttribute(name);
        if (value && /^\d{1,3}$/.test(value)) el.setAttribute(name, value);
      }
      el.append(inner);
      out.append(el);
    } else if (BLOCK_TAGS.has(tag) && onlyInline(child)) {
      // A div holding just text becomes a paragraph, so it does not run
      // into the text next to it.
      const p = doc.createElement('p');
      p.append(inner);
      out.append(p);
    } else {
      out.append(inner);
    }
  }
  return out;
}

function onlyInline(el) {
  let hasText = false;
  for (const node of el.childNodes) {
    if (node.nodeType === 3) {
      if (node.textContent.trim()) hasText = true;
    } else if (node.nodeType === 1) {
      if (!INLINE_TAGS.has(node.localName) && !DROP_TAGS.has(node.localName)) return false;
      if (text(node)) hasText = true;
    }
  }
  return hasText;
}

function cleanImage(img, doc, base) {
  const size = (name) => Number.parseInt(img.getAttribute(name) || '', 10);
  if (size('width') <= 2 || size('height') <= 2) return null; // tracking pixel
  // A lazy-loading page keeps the real image in data-src and the like; src
  // may be a placeholder.
  const srcset = img.getAttribute('data-srcset') || img.getAttribute('srcset');
  const candidates = [img.getAttribute('data-src'), img.getAttribute('data-lazy-src'), img.getAttribute('data-original'), srcset && largestFromSrcset(srcset), img.getAttribute('src')];
  let src = null;
  for (const candidate of candidates) {
    src = absolute(candidate, base, ['http:', 'https:']);
    if (src) break;
  }
  if (!src) return null;
  const out = doc.createElement('img');
  out.setAttribute('src', src);
  const alt = (img.getAttribute('alt') || '').trim();
  if (alt) out.setAttribute('alt', alt);
  return out;
}

// The widest candidate up to 1600 pixels, else the narrowest. Read as
// browsers do: an address runs to the next space (commas inside it belong to
// it, one at its end ends the candidate); its descriptor runs to the next comma.
function largestFromSrcset(srcset) {
  const candidates = [];
  let i = 0;
  while (i < srcset.length) {
    while (i < srcset.length && /[\s,]/.test(srcset[i])) i++;
    let end = i;
    while (end < srcset.length && !/\s/.test(srcset[end])) end++;
    let url = srcset.slice(i, end);
    let descriptor = '';
    if (url.endsWith(',')) {
      url = url.replace(/,+$/, '');
    } else {
      const comma = srcset.indexOf(',', end);
      descriptor = srcset.slice(end, comma === -1 ? srcset.length : comma);
      end = comma === -1 ? srcset.length : comma + 1;
    }
    if (url) candidates.push([url, Number.parseInt(descriptor.trim(), 10) || 0]);
    i = end;
  }
  const fitting = candidates.filter(([, width]) => width <= 1600).sort((a, b) => b[1] - a[1]);
  const all = [...candidates].sort((a, b) => a[1] - b[1]);
  return (fitting[0] || all[0] || [null])[0];
}

function absolute(value, base, schemes) {
  if (!value || value.trim().startsWith('data:')) return null;
  try {
    const url = new URL(value.trim(), base);
    return schemes.includes(url.protocol) ? url.href : null;
  } catch {
    return null;
  }
}

// --- tidying ------------------------------------------------------------------------

// Removes elements left without text or image, and repeated or dangling
// dividers and line breaks.
function tidy(out) {
  let changed = true;
  while (changed) {
    changed = false;
    for (const el of [...out.querySelectorAll('*')]) {
      if (['img', 'hr', 'br', 'td', 'th'].includes(el.localName)) continue;
      if (!text(el) && !el.querySelector('img')) {
        el.remove();
        changed = true;
      }
    }
  }
  for (const hr of [...out.querySelectorAll('hr')]) {
    let prev = hr.previousElementSibling;
    if (!prev || prev.localName === 'hr') hr.remove();
  }
  while (out.lastElementChild && ['hr', 'br'].includes(out.lastElementChild.localName)) out.lastElementChild.remove();
  while (out.firstElementChild && ['hr', 'br'].includes(out.firstElementChild.localName)) out.firstElementChild.remove();
}

function dropDuplicateTitle(out, title) {
  const first = out.querySelector('h1, h2, h3, p');
  if (first && title && /^h[1-3]$/.test(first.localName) && text(first).toLowerCase() === title.trim().toLowerCase()) {
    first.remove();
  }
}

// The header image: an image before any text.
function dropHeaderImage(out, ctx) {
  const walker = out.ownerDocument.createTreeWalker(out, 0x1 | 0x4); // elements and text
  for (let node = walker.nextNode(); node; node = walker.nextNode()) {
    if (node.nodeType === 3) {
      if (node.textContent.trim()) return;
    } else if (node.localName === 'img') {
      let el = node;
      while (el.parentElement && el.parentElement !== out && !text(el.parentElement)) el = el.parentElement;
      remove(el, ctx, 'header image');
      return;
    }
  }
}
