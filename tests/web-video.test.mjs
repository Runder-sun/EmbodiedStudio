import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';

const code=await readFile(new URL('../web/app.js',import.meta.url),'utf8');
const html=await readFile(new URL('../web/index.html',import.meta.url),'utf8');
const css=await readFile(new URL('../web/styles.css',import.meta.url),'utf8');
function page(current) {
  const pendingCloseEvents=[];
  const element=id=>{
    const attributes={},listeners=new Map();
    return {id,value:'',dataset:{},children:[],parentElement:null,hidden:false,paused:true,currentTime:0,duration:20,open:false,
      style:{setProperty(){}},classList:{values:new Set(),toggle(name,on){if(on)this.values.add(name);else this.values.delete(name);}},
      addEventListener(name,handler){const rows=listeners.get(name)||[];rows.push(handler);listeners.set(name,rows);},
      setAttribute(key,value){attributes[key]=String(value);if(key.startsWith('data-'))this.dataset[key.slice(5).replace(/-([a-z])/g,(_,v)=>v.toUpperCase())]=String(value);},
      getAttribute(key){return attributes[key]??null;},removeAttribute(key){delete attributes[key];delete this[key];},
      append(...rows){for(const row of rows){if(row.parentElement)row.parentElement.children=row.parentElement.children.filter(child=>child!==row);row.parentElement=this;this.children.push(row);}},
      appendChild(row){this.append(row);return row;},
      replaceChildren(...rows){for(const row of this.children)row.parentElement=null;this.children=[];this.append(...rows);},
      contains(row){return row===this||this.children.some(child=>child.contains?.(row));},
      async dispatch(name){const event={type:name,target:this,defaultPrevented:false,preventDefault(){this.defaultPrevented=true;}};
        if(this['on'+name])await this['on'+name](event);for(const handler of listeners.get(name)||[])await handler(event);
        if(name==='cancel'&&!event.defaultPrevented)this.close();},
      async click(){if(!this.disabled)await this.dispatch('click');},showModal(){this.open=true;},focus(){this.focused=true;},
      close(){const wasOpen=this.open;this.open=false;if(wasOpen)pendingCloseEvents.push(()=>{
        if(this.onclose)this.onclose({target:this});for(const handler of listeners.get('close')||[])handler({target:this});});},
      pause(){this.paused=true;},load(){this.loads=(this.loads||0)+1;},play(){this.paused=false;return Promise.resolve();}};
  };
  const elements=[...html.matchAll(/<([a-z][\w:-]*)\b([^>]*\bid="([^"]+)"[^>]*)>/gi)].map(match=>{
    const row=element(match[3]);row.tagName=match[1].toUpperCase();
    for(const attr of match[2].matchAll(/([\w-]+)="([^"]*)"/g))row.setAttribute(attr[1],attr[2]);
    row.hidden=/\bhidden(?:\s|>|$)/.test(match[2]);return row;
  });
  const get=id=>elements.find(row=>row.id===id);
  if(get('video-content'))get('panel-video').append(get('video-content'));
  const content=get('video-content')||get('panel-video');
  for(const id of ['video-views','video-availability','video-stage','video-caption'])content.append(get(id));
  get('video-stage').append(get('robot-video'));
  for(const id of ['video-episode','video-camera','video-annotations','video-download'])get('video-views').append(get(id));
  if(get('video-dialog-content'))get('video-dialog').append(get('video-dialog-content'));
  const document={getElementById:id=>elements.find(row=>row.id===id),querySelectorAll:selector=>selector==='[id]'?elements:[],
    querySelector:()=>({}),createElement:element,createTextNode:text=>({text})};
  document.querySelectorAll=selector=>selector==='[id]'?elements:selector==='[data-tab]'?elements.filter(row=>row.dataset.tab):[];
  const run=new Function('document','window','localStorage','current',code.slice(0,code.indexOf('\nawait refreshState();'))+
    "\nstate.projects=[current];activeId=current.id;return {renderVideo,selectResultTab,dom,setCurrent(value){state.projects=[value];activeId=value.id;}};");
  return {...run(document,{addEventListener(){},location:{origin:'http://localhost'}},{getItem(){return '';}},current),
    flushDialogClose(){while(pendingCloseEvents.length)pendingCloseEvents.shift()();}};
}
function project(episode='ep') {
  const prefix=`projects/p_test/episodes/${episode}`;
  return {id:'p_test',latestEpisode:{episode,artifacts:{annotations:prefix+'/annotations.json'},media:{primaryCameraId:'main',cameras:[
    {cameraId:'main',status:'available',required:true,video:prefix+'/videos/main.mp4',frameCount:3,firstTime:1,lastTime:3},
    {cameraId:'wrist',status:'partial',required:false,video:prefix+'/videos/wrist.mp4',frameCount:2,firstTime:.5,lastTime:3}]}}};
}

