"""Image-only justified layout; avatars, stickers and inline emoji stay untouched."""

GALLERY_CSS = """
.justified-gallery{display:flex;flex-direction:column;gap:6px;width:96%;max-width:100%;margin-top:16px}
.justified-gallery>img{max-width:100%;height:auto;max-height:480px;object-fit:contain;align-self:flex-start}
.justified-row{display:flex;gap:6px;width:100%;align-items:flex-start}
.justified-item{flex:var(--ratio,1) 1 0%;min-width:0;aspect-ratio:var(--ratio,1);overflow:hidden;border-radius:8px;background:transparent}
.justified-item img{display:block;width:100%;height:100%;max-width:none;object-fit:contain}
.justified-gallery.single-image{width:100%}
.single-image .comment-image-wrap{margin-top:0;max-width:100%;background:transparent}
.single-image .comment-image{display:block;width:auto;height:auto;max-width:100%;max-height:480px;object-fit:contain}
"""

GALLERY_SCRIPT = r"""
<script>
window.__parserXGalleryReady = (async () => {
  const galleries = [...document.querySelectorAll('.justified-gallery')];
  const failedComments = new Set();
  const waitForImage = img => new Promise(resolve => {
    const done = () => { img.removeEventListener('load', done); img.removeEventListener('error', done); resolve(); };
    if (img.complete) return resolve();
    img.addEventListener('load', done); img.addEventListener('error', done);
  });
  await Promise.all(galleries.map(async gallery => {
    const imgs = [...gallery.querySelectorAll('img')];
    await Promise.all(imgs.map(waitForImage));
    const valid = imgs.filter(img => img.complete && img.naturalWidth > 0 && img.naturalHeight > 0);
    gallery.replaceChildren();
    if (!valid.length) {
      gallery.style.display = 'none';
      const comment = gallery.closest('.comment-card, .reply-card');
      if (comment) failedComments.add(comment);
      return;
    }
    if (valid.length === 1) {
      gallery.classList.add('single-image');
      const wrap = document.createElement('div'); wrap.className = 'comment-image-wrap';
      wrap.append(valid[0]); gallery.append(wrap); return;
    }
    const counts = valid.length === 4 ? [2,2] : valid.length === 7 ? [3,2,2] : [];
    if (!counts.length) for(let i=0;i<valid.length;i+=3) counts.push(Math.min(3,valid.length-i));
    let offset = 0;
    let previousHeight = null;
    const width = gallery.getBoundingClientRect().width;
    for (const count of counts) {
      const row = document.createElement('div'); row.className='justified-row';
      const members = valid.slice(offset, offset+count); offset += count;
      const ratios = members.map(img => img.naturalWidth/img.naturalHeight);
      const naturalHeight = (width - 6*(count-1))/ratios.reduce((a,b)=>a+b,0);
      // A partial last row never grows taller than the preceding complete row.
      const height = count < 3 && ![2,4,7].includes(valid.length)
        ? Math.min(naturalHeight, previousHeight || 240) : naturalHeight;
      if (height < naturalHeight) row.style.width = (height*ratios.reduce((a,b)=>a+b,0)+6*(count-1))+'px';
      members.forEach((img,i) => {
        const item = document.createElement('div'); item.className='justified-item';
        item.style.setProperty('--ratio', String(ratios[i])); item.append(img); row.append(item);
      });
      gallery.append(row); previousHeight=height;
    }
  }));
  // Decide only after every gallery has settled, so a pending reply can keep its parent.
  for (const comment of [...document.querySelectorAll('.comment-card, .reply-card')].reverse()) {
    if (!failedComments.has(comment)) continue;
    const body = comment.querySelector(':scope > .comment-body');
    // A parent reached through reply removal may still have a pending sticker.
    await Promise.all([...body.querySelectorAll('.comment-sticker, .comment-emote')].map(waitForImage));
    const content = body.querySelector(':scope > .comment-content');
    const hasText = content.textContent.trim().length > 0;
    const hasImages = [...body.querySelectorAll('img')].some(img => img.complete && img.naturalWidth > 0 && img.naturalHeight > 0);
    if (hasText || hasImages || body.querySelector('.reply-card')) continue;
    const parent = comment.parentElement.closest('.comment-card, .reply-card');
    comment.remove();
    if (parent) failedComments.add(parent);
  }
  for (const section of document.querySelectorAll('.comment-section')) {
    if (!section.querySelector('.comment-card')) section.remove();
  }
})();
</script>
"""
