import MarkdownIt from 'markdown-it';
import texmath from 'markdown-it-texmath';
import katex from 'katex';
import {readColumns} from './columns.mjs';
import {readFontSize} from './font-syntax.mjs';
import {readUnderline} from './underline-syntax.mjs';

// Preserve fenced code while accepting the LaTeX delimiters used by ChatGPT.
export function normalizeMath(source){
 return String(source||'').split(/(```[\s\S]*?(?:```|$)|~~~[\s\S]*?(?:~~~|$)|`[^`\n]*`)/g).map((part,i)=>i%2?part:part.replace(/\\\[([\s\S]*?)\\\]/g,(_,math)=>'\n$$\n'+math.trim()+'\n$$\n').replace(/\\\(([\s\S]*?)\\\)/g,(_,math)=>'$'+math.trim()+'$')).join('');
}
export function assetUrl(path,project){
 if(!/^notes\/research-notes-assets\/[a-f0-9]{32}\.(png|jpg)$/.test(path||''))return '';
 return '/api/research-notes/asset?'+new URLSearchParams({project,asset:path.split('/').pop().split('.')[0]});
}
// Parse only our three fixed alignment values, never arbitrary HTML/CSS.
export function readAlignment(source){
 const opening=/^:::notes-align (left|center|right)[ \t]*\n/.exec(source);
 if(!opening)return null;
 const lines=source.slice(opening[0].length).split(/(?<=\n)/);let offset=opening[0].length,depth=1,fence=null;
 for(const line of lines){
  const text=line.replace(/[\r\n]+$/,'');
  const code=/^ {0,3}(`{3,}|~{3,})(.*)$/.exec(text);
  if(code){if(!fence)fence=[code[1][0],code[1].length];else if(fence[0]===code[1][0]&&code[1].length>=fence[1]&&!code[2].trim())fence=null;}
  else if(!fence){
   if(/^:::notes-align (left|center|right)[ \t]*$/.test(text))depth++;
   if(/^:::end-notes-align[ \t]*$/.test(text)&&!--depth)return {align:opening[1],body:source.slice(opening[0].length,offset),raw:source.slice(0,offset+line.length)};
  }
  offset+=line.length;
 }
 return null;
}
export function renderNotes(source,project,resolveImage){
 const md=new MarkdownIt({html:false,linkify:true,breaks:false}).use(texmath,{engine:katex,delimiters:'dollars',katexOptions:{trust:false,throwOnError:false,strict:'warn',maxExpand:1000}});
 md.inline.ruler.before('html_inline','notes_underline',(state,silent)=>{
  const underline=readUnderline(state.src.slice(state.pos));if(!underline)return false;
  if(!silent){
   state.push('notes_underline_open','u',1);
   const children=[];state.md.inline.parse(underline.body,state.md,state.env,children);
   for(const child of children){child.level+=state.level;state.tokens.push(child);}
   state.push('notes_underline_close','u',-1);
  }
  state.pos+=underline.raw.length;return true;
 });
 md.inline.ruler.before('html_inline','notes_font_size',(state,silent)=>{
  const span=readFontSize(state.src.slice(state.pos));if(!span)return false;
  if(!silent){
   const open=state.push('notes_font_size_open','span',1);open.attrs=[['data-notes-font-size',String(span.size)],['style','font-size:'+span.size+'px']];
   const children=[];state.md.inline.parse(span.body,state.md,state.env,children);
   for(const child of children){child.level+=state.level;state.tokens.push(child);}
   state.push('notes_font_size_close','span',-1);
  }
  state.pos+=span.raw.length;return true;
 });
 md.block.ruler.before('fence','notes_columns',(state,startLine,endLine,silent)=>{
  const source=state.getLines(startLine,endLine,0,false),block=readColumns(source);
  if(!block)return false;if(silent)return true;
  const open=state.push('notes_columns_open','div',1);open.attrs=[['data-notes-columns',''],['data-notes-column-width',String(block.width)],['style',`--notes-column-left:${block.width}fr;--notes-column-right:${100-block.width}fr`]];
  for(const body of block.columns){
   const column=state.push('notes_column_open','div',1);column.attrs=[['data-notes-column','']];
   state.md.block.parse(body,state.md,state.env,state.tokens);
   state.push('notes_column_close','div',-1);
  }
  state.push('notes_columns_close','div',-1);
  state.line=startLine+block.raw.split('\n').length-(block.raw.endsWith('\n')?1:0);return true;
 });
 md.block.ruler.before('fence','notes_alignment',(state,startLine,endLine,silent)=>{
  const source=state.getLines(startLine,endLine,0,false),block=readAlignment(source);
  if(!block)return false;if(silent)return true;
  const open=state.push('notes_align_open','div',1);open.attrs=[['data-notes-align',block.align]];
  state.md.block.parse(block.body,state.md,state.env,state.tokens);
  state.push('notes_align_close','div',-1);
  state.line=startLine+block.raw.split('\n').length-(block.raw.endsWith('\n')?1:0);return true;
 });
 md.renderer.rules.image=(tokens,index)=>{
  const token=tokens[index],original=token.attrGet('src'),url=resolveImage?resolveImage(original):assetUrl(original,project);
  if(!url)return '<p class="notes-missing-image">Figure unavailable: '+md.utils.escapeHtml(token.content||original||'image')+'</p>';
  return '<figure><img src="'+md.utils.escapeHtml(url)+'" alt="'+md.utils.escapeHtml(token.content||'Figure')+'"><figcaption>'+md.utils.escapeHtml(token.content||'')+'</figcaption></figure>';
 };
 const defaultLink=md.renderer.rules.link_open||((tokens,index,options,env,self)=>self.renderToken(tokens,index,options));
 md.renderer.rules.link_open=(tokens,index,options,env,self)=>{tokens[index].attrSet('target','_blank');tokens[index].attrSet('rel','noopener noreferrer');return defaultLink(tokens,index,options,env,self);};
 return md.render(normalizeMath(source));
}