test('switching cameras aligns by recorded simulation time rather than video offsets',()=>{
  const current=project(), p=page(current);p.renderVideo(current,true);
  p.dom['robot-video'].onloadedmetadata();p.dom['robot-video'].currentTime=1.5;p.dom['robot-video'].paused=false;
  p.dom['video-camera'].value='wrist';p.dom['video-camera'].onchange();p.dom['robot-video'].onloadedmetadata();
  assert.equal(p.dom['robot-video'].currentTime,2);
  assert.match(p.dom['robot-video'].src,/wrist\.mp4/);
  assert.match(p.dom['video-annotations'].href,/annotations\.json/);
});

test('a missing camera prefix does not pretend to cover the requested instant or another episode',()=>{
  const current=project();current.latestEpisode.media.cameras[1].lastTime=1;
  const p=page(current);p.renderVideo(current,true);p.dom['robot-video'].onloadedmetadata();p.dom['robot-video'].currentTime=1.5;
  p.dom['video-camera'].value='wrist';p.dom['video-camera'].onchange();p.dom['robot-video'].onloadedmetadata();
  assert.equal(p.dom['robot-video'].currentTime,0);assert.match(p.dom['video-availability'].textContent,/没有记录/);
  p.renderVideo(current);assert.match(p.dom['video-availability'].textContent,/没有记录/,'a state refresh must not erase the missing-time disclosure');
  const next=project('next');next.latestEpisode.media.cameras[0].firstTime=10;next.latestEpisode.media.cameras[0].lastTime=12;
  p.setCurrent(next);p.renderVideo(next);p.dom['robot-video'].onloadedmetadata();
  assert.equal(p.dom['video-camera'].value,'main');assert.equal(p.dom['robot-video'].currentTime,0);
  next.latestEpisode.media.cameras[0].video='projects/p_test/episodes/other/videos/main.mp4';
  p.renderVideo(next);assert.equal(p.dom['robot-video'].hidden,true);
});

test('the video tab switches only its results panel while retaining the three-dimensional preview',async()=>{
  const current=project(),p=page(current);p.renderVideo(current,true);await p.dom['tab-video'].click();
  assert.equal(p.dom['scene-workspace'].classList.values.has('video-primary'),false);
  assert.equal(p.dom['scene-viewport'].hidden,false);
  assert.equal(p.dom['panel-video'].hidden,false);assert.equal(p.dom['panel-progress'].hidden,true);assert.equal(p.dom['panel-data'].hidden,true);
  assert.equal(p.dom['video-stage'].hidden,false);
  p.dom['robot-video'].paused=false;await p.dom['tab-progress'].click();
  assert.equal(p.dom['scene-workspace'].classList.values.has('video-primary'),false);
  assert.equal(p.dom['panel-video'].hidden,true);assert.equal(p.dom['panel-progress'].hidden,false);
  assert.equal(p.dom['robot-video'].paused,true);
});

test('all three result tabs keep the three-dimensional area available with or without video',async()=>{
  const current=project(),p=page(current);await p.dom['tab-video'].click();
  assert.equal(p.dom['scene-workspace'].classList.values.has('video-primary'),false);
  p.renderVideo(current,true);assert.equal(p.dom['scene-workspace'].classList.values.has('video-primary'),false);
  for(const tab of ['data','progress','video']){await p.dom['tab-'+tab].click();assert.equal(p.dom['scene-workspace'].classList.values.has('video-primary'),false);assert.equal(p.dom['scene-viewport'].hidden,false);}
  current.latestEpisode.media.cameras.forEach(camera=>{camera.video=undefined;camera.status='unavailable';});
  p.renderVideo(current);assert.equal(p.dom['scene-workspace'].classList.values.has('video-primary'),false);
  assert.equal(p.dom['video-stage'].hidden,true);
});

