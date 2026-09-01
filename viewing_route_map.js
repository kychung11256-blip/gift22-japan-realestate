/* Shared Gift22 viewing-route MapLibre/GSI renderer.
 * Real GSI raster base map + HTML markers + optional route GeoJSON.
 * Does not fabricate route lines: if geometry is absent/empty, markers remain and a warning is shown.
 */
(function(){
  const GSI_STYLE={version:8,sources:{gsi:{type:'raster',tiles:['https://cyberjapandata.gsi.go.jp/xyz/pale/{z}/{x}/{y}.png'],tileSize:256,attribution:'<a href="https://maps.gsi.go.jp/development/ichiran.html">GSI Japan</a>'}},layers:[{id:'gsi-bg',type:'raster',source:'gsi'}]};
  const GREEN='#164d3d', GREEN2='#23755b', RED='#ef4444', BLUE='#4f7c70';
  function esc(v){return String(v??'').replace(/[&<>'"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[c]));}
  function lngLatOfStop(s){const p=s.property||s;const lat=Number(p.latitude||s.lat), lon=Number(p.longitude||s.lon);return Number.isFinite(lat)&&Number.isFinite(lon)&&lat&&lon?[lon,lat]:null;}
  function flattenGeometry(g){
    if(!g)return[];
    if(g.type==='LineString')return Array.isArray(g.coordinates)?g.coordinates.filter(c=>Array.isArray(c)&&c.length>=2):[];
    if(g.type==='MultiLineString')return (g.coordinates||[]).flat().filter(c=>Array.isArray(c)&&c.length>=2);
    if(g.type==='Feature')return flattenGeometry(g.geometry);
    if(g.type==='FeatureCollection')return (g.features||[]).flatMap(f=>flattenGeometry(f.geometry));
    return[];
  }
  function routeFeature(geometry){
    if(!geometry)return null;
    if(geometry.type==='FeatureCollection')return geometry;
    if(geometry.type==='Feature')return {type:'FeatureCollection',features:[geometry]};
    if(['LineString','MultiLineString'].includes(geometry.type))return {type:'FeatureCollection',features:[{type:'Feature',properties:{},geometry}]};
    return null;
  }
  function makeMarker(label,type){
    const el=document.createElement('button');
    el.type='button'; el.className='vr-marker '+(type||''); el.textContent=label;
    el.style.cssText='width:30px;height:30px;border-radius:50%;border:2px solid #fff;box-shadow:0 8px 18px rgba(18,34,29,.25);display:flex;align-items:center;justify-content:center;color:#fff;font-weight:800;font-size:12px;cursor:pointer;background:'+(type==='origin'?BLUE:type==='end'?'#8b5cf6':GREEN2);
    return el;
  }
  function addRouteLayer(map,geometry){
    const data=routeFeature(geometry); const coords=flattenGeometry(geometry);
    if(!data||!coords.length)return false;
    if(map.getLayer('viewing-route-line'))map.removeLayer('viewing-route-line');
    if(map.getSource('viewing-route'))map.removeSource('viewing-route');
    map.addSource('viewing-route',{type:'geojson',data});
    map.addLayer({id:'viewing-route-line',type:'line',source:'viewing-route',layout:{'line-cap':'round','line-join':'round'},paint:{'line-color':RED,'line-width':5,'line-opacity':0.9}});
    return true;
  }
  function fitAll(map,stops,geometry){
    const pts=[...flattenGeometry(geometry),...(stops||[]).map(lngLatOfStop).filter(Boolean)];
    if(!pts.length)return;
    let minX=pts[0][0],maxX=pts[0][0],minY=pts[0][1],maxY=pts[0][1];
    pts.forEach(([x,y])=>{minX=Math.min(minX,x);maxX=Math.max(maxX,x);minY=Math.min(minY,y);maxY=Math.max(maxY,y);});
    if(Math.abs(maxX-minX)<0.001){minX-=0.01;maxX+=0.01;} if(Math.abs(maxY-minY)<0.001){minY-=0.01;maxY+=0.01;}
    map.fitBounds([[minX,minY],[maxX,maxY]],{padding:70,maxZoom:15,duration:450});
  }
  class ViewingRouteMap{
    constructor(opts){
      this.container=typeof opts.container==='string'?document.getElementById(opts.container):opts.container;
      this.status=typeof opts.status==='string'?document.getElementById(opts.status):opts.status;
      this.readonly=!!opts.readonly; this.map=null; this.markers=[]; this.pending=null; this.loadError='';
      this.init();
    }
    init(){
      if(!this.container)return;
      if(typeof maplibregl==='undefined'){this.fail('MapLibre 載入失敗，請檢查網絡/CDN。');return;}
      try{
        this.map=new maplibregl.Map({container:this.container,style:GSI_STYLE,center:[139.75,35.68],zoom:11,maxBounds:[[129,30],[146,46]],attributionControl:true});
        this.map.addControl(new maplibregl.NavigationControl({showCompass:false}),'top-right');
        this.map.on('load',()=>{this.setStatus('GSI 地圖已載入'); if(this.pending)this.render(this.pending);});
        this.map.on('error',e=>{const msg=e&&e.error?e.error.message:'地圖載入錯誤'; this.setStatus(msg,true); console.warn('[viewing-map]',msg);});
      }catch(e){this.fail(e.message||String(e));}
    }
    fail(msg){
      this.loadError=msg; this.setStatus(msg,true);
      this.container.innerHTML='<div class="map-fallback"><strong>地圖未能載入</strong><p>'+esc(msg)+'</p><button type="button" data-map-retry>重試</button></div>';
      const btn=this.container.querySelector('[data-map-retry]'); if(btn)btn.onclick=()=>{this.container.innerHTML='';this.init();if(this.pending)setTimeout(()=>this.render(this.pending),300);};
    }
    setStatus(msg,warn){if(this.status){this.status.textContent=msg;this.status.classList.toggle('warn',!!warn);}}
    resize(){if(this.map)setTimeout(()=>this.map.resize(),50);}
    clear(){
      this.markers.forEach(m=>m.remove()); this.markers=[];
      if(this.map){['viewing-route-line'].forEach(id=>{if(this.map.getLayer(id))this.map.removeLayer(id)});['viewing-route'].forEach(id=>{if(this.map.getSource(id))this.map.removeSource(id)});}
    }
    render(data){
      this.pending=data||{}; if(!this.map||!this.map.loaded())return;
      this.clear(); const stops=[];
      const plan=data.plan||data; const selected=data.selected||[];
      const origin=plan.start?{...plan.start,seq:'出',isOrigin:true}:data.start;
      const originForFit=origin&&Number(origin.lat)&&Number(origin.lon)?origin:null;
      if(originForFit)stops.push(originForFit);
      if(plan.stops&&plan.stops.length){plan.stops.forEach(s=>stops.push(s));}
      else selected.forEach((p,i)=>stops.push({seq:i+1,property:p,listingId:p.id}));
      if(plan.end&&Number(plan.end.lat)&&Number(plan.end.lon)&&(Math.abs(Number(plan.end.lat)-(origin?.lat||0))>0.000001||Math.abs(Number(plan.end.lon)-(origin?.lon||0))>0.000001))stops.push({...plan.end,seq:'終',isEnd:true});
      const drawStops = (plan.routeGeometry||plan.geometry) ? stops.filter(s=>!s.isOrigin) : stops;
      drawStops.forEach((s,i)=>{
        const ll=s.isOrigin||s.isEnd?[Number(s.lon),Number(s.lat)]:lngLatOfStop(s); if(!ll)return;
        const p=s.property||s; const label=s.isOrigin?'出':s.isEnd?'終':String(s.seq||i);
        const el=makeMarker(label,s.isOrigin?'origin':s.isEnd?'end':'stop');
        const html=s.isOrigin||s.isEnd?`<strong>${esc(s.label||s.address||label)}</strong>`:`<strong>${esc(p.title||p.address||s.listingId)}</strong><div style="font-size:12px;color:#68776f;margin-top:4px">${esc(p.address||'')}</div><a href="${esc(p.url||('/listing/'+encodeURIComponent(s.listingId||p.id||'')))}" target="_blank" rel="noopener" style="color:${GREEN};font-weight:700">物件詳情 ↗</a>`;
        const marker=new maplibregl.Marker({element:el}).setLngLat(ll).setPopup(new maplibregl.Popup({offset:24,maxWidth:'280px'}).setHTML(html)).addTo(this.map);
        this.markers.push(marker);
      });
      const hasRoute=addRouteLayer(this.map,plan.routeGeometry||plan.geometry);
      if(!hasRoute && (plan.stops&&plan.stops.length))this.setStatus('路線 geometry 未提供：只顯示真實底圖及 markers，沒有繪製假路線。',true);
      else this.setStatus(hasRoute?'已繪製 provider 路線 geometry':'已顯示 shortlisted markers');
      fitAll(this.map,stops,plan.routeGeometry||plan.geometry); this.resize();
      if(hasRoute && originForFit){
        const el=makeMarker('出','origin');
        const marker=new maplibregl.Marker({element:el}).setLngLat([Number(originForFit.lon),Number(originForFit.lat)]).setPopup(new maplibregl.Popup({offset:24,maxWidth:'280px'}).setHTML(`<strong>${esc(originForFit.label||originForFit.address||'出發點')}</strong>`)).addTo(this.map);
        this.markers.push(marker);
      }
    }
    diagnostics(){
      const canvas=this.map?this.map.getCanvas():null;
      return {loaded:!!(this.map&&this.map.loaded()),canvasWidth:canvas?canvas.clientWidth:0,canvasHeight:canvas?canvas.clientHeight:0,hasRouteLayer:!!(this.map&&this.map.getLayer('viewing-route-line')),hasRouteSource:!!(this.map&&this.map.getSource('viewing-route')),markerCount:this.markers.length,loadError:this.loadError};
    }
  }
  window.Gift22ViewingRouteMap=ViewingRouteMap;
  window.Gift22ViewingRouteMapUtils={flattenGeometry,routeFeature};
})();
