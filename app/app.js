'use strict';
const $ = id => document.getElementById(id);
let token = '', api = '', result = null;
function switchPage(emb) {
  $('title').textContent = emb ? 'E-MB Valuation' : 'SECURE Automation';
  $('secure-panel').hidden = emb; $('valuation-panel').hidden = !emb;
  for (const [id, active] of [['emb-tab',emb],['secure-tab',!emb]]) {
    $(id).classList.toggle('active', active); $(id).setAttribute('aria-pressed', String(active));
  }
}
$('emb-tab').onclick = () => switchPage(true);
$('secure-tab').onclick = () => switchPage(false);
$('theme').onclick = () => { const dark = document.body.classList.toggle('dark'); $('theme').textContent = dark ? 'Light mode' : 'Dark mode'; };
function invalidate(){ result=null; $('export').disabled=true; $('totals').textContent='Recalculate after changing data.'; }
function addRow() {
  const row = document.createElement('tr'); const serial=document.createElement('td'); row.append(serial);
  for (const [key,label] of [['code','Item code'],['name','Activity'],['remaining','Remaining quantity'],['quantity','New quantity'],['rate','Unit rate'],['labourRate','Labour rate']]) {
    const cell=document.createElement('td'), input=document.createElement('input'); input.dataset.key=key;
    input.setAttribute('aria-label',label); input.maxLength=key==='name'?1000:128; input.oninput=invalidate; cell.append(input); row.append(cell);
  }
  row.append(document.createElement('td'),document.createElement('td'));
  const action=document.createElement('td'), remove=document.createElement('button'); remove.textContent='Delete';
  remove.onclick=()=>{row.remove();renumber();invalidate();};action.append(remove);row.append(action);$('rows').append(row);renumber();invalidate();
}
function renumber(){[...$('rows').rows].forEach((r,i)=>r.cells[0].textContent=i+1);}
function payload(){return {workCode:$('work-code').value, personDays:$('days').value,dailyWage:$('wage').value,
  rows:[...$('rows').rows].map(row=>Object.fromEntries([...row.querySelectorAll('input')].map(i=>[i.dataset.key,i.value])))};}
async function request(path, body){
  const response=await fetch(api+path,{method:body?'POST':'GET',headers:{Authorization:'Bearer '+token,...(body?{'Content-Type':'application/json'}:{})},body:body?JSON.stringify(body):undefined,signal:AbortSignal.timeout(20000)});
  const data=await response.json();if(!response.ok)throw Error(data.error||'Service unavailable.');return data;
}
$('add').onclick=addRow;
for(const id of ['days','wage','work-code'])$(id).oninput=invalidate;
$('calculate').onclick=async()=>{
  $('errors').textContent='';$('calculate').disabled=true;invalidate();
  try{
    const snapshot=payload();$('editor').disabled=true;const calculation=await request('/v1/valuation',snapshot);
    result={input:snapshot,calculation};calculation.rows.forEach((r,i)=>{const cells=$('rows').rows[i].cells;cells[7].textContent=r.amount;cells[8].textContent=r.labourAmount;});
    $('totals').textContent=`Total ₹${calculation.total} · Labour ₹${calculation.labourTotal} · Maximum labour ₹${calculation.maximumLabour} · Per person-day ${calculation.perPersonDay===null?'not available':'₹'+calculation.perPersonDay}`;
    if(calculation.overLimit)$('errors').textContent='Labour amount exceeds the person-day wage limit. Review before use.';
    $('export').disabled=false;
  }catch(error){$('errors').textContent=error.message;}finally{$('editor').disabled=!token;$('calculate').disabled=false;}
};
$('export').onclick=()=>{if(!result)return;const link=document.createElement('a');link.href=URL.createObjectURL(new Blob([JSON.stringify(result,null,2)],{type:'application/json'}));link.download='secure-valuation.json';link.click();setTimeout(()=>URL.revokeObjectURL(link.href),1000);};
$('signout').onclick=()=>{token='';$('rows').replaceChildren();invalidate();$('editor').disabled=true;$('signout').hidden=true;$('access').textContent='Signed out. Sign in again to continue.';};
async function boot(){
  try{
    const config=await (await fetch('config.json',{cache:'no-store'})).json();
    if(!config.apiBase){$('service').textContent='Framework preview — hosted service not connected';$('service-detail').textContent='The browser workspace is installed on the website. Google web sign-in, hosting and live portal verification must be completed before online automation is enabled.';return;}
    const url=new URL(config.apiBase);if(url.protocol!=='https:')throw Error('HTTPS service required.');api=url.href.replace(/\/$/,'');
    $('service').textContent='Connecting to hosted service…';$('service-detail').textContent='The free preview server may take about a minute to wake up.';
    const healthResponse=await fetch(api+'/health',{signal:AbortSignal.timeout(90000)});if(!healthResponse.ok)throw Error('Hosted service unavailable.');
    const health=await healthResponse.json();if(!health.licenseConfigured)throw Error('Hosted sign-in is not configured.');
    $('service').textContent='Valuation service connected';$('service-detail').textContent='Portal automation and PDF reading are not enabled in this framework release.';
    if(!config.googleClientId){$('access').textContent='Backend connected. Google web sign-in configuration is pending; calculations remain locked until server-verified sign-in is available.';return;}
    const script=document.createElement('script');script.src='https://accounts.google.com/gsi/client';script.async=true;
    script.onload=()=>{google.accounts.id.initialize({client_id:config.googleClientId,callback:async credential=>{
      try{token=credential.credential;const state=await request('/v1/access');$('access').textContent=`Access: ${state.access}. Server verified.`;$('editor').disabled=false;$('signout').hidden=false;if(!$('rows').rows.length)addRow();}
      catch(error){token='';$('editor').disabled=true;$('access').textContent=error.message;}
    }});google.accounts.id.renderButton($('google-button'),{theme:'outline',size:'large'});};
    script.onerror=()=>{$('access').textContent='Google sign-in could not load. Please retry.';};document.head.append(script);
  }catch(error){$('service').textContent='Hosted service unavailable';$('service-detail').textContent=error.message;}
}
boot();
