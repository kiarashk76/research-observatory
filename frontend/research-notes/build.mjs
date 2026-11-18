import {build} from 'esbuild';
import path from 'node:path';
const here=path.dirname(new URL(import.meta.url).pathname);
await build({entryPoints:[path.join(here,'editor.mjs')],outdir:path.resolve(here,'../../labcore/dashboard/notes-vendor'),entryNames:'notes-editor',assetNames:'fonts/[name]-[hash]',bundle:true,minify:true,format:'iife',target:['es2022'],loader:{'.woff':'file','.woff2':'file','.ttf':'file'},publicPath:'/notes-assets',legalComments:'linked'});
