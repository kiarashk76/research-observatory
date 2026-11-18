import {Mark} from '@tiptap/core';
import {validFontSize,readFontSize} from './font-syntax.mjs';
export const NotesFontSize=Mark.create({
 name:'notesFontSize',
 addAttributes(){return {size:{default:16,parseHTML:element=>validFontSize(element.dataset.notesFontSize)||16,renderHTML:attrs=>({'data-notes-font-size':validFontSize(attrs.size)||16,style:'font-size:'+(validFontSize(attrs.size)||16)+'px'})}};},
 parseHTML(){return [{tag:'span[data-notes-font-size]',getAttrs:element=>validFontSize(element.dataset.notesFontSize)?{}:false}];},
 renderHTML({HTMLAttributes}){return ['span',HTMLAttributes,0];},
 parseMarkdown(token,helpers){return helpers.applyMark('notesFontSize',helpers.parseInline(token.tokens),{size:token.size});},
 renderMarkdown(node,helpers){return '<span data-notes-font-size="'+(validFontSize(node.attrs?.size)||16)+'">'+helpers.renderChildren(node)+'</span>';},
 markdownTokenizer:{name:'notesFontSize',level:'inline',start:src=>src.indexOf('<span data-notes-font-size='),tokenize(src,_tokens,lexer){const span=readFontSize(src);if(span)return {type:'notesFontSize',raw:span.raw,size:span.size,tokens:lexer.inlineTokens(span.body)};}},
});
