'use strict';
$('read-pdf').onclick=async()=>{
  const file=$('pdf-file').files[0];
  if(!file || file.size>5*1024*1024){$('pdf-status').textContent='Choose a PDF no larger than 5 MB.';return;}
  $('read-pdf').disabled=true;$('pdf-review').replaceChildren();$('pdf-status').textContent='Reading PDF…';
  try{
    const response=await fetch(api+'/v1/pdf/read',{method:'POST',headers:{Authorization:'Bearer '+token,'Content-Type':'application/pdf'},body:file,signal:AbortSignal.timeout(75000)});
    const data=await response.json();if(!response.ok)throw Error(data.error||'PDF reading failed.');
    if(!token)return;
    for(const item of data.items){
      const section=document.createElement('details'),heading=document.createElement('summary');
      heading.textContent=`${item.number||''} · ${item.code} · ${item.name||''}`;section.append(heading);
      const wrap=document.createElement('div');wrap.className='table-scroll';const table=document.createElement('table');
      const head=table.createTHead().insertRow();
      for(const title of ['Description','No','L','B','D','CF','Quantity','Remark','Section']){const th=document.createElement('th');th.textContent=title;head.append(th);}
      const body=table.createTBody();
      for(const row of item.rows||[]){const tr=body.insertRow();for(const key of ['description','no','l','b','d','cf','quantity','remark','entry_section'])tr.insertCell().textContent=row[key]??'';}
      wrap.append(table);section.append(wrap);
      if(item.total_warning){const warning=document.createElement('p');warning.textContent=item.total_warning;section.append(warning);}
      $('pdf-review').append(section);
    }
    $('pdf-status').textContent=`${data.items.length} items extracted. ${data.notice}`;
  }catch(error){$('pdf-status').textContent=error.message;}
  finally{$('read-pdf').disabled=!token;}
};