test('explicit expansion moves the same player and its controls into a dialog without reloading or seeking',async()=>{
  const current=project(),original=structuredClone(current),p=page(current);p.renderVideo(current,true);p.dom['robot-video'].onloadedmetadata();
  await p.dom['tab-video'].click();
  p.dom['video-camera'].value='wrist';p.dom['video-camera'].onchange();p.dom['robot-video'].onloadedmetadata();
  const video=p.dom['robot-video'],content=p.dom['video-content'],loads=video.loads,src=video.src;
  video.currentTime=1.25;await video.play();
  assert.ok(p.dom['expand-video'],'video expansion is a separate explicit action');await p.dom['expand-video'].click();
  assert.equal(p.dom['video-dialog'].open,true);assert.equal(content.parentElement,p.dom['video-dialog-content']);
  assert.equal(p.dom['video-dialog-content'].contains(video),true);assert.equal(p.dom['video-dialog-content'].contains(p.dom['video-camera']),true);
  assert.equal(p.dom['robot-video'],video);assert.equal(video.currentTime,1.25);assert.equal(video.src,src);assert.equal(video.loads,loads);
  assert.equal(p.dom['video-camera'].value,'wrist');assert.equal(p.dom['scene-workspace'].classList.values.has('video-primary'),false);
  assert.deepEqual(current,original,'expansion changes presentation without editing scene, episode or media records');
});

test('closing expanded video returns the same paused player with its time, source and camera selection intact',async()=>{
  const current=project(),p=page(current);p.renderVideo(current,true);p.dom['robot-video'].onloadedmetadata();
  await p.dom['tab-video'].click();
  const video=p.dom['robot-video'],content=p.dom['video-content'];video.currentTime=1.4;await video.play();
  assert.ok(p.dom['expand-video']);await p.dom['expand-video'].click();
  const src=video.src,loads=video.loads;assert.ok(p.dom['close-video'],'the expanded video has an explicit close control');await p.dom['close-video'].click();
  assert.equal(video.paused,true,'playback pauses before the native asynchronous close event');p.flushDialogClose();
  assert.equal(p.dom['video-dialog'].open,false);assert.equal(content.parentElement,p.dom['panel-video']);assert.equal(content.contains(video),true);
  assert.equal(video.paused,true);assert.equal(video.currentTime,1.4);assert.equal(video.src,src);assert.equal(video.loads,loads);assert.equal(p.dom['video-camera'].value,'main');
  assert.equal(p.dom['expand-video'].focused,true,'closing returns keyboard focus to the expansion control');
});

test('Escape closes expanded video and pauses it before restoring the lower panel',async()=>{
  const current=project(),p=page(current);p.renderVideo(current,true);await p.dom['tab-video'].click();assert.ok(p.dom['expand-video']);await p.dom['expand-video'].click();
  p.dom['robot-video'].currentTime=1.75;await p.dom['robot-video'].play();await p.dom['video-dialog'].dispatch('cancel');
  assert.equal(p.dom['robot-video'].paused,true);p.flushDialogClose();
  assert.equal(p.dom['video-dialog'].open,false);assert.equal(p.dom['video-content'].parentElement,p.dom['panel-video']);
  assert.equal(p.dom['robot-video'].paused,true);assert.equal(p.dom['robot-video'].currentTime,1.75);
  assert.equal(p.dom['expand-video'].focused,true);
});

test('missing media cannot open the expanded preview and losing media closes an already open preview',async()=>{
  const current=project(),p=page(current);p.renderVideo(undefined,true);assert.ok(p.dom['expand-video']);await p.dom['expand-video'].click();
  assert.equal(p.dom['video-dialog'].open,false);
  p.renderVideo(current,true);await p.dom['expand-video'].click();assert.equal(p.dom['video-dialog'].open,true);
  current.latestEpisode.media.cameras.forEach(camera=>{camera.video=undefined;camera.status='unavailable';});p.renderVideo(current);
  p.flushDialogClose();
  assert.equal(p.dom['video-dialog'].open,false);assert.equal(p.dom['video-content'].parentElement,p.dom['panel-video']);assert.equal(p.dom['robot-video'].paused,true);
});

