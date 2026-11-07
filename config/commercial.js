/* Shared public contact, ISO dates and same-origin CSRF transport. */
(() => {
  'use strict';
  // Capture dynamic image failures too; no raw-media fallback.
  document.addEventListener('error',event=>{const img=event.target;if(img?.tagName!=='IMG'||img.hasAttribute('data-preview-img')||img.closest('.commercial-qr'))return;const text=document.createElement('span');text.textContent='安全圖片準備中';text.className='placeholder';img.replaceWith(text);},true);
  const nativeFetch = window.fetch.bind(window);
  let sessionPromise, csrf = '';
  const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  function date(value) {
    let s = String(value ?? '').normalize('NFKC').replace(/\s/g, '');
    let m = s.match(/^(令和|平成|昭和)(元|\d+)年(?:(\d{1,2})月(?:(\d{1,2})日)?)?$/);
    let y, mo, d, era;
    if (m) { era=m[1]; const n=m[2]==='元'?1:Number(m[2]); if(n<1)return '待確認'; y=({令和:2018,平成:1988,昭和:1925})[era]+n; mo=m[3];d=m[4]; }
    else {m=s.match(/^(\d{4})(?:年(?:(\d{1,2})月(?:(\d{1,2})日)?)?|[-/](\d{1,2})(?:[-/](\d{1,2}))?)?$/);if(!m)return '待確認';y=Number(m[1]);mo=m[2]||m[4];d=m[3]||m[5];}
    if(y<1||y>9999||mo&&(Number(mo)<1||Number(mo)>12))return '待確認';
    const test=new Date(0);test.setUTCFullYear(y,Number(mo||1)-1,Number(d||1));
    if(d&&(test.getUTCMonth()!==Number(mo)-1||test.getUTCDate()!==Number(d)))return '待確認';
    const out=String(y).padStart(4,'0')+(mo?'-'+String(mo).padStart(2,'0'):'')+(d?'-'+String(d).padStart(2,'0'):'');
    if(era){const [lo,hi]=({令和:['2019-05-01','9999-12-31'],平成:['1989-01-08','2019-04-30'],昭和:['1926-12-25','1989-01-07']})[era];const end=d?out:mo?out+'-31':out+'-12-31',start=d?out:mo?out+'-01':out+'-01-01';if(end<lo||start>hi)return '待確認';}
    return out;
  }
  function timestamp(value) {
    if (!/[T ]\d{2}:\d{2}/.test(String(value||''))) return date(value);
    const raw=String(value), dt=new Date(/[Zz]|[+-]\d\d:\d\d$/.test(raw)?raw:raw.replace(' ','T')+'Z');
    if(Number.isNaN(dt.getTime()))return '待確認';
    const parts = Object.fromEntries(new Intl.DateTimeFormat('en-GB',{timeZone:'Asia/Tokyo',year:'numeric',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',hourCycle:'h23'}).formatToParts(dt).map(p=>[p.type,p.value]));
    return `${parts.year}-${parts.month}-${parts.day} ${parts.hour}:${parts.minute} JST`;
  }
  function safeMedia(url) {try{const u=new URL(url,location.origin);return u.origin===location.origin&&!u.username&&!u.password&&/^\/api\/customer-safe-image\/[^/]+\/[^/]+$/.test(u.pathname)?u.pathname:'';}catch{return '';}}
  function normalize(data) {
    if(Array.isArray(data))return data.map(normalize);
    if(!data||typeof data!=='object')return data;
    const out={};for(const [key,value] of Object.entries(data)) {
      if(['builtAt','built_date_full','built_date','reins_registered_at','reins_updated_at'].includes(key)&&value){if(!Object.hasOwn(data,key+'_raw'))out[key+'_raw']=value;out[key]=date(value)==='待確認'?'':date(value);}
      else out[key]=normalize(value);
    }return out;
  }
  async function session(refresh=false) {
    if(refresh)sessionPromise=null;
    if(!sessionPromise)sessionPromise=nativeFetch('/api/auth/session',{credentials:'same-origin',cache:'no-store'}).then(async r=>{if(!r.ok)throw Error('無法確認登入狀態');const d=await r.json();csrf=d.csrf_token||'';return d;}).catch(e=>{sessionPromise=null;throw e;});
    return sessionPromise;
  }
  window.fetch=async function(input, init={}) {
    const url=new URL(typeof input==='string'?input:input.url,location.href),method=String(init.method||(input instanceof Request?input.method:'GET')).toUpperCase();
    if(url.origin===location.origin&&!['GET','HEAD','OPTIONS'].includes(method)) {
      await session();if(!csrf)throw Error('安全驗證失效，請重新登入');
      const headers=new Headers(init.headers||(input instanceof Request?input.headers:undefined));headers.set('X-CSRF-Token',csrf);init={...init,headers,credentials:'same-origin'};
    }
    const r=await nativeFetch(input,init);
    if(url.origin===location.origin&&(r.headers.get('content-type')||'').includes('application/json')){
      const json=r.json.bind(r);r.json=async()=>{const d=await json();if(d.csrf_token)csrf=d.csrf_token;return normalize(d);};
    }return r;
  };
  async function api(url, options) {const r=await fetch(url,options);let d;try{d=await r.json();}catch{throw Error('服務未能回應，請稍後再試');}if(!r.ok)throw Error(d.error||d.message||`請求失敗 (${r.status})`);return d;}
  const post=(url,data={},method='POST')=>api(url,{method,headers:{'Content-Type':'application/json'},body:JSON.stringify(data)});
  window.Commercial={date,timestamp,safeMedia,esc,session,api,post};
  document.addEventListener('DOMContentLoaded',async()=>{
    const publicPage=['/','/listings','/map','/privacy','/terms','/client/portal'].includes(location.pathname)||location.pathname.startsWith('/listing/')||/^(index|listings|listing|map|privacy|terms|client_portal)\.html$/.test(location.pathname.split('/').pop());
    const css=document.createElement('style');css.textContent=`body{color:#183a30;background:#f3f6f3;font-family:system-ui,sans-serif}header a{color:inherit}.reveal{opacity:1!important;transform:none!important}@media(max-width:600px){header>div{height:auto!important;min-height:64px;flex-wrap:wrap;gap:.5rem;padding:.6rem}header>div>div{flex-wrap:wrap;gap:.5rem}header .text-lg{font-size:1rem}}.commercial-contact{background:#f1f5f2;color:#183a30;border-top:1px solid #d4ded8;padding:28px max(5vw,20px);font:14px/1.7 system-ui,sans-serif;margin-top:32px}.commercial-contact strong{font-size:18px}.commercial-actions{display:flex;gap:12px;flex-wrap:wrap;margin:12px 0}.commercial-actions a,.commercial-actions button{display:inline-block;padding:10px 16px;background:#174b3b;color:white;border:0;border-radius:6px;text-decoration:none;font:inherit;cursor:pointer}.commercial-contact a{color:#174b3b}.commercial-actions a{color:white}.commercial-dialog{max-width:560px;width:calc(100% - 32px);border:1px solid #d4ded8;border-radius:10px;padding:24px;background:#fff;color:#183a30;font:16px/1.6 system-ui}.commercial-dialog::backdrop{background:rgba(10,25,20,.65)}.commercial-qr{display:grid;grid-template-columns:1fr 1fr;gap:20px}.commercial-qr img{max-width:100%;height:auto}.commercial-close{float:right;border:0;background:none;font-size:24px;cursor:pointer}.commercial-dialog a{color:#174b3b}@media(max-width:520px){.commercial-qr{grid-template-columns:1fr}.commercial-dialog{max-height:90vh;overflow:auto}}`;
    document.head.append(css);
    if(!publicPage)return;
    document.documentElement.lang='zh-HK';
    document.querySelectorAll('img').forEach(img=>img.addEventListener('error',()=>{if(img.closest('.commercial-qr'))return;const span=document.createElement('span');span.textContent='安全圖片準備中';span.className='placeholder';img.replaceWith(span);},{once:true}));
    let c={brand:'出雲ランドエステー',email:'akira@izumole.com',whatsapp_display:'+81 80-9190-7999',whatsapp_url:'https://wa.me/818091907999',label:'聯絡出雲ランドエステー'};
    try{c={...c,...await api('/api/public-contact')};}catch{/* Fixed official fallback; never expose upstream. */}
    const mail=/^[^\s@<>]+@[^\s@<>]+$/.test(c.email)?c.email:'akira@izumole.com';
    const wa=/^https:\/\/wa\.me\/\d+$/.test(c.whatsapp_url)?c.whatsapp_url:'https://wa.me/818091907999';
    const footer=document.createElement('footer');footer.className='commercial-contact';footer.innerHTML=`<strong>${esc(c.brand)}</strong><div>${esc(c.label)}</div><div class="commercial-actions"><a href="${esc(wa)}" target="_blank" rel="noopener">WhatsApp ${esc(c.whatsapp_display)}</a><a href="mailto:${esc(mail)}">Email</a><button type="button" data-contact-qr>WeChat / 官方 QR</button></div><div><a href="mailto:${esc(mail)}">${esc(mail)}</a> · <a href="/privacy">私隱政策</a> · <a href="/terms">使用條款</a> · <a href="/login/client">客戶登入</a></div>`;
    const modal=document.createElement('dialog');modal.className='commercial-dialog';modal.setAttribute('aria-label','官方聯絡 QR');modal.innerHTML=`<button class="commercial-close" aria-label="關閉">×</button><h2>聯絡${esc(c.brand)}</h2><div class="commercial-qr"><section><h3>WhatsApp</h3><img src="/api/public-contact-qr/whatsapp" alt="官方 WhatsApp QR"><p>${esc(c.whatsapp_display)}</p></section><section><h3>WeChat</h3><img src="/api/public-contact-qr/wechat" alt="官方 WeChat QR"></section></div><p><a href="mailto:${esc(mail)}">${esc(mail)}</a></p>`;
    document.body.append(footer,modal);footer.querySelector('button').onclick=()=>modal.showModal();modal.querySelector('button').onclick=()=>modal.close();modal.onclick=e=>{if(e.target===modal)modal.close();};modal.querySelectorAll('img').forEach(img=>img.onerror=()=>{const text=document.createElement('p');text.textContent='官方 QR 暫未能載入，請使用 WhatsApp 或 Email 聯絡。';img.replaceWith(text);});
  });
})();
