function visualEscape(value) {
  return String(value).replace(/[&<>"']/g, character => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[character]));
}

function markdownDocument(text, version, scroll = 0) {
  const html = DOMPurify.sanitize(marked.parse(text), {ALLOWED_TAGS: ['h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'p', 'br', 'hr', 'strong', 'em', 'del', 'blockquote', 'ul', 'ol', 'li', 'pre', 'code', 'table', 'thead', 'tbody', 'tr', 'th', 'td', 'a', 'img'], ALLOWED_ATTR: ['href', 'src', 'alt', 'title', 'start', 'colspan', 'rowspan']});
  const documentCopy = new DOMParser().parseFromString(html, 'text/html');
  const assets = version.assets || {};
  const captured = path => {
    const normalized = new URL(path, new URL(version.entrypoint || 'document.md', 'https://captured.invalid/')).pathname.slice(1);
    const value = assets[normalized] || assets[path.replace(/^\.\//, '')];
    if (!value) return null;
    const url = new URL(typeof value === 'string' ? value : value.url, location.origin);
    return url.origin === location.origin && url.pathname.startsWith('/visual-preview/') ? url.href : null;
  };
  for (const link of documentCopy.querySelectorAll('a')) {
    const href = link.getAttribute('href') || '';
    let url = captured(href);
    if (/^https?:\/\//i.test(href)) url = new URL(href).href;
    if (url) { link.href = url; link.target = '_blank'; link.rel = 'noopener noreferrer'; }
    else link.removeAttribute('href');
  }
  for (const image of documentCopy.querySelectorAll('img')) {
    const src = image.getAttribute('src') || '';
    const url = captured(src) || (/^data:image\/(png|jpeg|gif|webp|avif);base64,/i.test(src) ? src : null);
    if (url) image.src = url;
    else { const label = documentCopy.createElement('span'); label.textContent = image.alt ? `[Image: ${image.alt}]` : '[Image not captured]'; image.replaceWith(label); }
  }
  const token = crypto.randomUUID();
  const assetUrls = [...documentCopy.querySelectorAll('img')].map(image => image.src).filter(url => !url.startsWith('data:'));
  const policy = `default-src 'none'; style-src 'unsafe-inline'; img-src data: ${assetUrls.join(' ')}; script-src 'nonce-${token}'; form-action 'none'; base-uri 'none'`;
  const position = Number.isFinite(scroll) ? Math.max(0, scroll) : 0;
  return {token, html: `<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><meta http-equiv="Content-Security-Policy" content="${visualEscape(policy)}"><style>html{color-scheme:light;font:15px/1.65 system-ui,sans-serif;color:#23392c;background:#fff}body{margin:0;padding:24px;overflow-wrap:anywhere}h1,h2,h3{line-height:1.25}h1{font-size:27px}h2{font-size:21px}pre{padding:14px;background:#f2f5f3;border-radius:6px;white-space:pre-wrap}code{font-size:13px}blockquote{margin-left:0;padding-left:14px;border-left:3px solid #b6cec0;color:#536c5e}table{border-collapse:collapse;display:block;max-width:100%;overflow:auto;font-size:13px}td,th{padding:7px;border:1px solid #dce5df;text-align:left}a{color:#287247}img{display:block;max-width:100%;height:auto;margin:12px 0}</style></head><body>${documentCopy.body.innerHTML}<script nonce="${token}">let timer;addEventListener('scroll',()=>{clearTimeout(timer);timer=setTimeout(()=>parent.postMessage({type:'jev-reader-scroll',token:'${token}',y:scrollY},'*'),100)});addEventListener('load',()=>scrollTo(0,${position}));</script></body></html>`};
}

class PdfReader {
  constructor(root, saved, change) {
    this.root = root;
    this.saved = saved;
    this.change = change;
    this.epoch = 0;
    this.root.innerHTML = `<div class="reader-tools"><button type="button" class="pdf-prev" aria-label="Previous PDF page">‹</button><label>Page <input class="pdf-page" type="number" min="1" value="1" aria-label="PDF page"></label><span class="pdf-count"></span><button type="button" class="pdf-next" aria-label="Next PDF page">›</button><select class="pdf-zoom" aria-label="PDF zoom"><option value="fit">Fit width</option><option value="0.75">75%</option><option value="1">100%</option><option value="1.5">150%</option><option value="2">200%</option></select></div><form class="pdf-search"><input type="search" aria-label="Find in PDF" placeholder="Find in PDF"><button>Find next</button></form><p class="reader-status" role="status"></p><div class="pdf-page-view"><canvas aria-label="PDF page"></canvas></div>`;
    this.find = selector => root.querySelector(selector);
    this.find('.pdf-prev').onclick = () => this.show(this.number - 1);
    this.find('.pdf-next').onclick = () => this.show(this.number + 1);
    this.find('.pdf-page').onchange = event => this.show(Number(event.target.value));
    this.find('.pdf-zoom').value = saved.pdfZoom || 'fit';
    this.find('.pdf-zoom').onchange = () => this.show(this.number);
    this.find('.pdf-search').onsubmit = event => { event.preventDefault(); this.search(); };
    this.find('.pdf-page-view').onscroll = event => { this.saved.pdfScroll = event.target.scrollTop; this.change(); };
  }

  async load(bytes) {
    const epoch = this.epoch;
    const library = await import('/vendor/pdfjs/pdf.mjs');
    if (epoch !== this.epoch) return;
    library.GlobalWorkerOptions.workerSrc = '/vendor/pdfjs/pdf.worker.mjs';
    this.loading = library.getDocument({data: new Uint8Array(bytes), isEvalSupported: false, useSystemFonts: true, disableAutoFetch: true});
    this.document = await this.loading.promise;
    if (epoch !== this.epoch) return;
    this.find('.pdf-count').textContent = `of ${this.document.numPages}`;
    this.find('.pdf-page').max = this.document.numPages;
    await this.show(this.saved.pdfPage || 1, true);
  }

  async show(number, restore = false) {
    if (!this.document || this.closed) return;
    const epoch = ++this.epoch;
    this.renderTask?.cancel();
    this.number = Math.max(1, Math.min(this.document.numPages, number || 1));
    this.find('.pdf-page').value = this.number;
    this.find('.pdf-prev').disabled = this.number === 1;
    this.find('.pdf-next').disabled = this.number === this.document.numPages;
    this.saved.pdfPage = this.number;
    this.saved.pdfZoom = this.find('.pdf-zoom').value;
    this.change();
    try {
      const page = await this.document.getPage(this.number);
      if (epoch !== this.epoch) return;
      const base = page.getViewport({scale: 1});
      const available = Math.max(160, this.find('.pdf-page-view').clientWidth - 24);
      const scale = this.saved.pdfZoom === 'fit' ? available / base.width : Number(this.saved.pdfZoom);
      const viewport = page.getViewport({scale});
      const pixelRatio = Math.min(2, window.devicePixelRatio || 1, Math.sqrt(5000000 / (viewport.width * viewport.height)));
      const canvas = document.createElement('canvas');
      canvas.setAttribute('aria-label', `PDF page ${this.number}`);
      canvas.width = Math.floor(viewport.width * pixelRatio);
      canvas.height = Math.floor(viewport.height * pixelRatio);
      canvas.style.width = `${viewport.width}px`;
      canvas.style.height = `${viewport.height}px`;
      this.renderTask = page.render({canvasContext: canvas.getContext('2d'), viewport, transform: [pixelRatio, 0, 0, pixelRatio, 0, 0]});
      await this.renderTask.promise;
      if (epoch !== this.epoch) return;
      this.find('.pdf-page-view').replaceChildren(canvas);
      this.find('.pdf-page-view').scrollTop = restore ? this.saved.pdfScroll || 0 : 0;
    } catch (error) {
      if (epoch === this.epoch && error.name !== 'RenderingCancelledException') this.find('.reader-status').textContent = 'This page could not be rendered. Download the original PDF to open it.';
    }
  }

  async search() {
    const text = this.find('.pdf-search input').value.trim().toLocaleLowerCase();
    if (!text || !this.document) return;
    const document = this.document;
    this.find('.reader-status').textContent = 'Searching…';
    const start = this.lastSearch === text ? this.number % document.numPages + 1 : this.number;
    this.lastSearch = text;
    for (let offset = 0; offset < document.numPages; offset += 1) {
      const number = (start + offset - 1) % document.numPages + 1;
      const page = await document.getPage(number);
      const content = await page.getTextContent();
      if (this.closed || this.document !== document) return;
      const words = content.items.map(item => item.str || '').join(' ');
      const index = words.toLocaleLowerCase().indexOf(text);
      if (index >= 0) {
        await this.show(number);
        if (!this.closed) this.find('.reader-status').textContent = `Page ${number}: ${words.slice(Math.max(0, index - 35), index + text.length + 80)}`;
        return;
      }
    }
    if (!this.closed) this.find('.reader-status').textContent = 'No matching text found. Scanned pages may not contain searchable text.';
  }

  destroy() {
    this.closed = true;
    this.epoch += 1;
    this.renderTask?.cancel();
    this.loading?.destroy();
  }
}