test('changing projects closes expanded video before rendering another projects recording',async()=>{
  const current=project(),p=page(current);p.renderVideo(current,true);assert.ok(p.dom['expand-video']);await p.dom['expand-video'].click();
  await p.dom['robot-video'].play();const next=project('other');next.id='p_next';
  for(const camera of next.latestEpisode.media.cameras)camera.video=camera.video.replace('projects/p_test/','projects/p_next/');
  p.setCurrent(next);p.renderVideo(next,true);p.flushDialogClose();
  assert.equal(p.dom['video-dialog'].open,false);assert.equal(p.dom['video-content'].parentElement,p.dom['panel-video']);assert.equal(p.dom['robot-video'].paused,true);
  assert.match(p.dom['robot-video'].src,/p_next/);assert.equal(p.dom['video-camera'].value,'main');
});

test('an unchanged state refresh does not close expanded video or replace its player',async()=>{
  const current=project(),p=page(current);p.renderVideo(current,true);await p.dom['tab-video'].click();assert.ok(p.dom['expand-video']);await p.dom['expand-video'].click();
  const video=p.dom['robot-video'];video.currentTime=1.25;const loads=video.loads;p.renderVideo(current);
  assert.equal(p.dom['video-dialog'].open,true);assert.equal(p.dom['video-dialog-content'].contains(video),true);
  assert.equal(video.currentTime,1.25);assert.equal(video.loads,loads);
});

test('late camera metadata cannot restart playback after closing the dialog or pressing Escape',async()=>{
  for(const close of ['button','escape']){
    const current=project(),p=page(current);p.renderVideo(current,true);p.dom['robot-video'].onloadedmetadata();
    await p.dom['tab-video'].click();
    assert.ok(p.dom['expand-video']);await p.dom['expand-video'].click();
    const video=p.dom['robot-video'];video.currentTime=1.25;await video.play();
    p.dom['video-camera'].value='wrist';p.dom['video-camera'].onchange();const lateMetadata=video.onloadedmetadata;
    if(close==='button')await p.dom['close-video'].click();else await p.dom['video-dialog'].dispatch('cancel');
    assert.equal(video.paused,true);lateMetadata();
    assert.equal(video.paused,true,close+' invalidates the pending resume intent');
    assert.equal(video.currentTime,1.75,'the requested simulation instant remains aligned');
    assert.equal(p.dom['video-camera'].value,'wrist');assert.equal(p.dom['video-dialog'].open,false);
    p.flushDialogClose();assert.equal(p.dom['video-content'].parentElement,p.dom['panel-video']);
  }
});

test('a prior projects pending camera metadata cannot resume or seek the next projects video',async()=>{
  const current=project(),p=page(current);p.renderVideo(current,true);p.dom['robot-video'].onloadedmetadata();
  await p.dom['tab-video'].click();
  assert.ok(p.dom['expand-video']);await p.dom['expand-video'].click();
  const video=p.dom['robot-video'];video.currentTime=1.25;await video.play();p.dom['video-camera'].value='wrist';p.dom['video-camera'].onchange();
  const lateMetadata=video.onloadedmetadata,next=project('new-project');next.id='p_next';
  for(const camera of next.latestEpisode.media.cameras)camera.video=camera.video.replace('projects/p_test/','projects/p_next/');
  p.setCurrent(next);p.renderVideo(next,true);video.onloadedmetadata();const src=video.src;lateMetadata();p.flushDialogClose();
  assert.equal(video.paused,true);assert.equal(video.currentTime,0);assert.equal(video.src,src);assert.match(src,/p_next/);
  assert.equal(p.dom['video-camera'].value,'main');assert.equal(p.dom['video-dialog'].open,false);
});

test('late camera metadata cannot resume hidden playback after leaving the video results tab',async()=>{
  const current=project(),p=page(current);p.renderVideo(current,true);p.dom['robot-video'].onloadedmetadata();await p.dom['tab-video'].click();
  const video=p.dom['robot-video'];video.currentTime=1.25;await video.play();p.dom['video-camera'].value='wrist';p.dom['video-camera'].onchange();
  const lateMetadata=video.onloadedmetadata;await p.dom['tab-progress'].click();assert.equal(video.paused,true);lateMetadata();
  assert.equal(video.paused,true);assert.equal(video.currentTime,1.75);assert.equal(p.dom['panel-video'].hidden,true);
});

