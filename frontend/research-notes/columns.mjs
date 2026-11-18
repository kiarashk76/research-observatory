// Restricted layout syntax, shared by the rich editor and reading/PDF renderers.
export const columnWidth=value=>Number.isInteger(Number(value))&&Number(value)>=20&&Number(value)<=80?Number(value):50;
function container(source,name){
 const opening=new RegExp('^:::'+name+(name==='notes-columns'?'(?: (\\d{2}))?':'')+'[ \\t]*\\n').exec(source);
 if(!opening||(opening[1]&&columnWidth(opening[1])!==Number(opening[1])))return null;
 const lines=source.slice(opening[0].length).split(/(?<=\n)/);
 let offset=opening[0].length,depth=1,fence=null,math=false;
 for(const line of lines){
  const text=line.replace(/[\r\n]+$/,'');
  const code=/^ {0,3}(`{3,}|~{3,})(.*)$/.exec(text);
  if(code&&!math){if(!fence)fence=[code[1][0],code[1].length];else if(fence[0]===code[1][0]&&code[1].length>=fence[1]&&!code[2].trim())fence=null;}
  else if(!fence){
   if(/^\s*\$\$\s*$/.test(text))math=!math;
   else if(!math){
    if(new RegExp('^:::'+name+(name==='notes-columns'?'(?: \\d{2})?':'')+'$').test(text.trim()))depth++;
    if(text.trim()===':::end-'+name&&!--depth)return {width:columnWidth(opening[1]),body:source.slice(opening[0].length,offset),raw:source.slice(0,offset+line.length)};
   }
  }
  offset+=line.length;
 }
 return null;
}
export function readColumns(source){
 const outer=container(source,'notes-columns');if(!outer)return null;
 const columns=[];let rest=outer.body.trimStart();
 for(let i=0;i<2;i++){
  const column=container(rest,'notes-column');if(!column)return null;
  columns.push(column.body);rest=rest.slice(column.raw.length).trimStart();
 }
 return rest.trim()?null:{raw:outer.raw,columns,width:outer.width};
}
