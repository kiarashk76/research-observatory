'use strict';
const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');
const source=fs.readFileSync(require('node:path').join(__dirname,'../labcore/dashboard/app.js'),'utf8').split("document.addEventListener('click'")[0];
function run(code){
 const context=vm.createContext({console,URL,document:{querySelector:()=>({})}});
 vm.runInContext(source+`\ndb={digests:[],files:[]};projectId='p';\n`+code,context);
}
test('library defaults to digests while other views retain their defaults',()=>run(`
 if(defaultFilter('library')!=='digests'||defaultFilter('work')!=='all')throw Error('Wrong defaults');
`));
test('topics show paper summaries inline with closed details, safely escaped',()=>run(`
 db.digests=[{id:'d',project:'p',title:'Background round',period_end:'2026-11-04',text:'Coverage note',summary:'Issue summary',sections:[
 {topic:'Transformers',papers:[{title:'A <script> paper',url:'https://example.org/paper',summary:'Visible summary',details:'**Methods:** Detail body'}]},
 {topic:'Non-stationary RL',papers:[]},{topic:'Neuro-inspired AI',papers:[]},{topic:'Continual learning',papers:[]}]}];
 const html=digestShelf();
 for(const name of ['Transformers','Non-stationary RL','Neuro-inspired AI','Continual learning','Visible summary','Paper details'])if(!html.includes(name))throw Error('Missing '+name);
 if(html.includes('<script>'))throw Error('Unescaped paper title');
 if(/<details[^>]*data-digest-paper=[^>]* open/.test(html))throw Error('Details should start closed');
 if(html.includes('data-type="digest"'))throw Error('Digest still requires opening a drawer');
 expandedDigestPapers.add(digestPaperKey(db.digests[0],'Transformers',db.digests[0].sections[0].papers[0]));
 if(!/<details[^>]*data-digest-paper=[^>]* open/.test(digestShelf()))throw Error('Expanded state lost');
`));
test('search uses paper contents and respects workspace scope',()=>run(`
 db.digests=[{id:'d',project:'p',title:'Issue',period_end:'2026-11-04',text:'Notes',sections:[
 {topic:'Transformers',papers:[{title:'Attention',url:'https://example.org/a',summary:'Induction circuits',details:'Methods'}]},
 {topic:'Neuro-inspired AI',papers:[{title:'Cognition',url:'https://example.org/b',summary:'Cognitive adaptation',details:'Methods'}]}]},
 {id:'foreign',project:'other',topic:'Secret',text:'Another project'}];
 query='cognitive';const html=digestShelf();
 if(!html.includes('Cognition')||html.includes('Attention')||html.includes('Another project'))throw Error('Incorrect search or scope');
 query='transformers';if(!digestShelf().includes('Attention'))throw Error('Topic search should include its papers');
`));
test('older unstructured issues remain readable directly on the page',()=>run(`
 db.digests=[{id:'old',project:'p',title:'Legacy round',topic:'Topic',period_end:'2026-11-01',summary:'Legacy summary',text:'## Legacy full content'}];
 if(!digestShelf().includes('Legacy full content'))throw Error('Legacy digest hidden');
`));
