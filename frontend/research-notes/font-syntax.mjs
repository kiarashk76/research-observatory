export const FONT_SIZES=[12,14,16,18,20,24,28,32,40,48];
export const validFontSize=value=>FONT_SIZES.includes(Number(value))?Number(value):null;
export function readFontSize(source){
 const match=/^<span data-notes-font-size="(\d{2})">([\s\S]*?)<\/span>/.exec(source);
 if(!match||!validFontSize(match[1]))return null;
 return {raw:match[0],size:Number(match[1]),body:match[2]};
}
