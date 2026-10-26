const $ = id => document.getElementById(id);
const format = value => new Intl.DateTimeFormat('it-IT', {timeZone:'Europe/Rome', day:'2-digit', month:'2-digit', hour:'2-digit', minute:'2-digit', timeZoneName:'short'}).format(new Date(value));
let busy = false;
let latest = null;
let images = [];
let selectedId = null;

function selectedImage() {
  return selectedId === null ? latest : images.find(image => image.id === selectedId);
}

function neighbors() {
  const current = selectedImage();
  if (!current) return {};
  const timestamp = Date.parse(current.captured_at);
  return {
    previous: images.find(image => Date.parse(image.captured_at) < timestamp),
    next: images.slice().reverse().find(image => Date.parse(image.captured_at) > timestamp)
  };
}

function render() {
  const current = selectedImage();
  const live = selectedId === null;
  $('latest').hidden = !current;
  $('empty').hidden = !!current;
  $('download').hidden = !current;
  if (current) {
    if ($('latest').dataset.id !== current.id) {
      $('latest').src = current.url;
      $('latest').dataset.id = current.id;
    }
    $('latest').alt = (live ? 'Ultimo scatto: ' : 'Scatto storico: ') + format(current.captured_at);
    $('timestamp').textContent = format(current.captured_at);
    $('state').textContent = live
      ? (Date.now()-Date.parse(current.captured_at)>180000 ? '● Nessuno scatto recente' : '● Scatto recente')
      : '◷ Storico';
    $('download').href = current.url+'?download=true';
  } else {
    $('state').textContent = 'In attesa di immagini';
    $('timestamp').textContent = '—';
  }
  const {previous, next} = neighbors();
  $('previous').disabled = !previous;
  $('next').disabled = !next;
  $('go-live').disabled = !latest;
  $('go-live').setAttribute('aria-pressed', String(live));
  $('view-mode').textContent = live ? 'Ultima immagine · aggiornamento ogni 30 secondi' : 'Scatto orario · visualizzazione storico';
  for (const card of $('history').children) {
    card.setAttribute('aria-pressed', String(card.dataset.id === selectedId));
  }
}

function selectImage(id) {
  selectedId = id;
  render();
}

async function json(url) { const response = await fetch(url, {cache:'no-store'}); if (!response.ok) throw new Error('Servizio non disponibile. Nuovo tentativo tra 30 secondi.'); return response.json(); }
async function refresh() {
  if (busy || !$('camera').value) return;
  busy = true;
  const camera = $('camera').value;
  const base = `/api/v1/cameras/${encodeURIComponent(camera)}`;
  try {
    const [status, history] = await Promise.all([json(base+'/status'), json(base+'/history')]);
    if (camera !== $('camera').value) return;
    $('error').textContent = '';
    latest = status.latest;
    images = history.images;
    if (selectedId !== null && !images.some(image => image.id === selectedId)) {
      selectedId = null;
      $('error').textContent = 'Lo scatto selezionato non è più nello storico. Visualizzazione tornata in Live.';
    }
    $('history-empty').hidden = images.length>0;
    $('history').replaceChildren(...images.map(item => {
      const card=document.createElement('button'); card.type='button'; card.className='card'; card.dataset.id=item.id;
      card.setAttribute('aria-label', 'Visualizza scatto del '+format(item.captured_at));
      card.addEventListener('click', () => selectImage(item.id));
      const img=document.createElement('img'); img.src=item.url; img.alt=''; img.loading='lazy';
      const time=document.createElement('time'); time.dateTime=item.captured_at; time.textContent=format(item.captured_at);
      card.append(img,time); return card;
    }));
    render();
  } catch(error) { $('error').textContent=error.message; $('state').textContent='● Connessione interrotta'; }
  finally { busy=false; if(camera !== $('camera').value) refresh(); }
}
async function init(){
  try { const data=await json('/api/v1/cameras'); $('camera').replaceChildren(...data.cameras.map(camera=>new Option(camera.id,camera.id))); await refresh(); }
  catch(error){$('error').textContent=error.message;}
}
$('previous').addEventListener('click', () => { const image=neighbors().previous; if (image) selectImage(image.id); });
$('next').addEventListener('click', () => { const image=neighbors().next; if (image) selectImage(image.id); });
$('go-live').addEventListener('click', () => { selectImage(null); refresh(); });
$('camera').addEventListener('change',()=>{
  selectedId=null; latest=null; images=[];
  $('latest').dataset.id=''; $('history').replaceChildren();
  render(); refresh();
});
init(); setInterval(()=>{$('camera').options.length ? refresh() : init();},30000);
