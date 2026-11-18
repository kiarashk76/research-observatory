// Run with node tests/check_digest_ui.js. No server, browser, or live records.
'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const nodes = new Map();
const document = {querySelector(selector){
 if(!nodes.has(selector))nodes.set(selector,{innerHTML:'',open:false,showModal(){this.open=true;}});
 return nodes.get(selector);
}};
const context=vm.createContext({document,console,URL,Date});
const app=fs.readFileSync('labcore/dashboard/app.js','utf8').split("document.addEventListener('click'")[0];
vm.runInContext(app,context);
vm.runInContext(`
db={projects:[{id:'p',name:'Reading'}],digests:[
 {id:'old',project:'p',topic:'Adaptation',title:'Earlier issue',period_start:'2026-10-25',period_end:'2026-10-31',summary:'Prior issue',text:'Older notes',version:1},
 {id:'new',project:'p',topic:'Adaptation',title:'<img src=x onerror=alert(1)>',period_start:'2026-11-01',period_end:'2026-11-07',summary:'A quiet week',text:'## Sources\\n[Paper](https://example.org/paper)\\n\\n| Method | Caveat |\\n| --- | --- |\\n| A | Abstract only |\\n\\n<script>alert(1)</script>',version:2},
 {id:'other',project:'other',topic:'Other project',title:'PRIVATE',period_end:'2026-11-07'}
],wakeups:[],reports:[],papers:[],paper_links:[],decisions:[],files:[]};
projectId='p';page='library';filter='digests';renderResults();
`,context);
let html=nodes.get('#results').innerHTML;
assert(html.includes('Your weekly reading shelf'));
assert(html.indexOf('data-id="new"')<html.indexOf('data-id="old"'));
assert(!html.includes('PRIVATE'));
assert(!html.includes('<img src=x'));
assert(html.includes('&lt;img'));
vm.runInContext("query='2026-10-25';renderResults()",context);
html=nodes.get('#results').innerHTML;
assert(html.includes('Earlier issue'));
assert(!html.includes('data-id="new"'));
vm.runInContext("detail('digest','new')",context);
html=nodes.get('#detail-content').innerHTML;
assert(html.includes('<table>'));
assert(html.includes('href="https://example.org/paper"'));
assert(html.includes('Abstract only'));
assert(!html.includes('<script>'));
assert(nodes.get('#detail').open);
vm.runInContext("db.digests=[];query='';renderResults()",context);
assert(nodes.get('#results').innerHTML.includes('Your first digest will appear here'));
console.log('Digest UI: ordering, project boundaries, search, empty state, Markdown tables, links, and escaping passed.');
vm.runInContext(`
db.studies=[{project:'p',study_id:'s',title:'Study',question:'Question',paper_pdf:'reports/paper.pdf',detailed_pdf:'missing.pdf'}];
db.files=[{id:'pdf1',project:'p',path:'reports/paper.pdf'},{id:'other',project:'other',path:'reports/paper.pdf'}];
db.tasks=[];db.sessions=[];
`,context);
html=vm.runInContext('researchStory()',context);
assert(html.includes('Download all PDFs (1)'));
assert(html.includes('Download selected (0)'));
assert(html.includes('data-story-pdf="pdf1"'));
assert(!html.includes('data-story-pdf="other"'));
assert(html.includes('Not available yet'));
vm.runInContext("selectedStoryPdfs.add('pdf1')",context);
html=vm.runInContext('researchStory()',context);
assert(html.includes('Download selected (1)'));
assert(html.includes('checked'));
vm.runInContext('db.files=[]',context);
html=vm.runInContext('researchStory()',context);
assert(html.includes('Download selected (0)'));
assert(html.includes('data-download-story-pdfs="all" disabled'));
console.log('Story PDF selection: project boundaries, unavailable PDFs, counts, and stale selections passed.');
