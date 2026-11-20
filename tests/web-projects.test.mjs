import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { publicProject } from '../src/server.mjs';

const code=await readFile(new URL('../web/app.js',import.meta.url),'utf8');
const html=await readFile(new URL('../web/index.html',import.meta.url),'utf8');
function page(projects){
  const requests=[];
  const element=id=>({id,value:'',dataset:{},children:[],hidden:false,scrollTop:0,scrollHeight:0,clientHeight:0,
    style:{setProperty(){}},classList:{toggle(){}},addEventListener(){},setAttribute(){},getAttribute(){return 'false';},
    removeAttribute(){},append(...items){this.children.push(...items);},replaceChildren(...items){this.children=items;},
    get options(){return this.children;},pause(){},load(){},focus(){}});
  const elements=[...html.matchAll(/\bid="([^"]+)"/g)].map(row=>element(row[1]));
  const doc={getElementById:id=>elements.find(e=>e.id===id),querySelectorAll:s=>s==='[id]'?elements:[],
    querySelector:()=>element('query'),createElement:element,createTextNode:text=>({text})};
  const fetch=(path,options)=>new Promise(resolve=>requests.push({path,options,resolve:()=>resolve({ok:true,headers:{get(){return 'application/json';}},async json(){return {};}})}));
  const run=new Function('document','window','localStorage','fetch',code.slice(0,code.indexOf('\nawait refreshState();'))+
    '\nrefreshState=async()=>{};return {dom,applyState,streams,current:()=>activeId};');
  const p=run(doc,{addEventListener(){},location:{origin:'http://localhost'}},{getItem(){return '';},setItem(){}},fetch);
  p.applyState({codex:{ready:true},config:{},projects:projects || ['p_A','p_B'].map(id=>({id,name:id,status:'ready',prompt:'',messages:[],jobs:[],artifacts:{}}))});
  return {...p,requests,switchTo(id){p.dom['project-select'].value=id;p.dom['project-select'].onchange();},
    type(text){p.dom['chat-input'].value=text;p.dom['chat-input'].oninput();}};
}

test('each parallel project retains its own unsent prompt',()=>{
  const p=page();p.type('A request stays in A');p.switchTo('p_B');
  assert.equal(p.dom['chat-input'].value,'');p.type('B request stays in B');p.switchTo('p_A');
  assert.equal(p.dom['chat-input'].value,'A request stays in A');p.switchTo('p_B');
  assert.equal(p.dom['chat-input'].value,'B request stays in B');
});

test('a pending A send does not block B or clear its matching draft when A completes',async()=>{
  const p=page();p.type('same text');const sendingA=p.dom['chat-form'].onsubmit({preventDefault(){}});
  assert.equal(p.requests[0].path,'/api/projects/p_A/chat');p.switchTo('p_B');p.type('same text');
  assert.equal(p.dom['send-message'].disabled,false,'a different project can be sent while A awaits its native acknowledgement');
  const sendingB=p.dom['chat-form'].onsubmit({preventDefault(){}});
  assert.equal(p.requests[1].path,'/api/projects/p_B/chat');p.requests[0].resolve();await sendingA;
  assert.equal(p.dom['chat-input'].value,'same text','an A response cannot clear B even if its text matches');
  assert.equal(p.dom['send-message'].disabled,true,'B keeps its own pending state');
  p.requests[1].resolve();await sendingB;assert.equal(p.dom['chat-input'].value,'');
});


test('A interrupt completion cannot delete B live stream after project switch',async()=>{
 const p=page();p.applyState({codex:{ready:true},config:{},projects:['p_A','p_B'].map(id=>({id,name:id,status:'active',activeTurnId:id+'turn',generation:{status:'active'},prompt:'',messages:[],jobs:[],artifacts:{}}))});
 p.streams.set('p_A',{itemId:'a',text:'A live'});p.streams.set('p_B',{itemId:'b',text:'B live'});
 const stopping=p.dom.interrupt.onclick();assert.equal(p.requests[0].path,'/api/projects/p_A/interrupt');
 p.switchTo('p_B');p.requests[0].resolve();await stopping;
 assert.equal(p.streams.get('p_B')?.text,'B live');
});

test('each project keeps its own pending stop and one reply cannot re-enable another stop',async()=>{
 const p=page();p.applyState({codex:{ready:true},config:{},projects:['p_A','p_B'].map(id=>({id,name:id,status:'active',activeTurnId:id+'turn',generation:{status:'active'},prompt:'',messages:[],jobs:[],artifacts:{}}))});
 const a=p.dom.interrupt.onclick();assert.equal(p.dom.interrupt.disabled,true);
 p.switchTo('p_B');assert.equal(p.dom.interrupt.disabled,false);const b=p.dom.interrupt.onclick();
 p.requests[0].resolve();await a;assert.equal(p.dom.interrupt.disabled,true);
 p.requests[1].resolve();await b;assert.equal(p.dom.interrupt.disabled,false);
});

