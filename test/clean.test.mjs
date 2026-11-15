import assert from 'node:assert/strict';
import { test } from 'node:test';
import { JSDOM } from 'jsdom';
import { cleanArticle } from '../app/clean.mjs';

const parse = (html) => new JSDOM(html).window.document;
const body = (html) => new JSDOM(`<body>${html}</body>`).window.document.body;
const text = (html) => body(html).textContent.replace(/\s+/g, ' ').trim();

// Laid out like a Superhuman AI issue on beehiiv: a flat list of blocks,
// sponsor links marked with shad=1.
const BEEHIIV = `<html><body><nav>Menu</nav>
<div class="dream-post-content-doc">
  <div><a href="https://sponsor.example/?utm=x&amp;shad=1"><img src="https://media.example/banner.png" width="900"></a></div>
  <div><p>Welcome back. Here is the news.</p></div>
  <div><h5>TODAY IN AI</h5></div>
  <div><img src="/uploads/lead.jpg"></div>
  <div><p>1. A real story with <a href="https://news.example/a">a link</a>.</p></div>
  <div><h6>PRESENTED BY SPONSOR</h6><h2>Buy our thing</h2></div>
  <div><img src="https://media.example/ad.png"></div>
  <div><p>Our thing is great. <a href="https://sponsor.example/buy?shad=1">Get it</a></p></div>
  <div><h2>Tools</h2></div>
  <div><ul><li><a href="https://tool-a.example/?shad=1">Tool A*</a>: sponsored</li><li><a href="https://tool-b.example/">Tool B</a>: useful</li></ul></div>
  <div><h2>Spot the fake</h2></div>
  <div><img src="https://media.example/quiz.png"></div>
  <div><h3>Which one is AI?</h3><ul><li><a href="#">Image A</a></li></ul><p><a href="#">Login</a> or <a href="#">Subscribe</a> to participate</p></div>
  <div><h5>EXTRAS</h5></div>
  <div><img src="https://media.example/extras.png"></div>
  <div><h3>Want more?</h3><p>Learn at <a href="academy.superhuman.ai/start?utm_content=full_ad">our academy</a></p></div>
  <div><script>alert(1)</script></div>
</div></body></html>`;

test('Superhuman: drops sponsor sections, sponsored list entries, polls and own promotions', () => {
  const { html, removed } = cleanArticle(parse(BEEHIIV), {
    url: 'https://www.superhuman.ai/p/issue',
    site: 'superhuman.ai',
    platform: 'beehiiv',
    page: true,
  });
  const t = text(html);
  for (const kept of ['Welcome back', 'TODAY IN AI', 'A real story', 'Tool B: useful']) assert.match(t, new RegExp(kept));
  for (const gone of ['Menu', 'Buy our thing', 'Our thing', 'Tool A', 'Spot the fake', 'Which one', 'EXTRAS', 'Want more', 'alert']) {
    assert.doesNotMatch(t, new RegExp(gone));
  }
  assert.deepEqual(removed, { ads: 3, polls: 1, promos: 1 });
  const images = [...body(html).querySelectorAll('img')].map((i) => i.getAttribute('src'));
  assert.deepEqual(images, ['https://www.superhuman.ai/uploads/lead.jpg']);
});

test('another beehiiv newsletter keeps its sponsor links but loses polls', () => {
  const { html, removed } = cleanArticle(parse(BEEHIIV), { url: 'https://other.example/p/x', site: 'other.example', platform: 'beehiiv' });
  assert.match(text(html), /Buy our thing/);
  assert.equal(removed.polls, 1);
  assert.equal(removed.ads, undefined);
});

// Laid out like a Ben's Bites post in its Substack feed.
const SUBSTACK = `
<p>Hi folks,</p>
<div class="subscription-widget-wrap-editor"><div class="subscription-widget"><p>Subscribe to keep reading</p><form><input type="email"></form></div></div>
<p>A real paragraph with <a href="https://example.com/x" onclick="steal()">a link</a>.</p>
<div class="twitter-embed" data-attrs="{&quot;full_text&quot;:&quot;tweet text&quot;}"></div>
<div><hr></div>
<p><em>Ben’s Bites is brought to you by <a href="https://sponsor.example">Sponsor</a></em></p>
<blockquote><p><strong>Sponsor pitch</strong>, buy now.</p></blockquote>
<div><hr></div>
<h3>Headlines</h3>
<div class="captioned-image-container"><figure><a class="image-link" href="https://substackcdn.com/full.png"><img src="https://substackcdn.com/img.png" alt="A chart"></a><figcaption>Caption</figcaption></figure></div>
<p class="button-wrapper"><a class="button" href="https://www.bensbites.com/subscribe">Subscribe now</a></p>
<div class="youtube-wrap"><div class="youtube-inner"><iframe src="https://www.youtube-nocookie.com/embed/x"></iframe></div></div>
<p>The last real paragraph.</p>
<h3>* sponsors who make this newsletter possible :)</h3>
<h3>Wanna partner with us?</h3>
<p>Email us</p>`;

