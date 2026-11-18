import {Node} from '@tiptap/core';
import {NodeSelection,TextSelection} from '@tiptap/pm/state';
import {closeHistory} from '@tiptap/pm/history';
import {Fragment} from '@tiptap/pm/model';
import {readAlignment} from './render.mjs';
import {readColumns,columnWidth} from './columns.mjs';

// A restricted Markdown container preserves layout without accepting raw HTML.
export const NotesAlign=Node.create({
 name:'notesAlign',group:'block',content:'block+',defining:true,
 addAttributes(){return {align:{default:'left',parseHTML:element=>['left','center','right'].includes(element.dataset.notesAlign)?element.dataset.notesAlign:'left',renderHTML:attributes=>({'data-notes-align':attributes.align})}};},
 parseHTML(){return [{tag:'div[data-notes-align]'}];},
 renderHTML({HTMLAttributes}){return ['div',HTMLAttributes,0];},
 parseMarkdown(token,helpers){return {type:'notesAlign',attrs:{align:token.align},content:helpers.parseChildren(token.tokens)};},
 renderMarkdown(node,helpers){return ':::notes-align '+node.attrs.align+'\n\n'+helpers.renderChildren(node.content,'\n\n')+'\n\n:::end-notes-align';},
 markdownTokenizer:{name:'notesAlign',level:'block',start:src=>src.indexOf(':::notes-align '),tokenize(src,_tokens,lexer){const block=readAlignment(src);if(block)return {type:'notesAlign',raw:block.raw,align:block.align,tokens:lexer.blockTokens(block.body)};}},
});

export const NotesColumn=Node.create({
 name:'notesColumn',content:'block+',defining:true,isolating:true,
 parseHTML(){return [{tag:'div[data-notes-column]'}];},
 renderHTML(){return ['div',{'data-notes-column':''},0];},
 renderMarkdown(node,helpers){return ':::notes-column\n\n'+helpers.renderChildren(node.content,'\n\n')+'\n\n:::end-notes-column';},
});
export const NotesColumns=Node.create({
 name:'notesColumns',group:'block',content:'notesColumn notesColumn',defining:true,isolating:true,
 addAttributes(){return {width:{default:50,parseHTML:el=>columnWidth(el.dataset.notesColumnWidth),renderHTML:attrs=>({'data-notes-column-width':columnWidth(attrs.width),style:'--notes-column-left:'+columnWidth(attrs.width)+'fr;--notes-column-right:'+(100-columnWidth(attrs.width))+'fr'})}};},
 parseHTML(){return [{tag:'div[data-notes-columns]'}];},
 renderHTML({HTMLAttributes}){return ['div',{'data-notes-columns':'',...HTMLAttributes},0];},
 parseMarkdown(token,helpers){return {type:'notesColumns',attrs:{width:columnWidth(token.width)},content:token.columns.map(tokens=>{const content=helpers.parseChildren(tokens);return {type:'notesColumn',content:content.length?content:[{type:'paragraph'}]};})};},
 renderMarkdown(node,helpers){return ':::notes-columns'+(columnWidth(node.attrs.width)===50?'':' '+columnWidth(node.attrs.width))+'\n\n'+helpers.renderChildren(node.content,'\n\n')+'\n\n:::end-notes-columns';},
 markdownTokenizer:{name:'notesColumns',level:'block',start:src=>src.indexOf(':::notes-columns'),tokenize(src,_tokens,lexer){const block=readColumns(src);if(block)return {type:'notesColumns',raw:block.raw,width:block.width,columns:block.columns.map(body=>lexer.blockTokens(body))};}},
});
export function columnContext(state){
 const $from=state.selection.$from;
 for(let depth=$from.depth;depth>0;depth--)if($from.node(depth).type.name==='notesColumn'){
  const rowDepth=depth-1;
  return {depth,row:$from.node(rowDepth),rowPos:$from.before(rowDepth),column:$from.index(rowDepth)};
 }
 return null;
}
export function resizeColumns(editor,value){
 const context=columnContext(editor.state);if(!context)return false;
 const tr=closeHistory(editor.state.tr).setNodeMarkup(context.rowPos,null,{...context.row.attrs,width:columnWidth(value)});
 editor.view.dispatch(tr);return true;
}
export function changeColumns(editor,action){
 const {state}=editor,context=columnContext(state),block=currentBlock(state),schema=state.schema;
 if(!block)return false;
 let tr=closeHistory(state.tr);
 if(action==='two-columns'){
  if(context)return false;
  const column=content=>schema.nodes.notesColumn.create(null,content);
  const row=schema.nodes.notesColumns.create(null,[column(block.node),column(schema.nodes.paragraph.create())]);
  tr.replaceWith(block.pos,block.pos+block.node.nodeSize,row);
  if(block.index===block.parent.childCount-1)tr.insert(block.pos+row.nodeSize,schema.nodes.paragraph.create());
  tr.setSelection(TextSelection.near(tr.doc.resolve(block.pos+row.nodeSize-3)));
 }else if(context&&action==='remove-columns'){
  const content=[];context.row.forEach(column=>column.forEach(node=>content.push(node)));
  tr.replaceWith(context.rowPos,context.rowPos+context.row.nodeSize,content);
  tr.setSelection(TextSelection.near(tr.doc.resolve(context.rowPos)));
 }else if(context&&action==='other-column'){
  const contents=[[],[]];context.row.forEach((column,_offset,i)=>column.forEach(node=>contents[i].push(node)));
  contents[context.column].splice(block.index,1);const other=1-context.column;
  // Replace a vacant destination rather than accumulating empty placeholders.
  if(contents[other].length===1&&contents[other][0].type.name==='paragraph'&&!contents[other][0].content.size)contents[other]=[];
  contents[other].push(block.node);
  const columns=contents.map(nodes=>schema.nodes.notesColumn.create(null,nodes.length?nodes:schema.nodes.paragraph.create()));
  const row=schema.nodes.notesColumns.create(context.row.attrs,columns);
  tr.replaceWith(context.rowPos,context.rowPos+context.row.nodeSize,row);
  const columnPos=context.rowPos+1+(other?columns[0].nodeSize:0);
  const preceding=columns[other].content.size-block.node.nodeSize;
  tr.setSelection(TextSelection.near(tr.doc.resolve(columnPos+1+preceding)));
 }else return false;
 editor.view.dispatch(tr.scrollIntoView());editor.view.focus();return true;
}