test('scene-only chat requests generation and evaluation without robot data',async()=>{
 const p=page();p.dom['output-scope'].value='scene';p.dom['output-scope'].onchange();p.type('Create this scene and its rubric');
 const sending=p.dom['chat-form'].onsubmit({preventDefault(){}});
 assert.deepEqual(JSON.parse(p.requests[0].options.body).requiredOutputs,['generation','evaluation']);
 assert.equal(JSON.parse(p.requests[0].options.body).environmentRefs,undefined);
 p.requests[0].resolve();await sending;
});

test('robot collection uses selected frozen layouts and preserves their split',async()=>{
 const p=page([{id:'p_A',name:'Kitchen',status:'complete',prompt:'',messages:[],jobs:[],artifacts:{},
  environmentRevisions:[{revision:'layout_train',split:'train',capabilities:{stageSplit:true}},
   {revision:'layout_test',split:'test',capabilities:{stageSplit:true}}]}]);
 p.dom['collect-data'].onclick();
 assert.equal(p.dom['output-scope'].value,'robot');
 assert.equal(p.requests.length,0,'choosing collection must not send a prompt');
 p.type('Collect the requested demonstrations');const sending=p.dom['chat-form'].onsubmit({preventDefault(){}});
 const body=JSON.parse(p.requests[0].options.body);
 assert.deepEqual(body.requiredOutputs,['demonstrations']);
 assert.deepEqual(body.environmentRefs,[{revision:'layout_train',split:'train'}]);
 p.requests[0].resolve();await sending;
});

test('real public project labels keep held-out layouts out of default robot collection',async()=>{
 const environmentRefs=['train','validation','test'].map(split=>({revision:'layout_'+split,split}));
 const project={id:'p_A',name:'SYSTEM_VERIFICATION_ONLY',status:'complete',prompt:'',messages:[],events:[],artifacts:{},
  jobs:[...environmentRefs.map(ref=>({kind:'build',status:'completed',result:{projectId:'p_A',revision:ref.revision,
   capabilities:{stageSplit:true},tasks:[{id:'task'}]}})),
   {kind:'consume',status:'completed',delivery:{accepted:true,generationId:'scene-generation',environmentRefs,
    artifactRef:'projects/p_A/deliveries/scene/delivery.json',products:{evaluation:{passed:true}}}}]};
 const original=JSON.stringify(project);const p=page([publicProject(project)]);p.dom['collect-data'].onclick();
 const rows=p.dom['collection-layouts'].children;
 assert.deepEqual(rows.map(row=>row.children[0].checked),[true,false,false]);
 p.type('Collect data');const first=p.dom['chat-form'].onsubmit({preventDefault(){}});
 assert.deepEqual(JSON.parse(p.requests[0].options.body).environmentRefs,[environmentRefs[0]]);
 p.requests[0].resolve();await first;
 const testChoice=p.dom['collection-layouts'].children[2].children[0];testChoice.checked=true;testChoice.onchange();
 p.type('Collect a separate evaluation episode');
 const second=p.dom['chat-form'].onsubmit({preventDefault(){}});
 assert.deepEqual(JSON.parse(p.requests[1].options.body).environmentRefs,[environmentRefs[0],environmentRefs[2]]);
 p.requests[1].resolve();await second;
 assert.equal(JSON.stringify(project),original,'UI selection does not relabel the frozen environment');
});

test('collection without a compatible frozen environment does not start a generation',async()=>{
 const p=page();p.dom['output-scope'].value='robot';p.dom['output-scope'].onchange();p.type('Collect data');
 await p.dom['chat-form'].onsubmit({preventDefault(){}});
 assert.equal(p.requests.length,0);
});

test('delivery cards show only the requested scene products',()=>{
 const p=page([{id:'p_A',name:'Scene',status:'complete',prompt:'',messages:[],jobs:[],artifacts:{},
 generation:{status:'complete',requiredOutputs:['generation','evaluation']},
 delivery:{accepted:true,requiredOutputs:['generation','evaluation'],products:{generation:{passed:true},evaluation:{passed:true}}}}]);
 assert.equal(p.dom['delivery-products'].children.length,2);
 assert.equal(p.dom['delivery-status'].textContent,'本次产物已交付');
});

test('active collection preserves its locked task selection when adding a message',async()=>{
 const refs=[{revision:'layout_a',split:'train',taskIds:['task_b']}];
 const p=page([{id:'p_A',name:'Collect',status:'active',prompt:'',messages:[],jobs:[],artifacts:{},
 generation:{status:'active',requiredOutputs:['demonstrations'],environmentRefs:refs},
 environmentRevisions:[{revision:'layout_a',capabilities:{stageSplit:true}}]}]);
 assert.equal(p.dom['output-scope'].disabled,true);p.type('Keep collecting');
 const sending=p.dom['chat-form'].onsubmit({preventDefault(){}});
 assert.deepEqual(JSON.parse(p.requests[0].options.body).environmentRefs,refs);
 p.requests[0].resolve();await sending;
});
