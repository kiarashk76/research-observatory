import fs from 'node:fs';
import path from 'node:path';
import {chromium} from 'playwright';
import {renderNotes} from './render.mjs';
const input=JSON.parse(fs.readFileSync(0,'utf8'));
const escape=text=>String(text).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const images=input.images||{};
const body=renderNotes(input.markdown,input.project,source=>images[source]||'');
const html='<!doctype html><html><head><meta charset="utf-8"><link rel="stylesheet" href="http://notes.local/notes-assets/notes-editor.css"><style>@page{size:A4;margin:20mm 18mm}body{font-family:Arial,sans-serif;color:#172033;font-size:11pt;line-height:1.65}.notes-paper{max-width:none}.notes-paper h1{font-size:24pt}.notes-paper h2{font-size:18pt;break-after:avoid}.notes-paper h3{break-after:avoid}.notes-paper figure,.notes-paper blockquote{break-inside:avoid}.notes-paper img{max-height:210mm}pre{white-space:pre-wrap}a{color:#31509c}</style></head><body><header><small>'+escape(input.title)+' · Research Notes · Revision '+input.revision+'</small></header><main class="notes-paper">'+body+'</main></body></html>';
const browser=await chromium.launch({headless:true,args:['--disable-gpu','--disable-dev-shm-usage','--no-sandbox']});
try{
 const page=await browser.newPage({javaScriptEnabled:false});
 await page.route('**/*',async route=>{
  const url=new URL(route.request().url());
  if(url.origin==='http://notes.local'&&url.pathname==='/')return route.fulfill({contentType:'text/html',body:html});
  if(url.origin==='http://notes.local'&&url.pathname.startsWith('/notes-assets/')){
   const target=path.resolve(input.assets,url.pathname.slice('/notes-assets/'.length));
   if(target.startsWith(path.resolve(input.assets)+path.sep)&&fs.existsSync(target))return route.fulfill({path:target});
  }
  return route.abort();
 });
 await page.goto('http://notes.local/',{waitUntil:'networkidle'});
 await page.evaluate(()=>document.fonts.ready);
 await page.pdf({path:input.output,format:'A4',printBackground:true,preferCSSPageSize:true});
}finally{await browser.close();}
