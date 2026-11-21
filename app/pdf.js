(()=>{
'use strict';
const $ = id => document.getElementById(id);
let estimate = null, dirty = false, copiedRows = [], revision = 0;
const columns = [['serial','Sl No'],['description','Description'],['no','No'],['l','L'],['b','B'],['d','D'],['cf','CF'],['quantity','Quantity'],['remark','Remark']];
function markChanged(){revision+=1;dirty=true;$('save-review').disabled=!window.secureApp?.signedIn();$('review-state').textContent='Unsaved changes';}
function addMeasurementRow(itemIndex){
  const rows=estimate.items[itemIndex].rows||(estimate.items[itemIndex].rows=[]);rows.push({serial:String(rows.length+1),description:'',no:'',l:'',b:'',d:'',cf:'',quantity:'',remark:'',head:''});renderSecureEstimate(estimate);markChanged();
}
function renderSecureEstimate(data){
  const keepDirty=data===estimate&&dirty;const opened=new Set([...$('pdf-review').querySelectorAll('details[open]')].map(node=>node.dataset.itemIndex));estimate=data;window.secureEstimate=data;const host=$('pdf-review');host.replaceChildren();
  for(const [itemIndex,item] of (data.items||[]).entries()){
    const section=document.createElement('details');section.className='estimate-item';section.dataset.itemIndex=String(itemIndex);section.open=opened.has(String(itemIndex));section.dataset.search=`${item.code||''} ${item.name||''}`.toLocaleLowerCase();
    const summary=document.createElement('summary');summary.textContent=`${item.number||itemIndex+1}. ${item.code||''} — ${item.name||''} · ${item.rows?.length||0} measurement rows`;section.append(summary);
    if(item.warning||item.total_warning){const warning=document.createElement('p');warning.className='item-warning';warning.textContent=item.warning||item.total_warning;section.append(warning);}
    const scroller=document.createElement('div');scroller.className='table-scroll';const table=document.createElement('table'),thead=table.createTHead(),header=thead.insertRow();
    for(const [,title] of columns){const th=document.createElement('th');th.textContent=title;header.append(th);}const actionHeader=document.createElement('th');actionHeader.textContent='Action';header.append(actionHeader);
    const checkHeading=document.createElement('th');checkHeading.textContent='Check';header.prepend(checkHeading);
    const tbody=table.createTBody();
    for(const [rowIndex,row] of (item.rows||[]).entries()){
      const tr=tbody.insertRow();tr.dataset.itemIndex=itemIndex;tr.dataset.rowIndex=rowIndex;if(row.entry_section==='deduction'||Number(row.quantity)<0)tr.className='deduction-row';
      const checkCell=tr.insertCell(),check=document.createElement('input');check.type='checkbox';check.setAttribute('aria-label',`Select row ${rowIndex+1}`);checkCell.append(check);
      for(const [key,label] of columns){const cell=tr.insertCell();if(key==='serial'){cell.textContent=row[key]||String(rowIndex+1);continue;}const input=document.createElement('input');input.type='text';input.maxLength=key==='description'||key==='remark'?1000:128;input.dataset.key=key;input.setAttribute('aria-label',`${label} for item ${item.code}, row ${rowIndex+1}`);input.value=row[key]??'';input.oninput=()=>{row[key]=input.value;markChanged();};cell.append(input);}
      const action=tr.insertCell(),remove=document.createElement('button');remove.type='button';remove.textContent='Delete row';remove.onclick=()=>{estimate.items[itemIndex].rows.splice(rowIndex,1);renderSecureEstimate(estimate);markChanged();};action.append(remove);
    }
    const controls=document.createElement('div');controls.className='actions';
    const selected=()=>[...tbody.rows].flatMap((tr,index)=>tr.querySelector('input[type=checkbox]').checked?[index]:[]);
    const action=(title,callback)=>{const button=document.createElement('button');button.type='button';button.textContent=title;button.onclick=callback;controls.append(button);};
    action('Copy all',()=>{copiedRows=structuredClone(item.rows||[]);$('review-state').textContent=`${copiedRows.length} rows copied.`;});
    action('Copy checked',()=>{copiedRows=selected().map(index=>structuredClone(item.rows[index]));$('review-state').textContent=`${copiedRows.length} rows copied.`;});
    action('Paste copied rows',()=>{if(!copiedRows.length)return;(item.rows||(item.rows=[])).push(...structuredClone(copiedRows));renderSecureEstimate(estimate);markChanged();});
    action('Delete checked rows',()=>{const indices=new Set(selected());if(!indices.size)return;item.rows=item.rows.filter((row,index)=>!indices.has(index));renderSecureEstimate(estimate);markChanged();});
    section.append(controls);scroller.append(table);section.append(scroller);const add=document.createElement('button');add.type='button';add.textContent='Add measurement row';add.onclick=()=>addMeasurementRow(itemIndex);section.append(add);host.append(section);
  }
  dirty=keepDirty;$('save-review').disabled=!dirty||!window.secureApp?.signedIn();$('review-state').textContent=dirty?'Unsaved changes':data.items?.length?'Ready for review.':'No estimate items found.';
  const search=$('review-search').value.trim().toLocaleLowerCase();for(const section of host.querySelectorAll('.estimate-item'))section.hidden=Boolean(search&&!section.dataset.search.includes(search));
}
window.renderSecureEstimate=renderSecureEstimate;
$('review-search').oninput=()=>{const search=$('review-search').value.trim().toLocaleLowerCase();for(const section of $('pdf-review').querySelectorAll('.estimate-item'))section.hidden=Boolean(search&&!section.dataset.search.includes(search));};
$('save-review').onclick=async()=>{
  if(!estimate||!dirty)return;const savingEstimate=estimate,savingRevision=revision;$('save-review').disabled=true;$('review-state').textContent='Calculating measurement quantities…';
  try{
    const rows=[];for(const item of estimate.items)for(const row of item.rows||[])rows.push(row);
    const calculated=await window.secureApp.request('/v1/measurements/calculate',{rows:structuredClone(rows)});if(estimate!==savingEstimate||revision!==savingRevision){$('review-state').textContent='Measurements changed during calculation. Save again to include your latest edits.';$('save-review').disabled=false;return;}if(!Array.isArray(calculated.rows)||calculated.rows.length!==rows.length)throw Error('Calculation returned incomplete rows. Your edits remain open.');let index=0;
    for(const item of estimate.items){let total=0;for(const row of item.rows||[]){const value=calculated.rows[index++];row.quantity=value.quantity;row.deduction=value.deduction;row.entry_section=value.entry_section;if(value.warning)row.warning=value.warning;else delete row.warning;total+=Number(String(row.quantity||'0').replaceAll(',',''))||0;}item.total=total.toFixed(4);}
    estimate.savedAt=new Date().toISOString();if(!window.secureApp.persistEstimate(estimate))throw Error('Changes remain open but could not be saved. Keep this page open.');dirty=false;renderSecureEstimate(estimate);window.secureApp.refreshOverview(estimate);$('review-state').textContent='Changes saved in this browser.';window.secureApp.log('Saved reviewed estimate measurements');
  }catch(error){$('review-state').textContent=error.message;$('save-review').disabled=false;if(error.status===402)await window.secureApp.refreshAccess();}
};
$('pdf-file').addEventListener('change',()=>{const file=$('pdf-file').files[0];$('read-pdf').disabled=!file||!window.secureApp?.canUse();if(file){$('pdf-status').textContent=file.name;$('status-title').textContent='PDF selected';$('status-detail').textContent=window.secureApp?.canUse()?'Select Read PDF to review this estimate.':'Sign in and activate access to read this PDF.';}});
$('read-pdf').onclick=async()=>{
  const file=$('pdf-file').files[0];if(!file||!file.name.toLowerCase().endsWith('.pdf')||file.size>25*1024*1024){$('pdf-status').textContent='Choose a PDF no larger than 25 MB.';return;}
  $('read-pdf').disabled=true;$('pdf-review').replaceChildren();$('pdf-status').textContent='Reading every page with the desktop trained reader…';$('status-title').textContent='Reading estimate PDF';$('status-detail').textContent=file.name;window.secureApp.setProgress(null,'Reading every page…');
  try{
    const response=await fetch(api+'/v1/pdf/read',{method:'POST',headers:{Authorization:'Bearer '+token,'Content-Type':'application/pdf','X-Reader-Mode':$('reader-mode').value},body:file,signal:AbortSignal.timeout(75000)});
    const responseText=await response.text();let result;try{result=JSON.parse(responseText);}catch{throw Error(`PDF service returned HTTP ${response.status}. Please retry; no measurements were imported.`);}if(!response.ok){const error=Error(result.error||'PDF reading failed.');error.status=response.status;throw error;}if(!token)return;
    estimate={id:globalThis.crypto?.randomUUID?.()||`${Date.now()}-${Math.random().toString(36).slice(2)}`,fileName:file.name,savedAt:new Date().toISOString(),items:result.items,notice:result.notice};
    renderSecureEstimate(estimate);window.secureApp.persistEstimate(estimate);window.secureApp.refreshOverview(estimate);window.secureApp.setProgress(100,`${result.items.length} items read. Review every measurement before entry.`);
    $('pdf-status').textContent=`${result.items.length} items read. ${result.notice}`;$('status-title').textContent='PDF reading complete';$('status-detail').textContent=`${result.items.length} item codes · ${result.items.reduce((sum,item)=>sum+(item.rows||[]).length,0)} measurement rows`;
    window.secureApp.setPage('review');window.secureApp.log(`Read ${file.name}: ${result.items.length} items`);
  }catch(error){$('pdf-status').textContent=error.message;$('status-title').textContent='PDF reading needs attention';$('status-detail').textContent=error.message;window.secureApp.setProgress(0,error.message);if(error.status===402)await window.secureApp.refreshAccess();}
  finally{$('read-pdf').disabled=!window.secureApp?.canUse()||!$('pdf-file').files?.length;}
};
})();
