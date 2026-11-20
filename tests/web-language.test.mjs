import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
const code=await readFile(new URL('../web/app.js',import.meta.url),'utf8');
const locale=await readFile(new URL('../web/i18n.js',import.meta.url),'utf8');
const html=await readFile(new URL('../web/index.html',import.meta.url),'utf8');
function page(blockedStorage=false){
 const all=[];
 const element=id=>{const attrs={};const e={id,value:'',children:[],dataset:{},style:{setProperty(){}},classList:{toggle(){}},addEventListener(){},
  setAttribute(k,v){attrs[k]=String(v);},getAttribute(k){return attrs[k]??null;},replaceChildren(...children){this.children=children;},append(...children){this.children.push(...children);},pause(){},load(){}};all.push(e);return e;};
 const ids=[...html.matchAll(/\bid="([^"]+)"/g)].map(m=>element(m[1]));
 const doc={documentElement:{},getElementById:id=>ids.find(e=>e.id===id),querySelectorAll:s=>s==='[id]'?ids:s.startsWith('[data-i18n')?all.filter(e=>e.getAttribute(s.slice(1,-1))!==null):[],querySelector:()=>element('query'),createElement:element,createTextNode:text=>({text}),};
 const storage={getItem(){if(blockedStorage)throw new Error('storage blocked');return null;},setItem(){if(blockedStorage)throw new Error('storage blocked');}};
 const window={document:doc,navigator:{language:'zh-CN'},localStorage:storage,addEventListener(){}};
 new Function('window',locale)(window);
 const p=new Function('document','window','localStorage',code.slice(0,code.indexOf('\nawait refreshState();'))+'\nreturn {dom,setStatus,showPreviewError};')(doc,window,storage);
 return {...p,locale:window.SceneAgentI18n,document:doc};
}
function text(e){return e.textContent ?? e.text ?? e.children.map(text).join('');}
test('language changes translate existing connection and preview labels without a reconnect or model reload',()=>{
 const p=page();p.setStatus(p.dom['connection-status'],'实时连接',true,false);p.showPreviewError('raw failure detail');
 assert.equal(text(p.dom['connection-status']),'实时连接');
 p.locale.setLanguage('en');p.locale.apply(p.document);
 assert.equal(text(p.dom['connection-status']),'Connected');
 assert.equal(text(p.dom['preview-status']),'Preview failed to load');
 assert.equal(p.dom['scene-error'].textContent,'raw failure detail');
 p.locale.setLanguage('zh');p.locale.apply(p.document);
 assert.equal(text(p.dom['connection-status']),'实时连接');
 assert.equal(text(p.dom['preview-status']),'预览加载失败');
});

test('the workspace still initializes when browser preference storage is unavailable',()=>{
 const p=page(true);p.setStatus(p.dom['connection-status'],'实时连接',true,false);
 p.locale.setLanguage('en');p.locale.apply(p.document);
 assert.equal(text(p.dom['connection-status']),'Connected');
});