test('video result selection has no stylesheet rule that removes the three-dimensional preview',()=>{
  for(const rule of css.matchAll(/([^{}]+)\{([^{}]*)\}/g)){
    if(rule[1].includes('video-primary')&&rule[1].includes('scene-viewport'))assert.doesNotMatch(rule[2],/display\s*:\s*none/);
  }
});

test('a new build without media retains published historical runs with explicit version labels',()=>{
  const current=project();const recorded={...current.latestEpisode,projectId:current.id,revision:'old-revision'};
  current.latestEpisode={episode:'new',revision:'new-revision'};current.latestBuild={revision:'new-revision'};
  current.jobs=[{id:'recorded',kind:'rollout',status:'completed',result:recorded}];
  const p=page(current);p.renderVideo(current,true);
  assert.equal(p.dom['robot-video'].hidden,false);
  assert.match(p.dom['robot-video'].src,/main\.mp4/);
  assert.match(p.dom['video-caption'].textContent,/old-revision/);
  assert.match(p.dom['video-caption'].textContent,/历史版本/);
});

test('choosing a published run preserves its own camera and timing instead of another run',()=>{
  const current=project('newest');current.latestEpisode.revision='new-revision';
  const prior=project('prior').latestEpisode;prior.projectId=current.id;prior.revision='old-revision';
  current.jobs=[{id:'prior',kind:'rollout',status:'completed',result:prior},
    {id:'foreign',kind:'rollout',status:'completed',result:{...project('foreign').latestEpisode,projectId:'p_other'}},
    {id:'candidate',kind:'rollout',status:'failed',result:{...project('candidate').latestEpisode,projectId:current.id}}];
  const p=page(current);p.renderVideo(current,true);p.dom['robot-video'].currentTime=2;
  p.dom['video-episode'].value='prior';p.renderVideo(current);p.dom['robot-video'].onloadedmetadata();
  assert.match(p.dom['robot-video'].src,/episodes%2Fprior%2F/);
  assert.equal(p.dom['robot-video'].currentTime,0);
  assert.equal(p.dom['video-camera'].value,'main');
  assert.equal(p.dom['video-episode'].children.length,3,'latest plus two known runs; exclude foreign and failed candidates');
  p.renderVideo(current);assert.match(p.dom['robot-video'].src,/episodes%2Fprior%2F/);
});


test('unbound legacy video cannot be labelled as the latest episode and revision',()=>{
 const current={id:'p_test',latestBuild:{revision:'new-revision'},latestEpisode:{episode:'new-episode',revision:'new-revision'},jobs:[],artifacts:{video:'/api/projects/p_test/artifacts/main.mp4?file=projects%2Fp_test%2Fepisodes%2Fold-episode%2Fvideos%2Fmain.mp4'}};
 const p=page(current);p.renderVideo(current,true);
 assert.ok(p.dom['robot-video'].hidden || (!p.dom['video-caption'].textContent.includes('new-episode')&&!p.dom['video-caption'].textContent.includes('new-revision')),p.dom['video-caption'].textContent);
});

test('a legacy recording with an exact matching project and episode remains playable',()=>{
 const current={id:'p_test',latestBuild:{revision:'current-revision'},latestEpisode:{episode:'matching-episode',revision:'current-revision'},jobs:[],artifacts:{video:'/api/projects/p_test/artifacts/main.mp4?file=projects%2Fp_test%2Fepisodes%2Fmatching-episode%2Fvideos%2Fmain.mp4'}};
 const p=page(current);p.renderVideo(current,true);assert.equal(p.dom['robot-video'].hidden,false);
 assert.match(p.dom['robot-video'].src,/matching-episode/);assert.match(p.dom['video-caption'].textContent,/matching-episode/);
});

test('an empty workspace has no fallback recording and keeps the 3D area available',()=>{
 const p=page({id:'p_test'});p.renderVideo(undefined,true);
 assert.equal(p.dom['robot-video'].hidden,true);assert.equal(p.dom['video-stage'].hidden,true);
});