// Document blocks include their layout container, so moving/deleting never
// strands alignment or leaves behind part of a table/quote.
export function currentBlock(state){
 const {doc,selection}=state;
 if(!doc.childCount)return null;
 const depth=columnContext(state)?.depth??0,parent=selection.$from.node(depth);
 const index=Math.min(selection.$from.index(depth),parent.childCount-1);
 let pos=depth?selection.$from.start(depth):0;for(let i=0;i<index;i++)pos+=parent.child(i).nodeSize;
 return {node:parent.child(index),index,pos,parent};
}
export function changeBlock(editor,action){
 const state=editor.state,block=currentBlock(state);if(!block)return false;
 const {doc}=state,{node,index,pos,parent}=block,end=pos+node.nodeSize;let tr=closeHistory(state.tr);
 if(action==='select-block')tr.setSelection(NodeSelection.create(doc,pos));
 else if(action==='delete-block')tr.delete(pos,end);
 else if(action==='move-up'){
  if(!index)return false;
  const previous=parent.child(index-1),start=pos-previous.nodeSize;
  tr.replaceWith(start,end,Fragment.fromArray([node,previous]));tr.setSelection(NodeSelection.create(tr.doc,start));
 }else if(action==='move-down'){
  if(index>=parent.childCount-1)return false;
  const next=parent.child(index+1);
  tr.replaceWith(pos,end+next.nodeSize,Fragment.fromArray([next,node]));tr.setSelection(NodeSelection.create(tr.doc,pos+next.nodeSize));
 }else return false;
 editor.view.dispatch(tr.scrollIntoView());editor.view.focus();return true;
}
export function alignBlocks(editor,align){
 if(!['left','center','right'].includes(align))return false;
 const {state}=editor,{selection}=state;let tr=closeHistory(state.tr);const blocks=[];
 const context=columnContext(state),parent=context?selection.$from.node(context.depth):state.doc,offset=context?selection.$from.start(context.depth):0;
 parent.forEach((node,relative)=>{const pos=relative+offset;
  if(selection.empty?(selection.from>=pos&&selection.from<pos+node.nodeSize):(pos<selection.to&&pos+node.nodeSize>selection.from))blocks.push({node,pos});
 });
 for(const {node,pos} of [...blocks].reverse()){
  if(node.type.name==='notesAlign')tr.setNodeMarkup(pos,null,{...node.attrs,align});
  else tr.replaceWith(pos,pos+node.nodeSize,state.schema.nodes.notesAlign.create({align},node));
 }
 if(!blocks.length)return false;
 // Replace steps collapse the old cursor to a boundary. Preserve its offset
 // inside the wrapped content, including table cells and multi-block ranges.
 const mapped=position=>{let delta=0;for(const {node,pos} of blocks){if(node.type.name==='notesAlign')continue;if(position>=pos+node.nodeSize)delta+=2;else if(position>=pos)return position+delta+1;else break;}return position+delta;};
 if(selection instanceof NodeSelection){const wasWrapped=selection.node.type.name!=='notesAlign';tr.setSelection(NodeSelection.create(tr.doc,mapped(selection.from)-(wasWrapped?1:0)));}
 else tr.setSelection(TextSelection.between(tr.doc.resolve(mapped(selection.anchor)),tr.doc.resolve(mapped(selection.head))));
 editor.view.dispatch(tr.scrollIntoView());editor.view.focus();return true;
}