test("Ben's Bites: drops the sponsor block, sponsor footer, subscribe boxes and embeds", () => {
  const { html, removed } = cleanArticle(parse(`<body>${SUBSTACK}</body>`), {
    url: 'https://www.bensbites.com/p/post',
    site: 'bensbites.com',
    platform: 'substack',
    page: false,
    title: 'Post',
  });
  const t = text(html);
  for (const kept of ['Hi folks', 'A real paragraph with a link', 'Headlines', 'Caption', 'The last real paragraph']) assert.match(t, new RegExp(kept));
  for (const gone of ['Subscribe', 'tweet text', 'Sponsor pitch', 'brought to you', 'sponsors who', 'Wanna partner', 'Email us']) {
    assert.doesNotMatch(t, new RegExp(gone));
  }
  assert.deepEqual(removed, { ads: 1, promos: 3, embeds: 2 });
  const out = body(html);
  assert.equal(out.querySelectorAll('hr').length, 1, 'one divider left where the sponsor block was');
  const img = out.querySelector('img');
  assert.equal(img.getAttribute('src'), 'https://substackcdn.com/img.png');
  assert.equal(img.getAttribute('alt'), 'A chart');
  assert.equal(img.closest('a'), null, "the image no longer links to Substack's servers");
  assert.equal(out.querySelector('a[onclick]'), null);
});

const LONG = 'This sentence is part of the article and is long enough to count. '.repeat(10);

test('a web page: finds the article, drops header image and duplicate title, keeps plain tags only', () => {
  const doc = parse(`<html><head><title>T</title><style>p{}</style></head><body>
    <header><nav><a href="/">Home</a></nav></header>
    <main><article>
      <h1>The Title</h1>
      <figure><img src="hero.jpg" alt="Hero"><figcaption></figcaption></figure>
      <p>${LONG}</p>
      <div>Loose text in a div</div><div>Another <b>div</b></div>
      <img src="data:image/png;base64,AAAA">
      <img src="pixel.gif" width="1" height="1">
      <img data-src="lazy.jpg" src="data:image/gif;base64,R0">
      <img data-src="real.jpg" src="/placeholder.gif">
      <img srcset="small.jpg 400w, big.jpg 1200w, huge.jpg 3000w">
      <p><a href="javascript:alert(1)">bad link</a> and <a href="/relative">relative</a></p>
      <p style="color:red" class="x" onmouseover="x()">Styled</p>
      <iframe src="https://elsewhere.example/"></iframe>
      <svg><text>vector</text></svg>
    </article></main>
    <aside><p>${LONG}</p></aside>
  </body></html>`);
  const { html, removed } = cleanArticle(doc, { url: 'https://blog.example/posts/1', title: 'The Title' });
  const out = body(html);
  assert.deepEqual(removed, { embeds: 1, 'header image': 1 });
  assert.equal(out.querySelector('h1'), null);
  assert.deepEqual([...out.querySelectorAll('img')].map((i) => i.getAttribute('src')), ['https://blog.example/posts/lazy.jpg', 'https://blog.example/posts/real.jpg', 'https://blog.example/posts/big.jpg']);
  assert.deepEqual([...out.querySelectorAll('p')].map((p) => p.textContent).slice(1, 3), ['Loose text in a div', 'Another div']);
  const links = [...out.querySelectorAll('a')];
  assert.equal(links[0].getAttribute('href'), null, 'javascript: link loses its address');
  assert.equal(links[1].getAttribute('href'), 'https://blog.example/relative');
  const attributes = [...out.querySelectorAll('*')].flatMap((el) => [...el.attributes].map((a) => a.name));
  assert.deepEqual([...new Set(attributes)].sort(), ['alt', 'href', 'src'].filter((a) => attributes.includes(a)).sort());
  assert.doesNotMatch(out.textContent, /vector|Home|p\{\}/);
  assert.equal(out.textContent.split('This sentence').length - 1, 10, 'the aside is not part of the article');
});

test('a page without article or main: takes the element holding the most paragraph text', () => {
  const doc = parse(`<body><div id="menu"><p>Short</p></div><div id="post"><p>${LONG}</p><p>${LONG}</p></div><div id="footer"><p>Copyright notice that is long enough to count here.</p></div></body>`);
  const { html } = cleanArticle(doc, { url: 'https://site.example/a' });
  assert.equal(body(html).querySelectorAll('p').length, 2);
  assert.doesNotMatch(text(html), /Copyright|Short/);
});

test('a header image inside the first paragraph goes, a later one stays', () => {
  const { html, removed } = cleanArticle(parse('<body><p><a href="https://x.example/"><img src="https://x.example/top.png"></a></p><p>Text</p><p><img src="https://x.example/mid.png"></p></body>'), {
    url: 'https://x.example/',
    page: false,
  });
  assert.deepEqual([...body(html).querySelectorAll('img')].map((i) => i.getAttribute('src')), ['https://x.example/mid.png']);
  assert.equal(removed['header image'], 1);
});

test('srcset: candidates with or without spaces after commas, and addresses that contain commas', () => {
  const doc = parse(`<body><p>Text</p>
    <img srcset="large.jpg 2000w,small.jpg 800w">
    <img srcset="https://substackcdn.com/image/fetch/w_424,c_limit,f_webp/a.png 424w, https://substackcdn.com/image/fetch/w_1456,c_limit,f_webp/a.png 1456w">
    <img srcset="huge.jpg 3000w">
  </body>`);
  const { html } = cleanArticle(doc, { url: 'https://site.example/', page: false });
  assert.deepEqual([...body(html).querySelectorAll('img')].map((i) => i.getAttribute('src')), [
    'https://site.example/small.jpg',
    'https://substackcdn.com/image/fetch/w_1456,c_limit,f_webp/a.png',
    'https://site.example/huge.jpg',
  ]);
});
