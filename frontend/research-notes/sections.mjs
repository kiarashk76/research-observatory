import MarkdownIt from 'markdown-it';
import texmath from 'markdown-it-texmath';

const parser=new MarkdownIt().use(texmath,{delimiters:['dollars','brackets']});

// Copy boundaries intentionally use the next heading of the SAME level.
// Agent edit permissions use their own, stricter section boundaries.
export function markdownSections(markdown){
 const lines=markdown.split('\n'),tokens=parser.parse(markdown,{}),ignored=new Set();
 for(const token of tokens)if(['fence','code_block','math_block','math_block_eqno'].includes(token.type)&&token.map)
  for(let i=token.map[0];i<token.map[1];i++)ignored.add(i);
 const stacks=[],active=[];
 for(let i=0;i<=lines.length;i++){
  stacks[i]=[...active];
  if(ignored.has(i))continue;
  const open=/^:::notes-align (left|center|right)\s*$/.exec(lines[i]||'');
  if(open)active.push(open[1]);
  else if(/^:::end-notes-align\s*$/.test(lines[i]||''))active.pop();
 }
 const headings=tokens.filter(t=>t.type==='heading_open'&&t.level===0&&t.map).map(t=>{
  const index=tokens.indexOf(t),inline=tokens[index+1];
  return {level:Number(t.tag.slice(1)),start:t.map[0],title:inline?.content||lines[t.map[0]]};
 });
 return headings.map((heading,index)=>{
  const end=headings.slice(index+1).find(h=>h.level===heading.level)?.start??lines.length;
  // Restore layout containers that cross a section boundary, keeping copied
  // quotes, tables, equations and alignment valid as a standalone fragment.
  const prefix=stacks[heading.start].map(a=>`:::notes-align ${a}\n\n`).join('');
  const suffix=stacks[end].map(()=>'\n\n:::end-notes-align').join('');
  // A section may contain only one side of a two-column block. Flatten its
  // column markers for portable Markdown instead of exporting a broken row.
  const body=lines.slice(heading.start,end).filter((line,index)=>ignored.has(heading.start+index)||!/^:::(?:end-)?notes-columns?(?: \d{2})?\s*$/.test(line)).join('\n');
  return {...heading,end,markdown:prefix+body.trimEnd()+suffix+'\n'};
 });
}
