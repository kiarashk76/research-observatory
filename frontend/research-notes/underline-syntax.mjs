// Accept only the exact underline tags; keep ordinary HTML disabled.
export function readUnderline(source){
 if(!/^<u>/i.test(source))return null;
 const delimiters=/\\[\s\S]|`+|<\/u>/gi;delimiters.lastIndex=3;
 for(let match;(match=delimiters.exec(source));){
  if(match[0].startsWith('`')){
   const code=/^(`+)([^`]|[^`][\s\S]*?[^`])\1(?!`)/.exec(source.slice(match.index));
   if(code)delimiters.lastIndex=match.index+code[0].length;
  }else if(!match[0].startsWith('\\')){
   const body=source.slice(3,match.index);
   return body?{raw:source.slice(0,delimiters.lastIndex),body}:null;
  }
 }
 return null;
}
