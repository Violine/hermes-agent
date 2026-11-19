// Structural UI checks run against the exact legacy overlay deployed to Hermes.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const source = fs.readFileSync(path.join(__dirname, '../dashboard/kanban/index.js'), 'utf8');
const helper = fs.readFileSync(path.join(__dirname, '../dashboard/operator-status.js'), 'utf8');
assert.ok(source.includes('// BEGIN operator-status.js\n' + helper + '  // END operator-status.js'), 'deployed helper must match canonical tested source');
let component;
let fetchImpl = async () => ({});
const React = {
  Component: class {}, Fragment: 'fragment',
  createElement(type, props, ...children) { return {type, props: {...props, children}}; },
  useMemo(fn) { return fn(); }, useState(v) { return [typeof v === 'function' ? v() : v, () => {}]; },
};
const context = { window: {
  __HERMES_PLUGIN_SDK__: {React,
    components: Object.fromEntries(['Card','CardContent','Badge','Button','Input','Label','Select','SelectOption'].map(n => [n, n])),
    hooks: {useState: React.useState, useEffect() {}, useCallback: f=>f, useMemo: f=>f(), useRef: current=>({current})},
    utils: {cn: (...x)=>x.filter(Boolean).join(' '), timeAgo: ()=>''},
    useI18n: ()=>({t:{},locale:'ru'}), fetchJSON: (...args)=>fetchImpl(...args)},
  __HERMES_PLUGINS__: {register(name, value) { assert.equal(name, 'kanban'); component = value; }},
}, URL, URLSearchParams, Date, console, Set, Map, Promise, setTimeout, clearTimeout };
const instrumented = source.replace('  // Register\n', '  // Register\n')
  .replace('  if (window.__HERMES_PLUGINS__ &&', '  window.__test = {TaskDetail, TaskCard, BoardToolbar, BoardSwitcher, operatorColumns, mapTeamTasks, loadBlockedContexts, operatorStatus};\n  if (window.__HERMES_PLUGINS__ &&');
vm.runInNewContext(instrumented, context);
assert.equal(typeof component, 'function');
const api = context.window.__test;
function nodes(n, predicate, out=[]) {
  if (n == null || typeof n !== 'object') return out;
  if (Array.isArray(n)) { n.forEach(x=>nodes(x,predicate,out)); return out; }
  if (predicate(n)) out.push(n);
  nodes(n.props?.children,predicate,out); return out;
}
function text(n) {
  if (n == null || typeof n === 'boolean') return '';
  if (Array.isArray(n)) return n.map(text).join(' ');
  if (typeof n !== 'object') return String(n);
  return text(n.props?.children);
}
function initiallyVisible(n) {
  if (n == null || typeof n !== 'object') return text(n);
  if (Array.isArray(n)) return n.map(initiallyVisible).join(' ');
  if (n.type === 'details' && !n.props.open) return initiallyVisible(n.props.children.find(x=>x?.type === 'summary'));
  return initiallyVisible(n.props?.children);
}
const task={id:'qa1', title:'[req-0557438ef182e8847164] Проверить миграцию', status:'blocked', assignee:'team-qa',body:'INTERNAL_PROMPT '.repeat(200),skills:[]};
const teamTask={id:'qa1',status:'blocked',assignment_state:'pending',selection_reason:'Выбор ресурса: pool anthropic-main reached max_concurrent'};
const detail=api.TaskDetail({data:{task,comments:[1,2,3,4].map(id=>({id,body: id===4 ? 'LATEST_LONG_COMMENT '.repeat(100):'comment '+id})).concat([{id:5,author:'team-controller',body:'Назначение по доступному лимиту: {"pool":"RAW_POOL_METADATA"}'}]),events:[],links:{parents:[],children:[]},runs:[]},teamTask,allTasks:[],renderMarkdown:false});
const visible=initiallyVisible(detail);
assert.match(visible,/Ожидает свободный слот/);
assert.match(visible,/От вас ничего не требуется/);
assert.match(visible,/Исполнитель QA/);
assert.doesNotMatch(visible,/INTERNAL_PROMPT|req-0557438/);
assert.doesNotMatch(visible,/comment 1/,'old comments stay collapsed');
assert.doesNotMatch(visible,/RAW_POOL_METADATA/,'machine assignment messages stay in technical details');
assert.ok(visible.length < 1000, 'initial detail stays readable even with long prompt and comments');
const technical=nodes(detail,n=>n.type==='details').find(n=>text(n.props.children[0])==='Технические подробности');
assert.ok(technical && !technical.props.open);
for (const name of ['Данные задачи','Управление задачей','Инструкция агенту','Связи и зависимости']) {
  assert.ok(nodes(technical,n=>n.type==='details').some(n=>text(n.props.children[0])===name));
}
const editing=api.TaskDetail({data:{task,links:{parents:[],children:[]}},editing:true,setEditing(){},onPatch(){},allTasks:[]});
assert.ok(nodes(editing,n=>n.type?.name === 'TitleEditor').length,'title editing survives');
const card=api.TaskCard({task:{...task,_team:teamTask},allTasks:[]});
assert.match(text(card),/Ожидает свободный слот/);
assert.match(text(card),/QA/);
assert.doesNotMatch(text(card),/req-0557438/);
assert.equal(task.title,'[req-0557438ef182e8847164] Проверить миграцию','display cleanup must not mutate stored title');
const cols=[{name:'triage',tasks:[]},{name:'running',tasks:[{}]},{name:'blocked',tasks:[{}]},{name:'done',tasks:[{}]}];
assert.deepEqual(Array.from(api.operatorColumns(cols,{}),c=>c.name),['running','blocked']);
assert.deepEqual(Array.from(api.operatorColumns(cols,{showDone:true}),c=>c.name),['running','blocked','done']);
assert.deepEqual(Array.from(api.operatorColumns(cols,{forceHistory:true}),c=>c.name),['running','blocked','done'],'search/filters include finished tasks');
assert.equal(api.operatorColumns(cols,{showAllColumns:true}).length,4,'all stages remain accessible');
assert.equal(api.operatorColumns([{name:'triage',tasks:[]},{name:'done',tasks:[{}]}],{})[0].name,'triage','no blank board');
const switcher=api.BoardSwitcher({board:'engineering-team',boardList:[{slug:'default',total:0},{slug:'engineering-team',name:'Команда',total:0}],totalTasks:32});
assert.match(text(switcher),/32 задач/,'counter comes from displayed board, not stale metadata');
const mapping=api.mapTeamTasks({requests:[{id:'req-a',stage:'awaiting_owner_input',tasks:[{id:'qa1',role:'qa'}]}]});
assert.equal(mapping.qa1.request_id,'req-a');
assert.equal(api.operatorStatus(task,{teamTask:mapping.qa1}).href,'/engineering-team?request=req-a');
const root={id:'root',status:'blocked',assignee:'team-controller',created_by:'hermes-team-controller',idempotency_key:'req-a:root:r6'};
const rootMapping=api.mapTeamTasks({requests:[{id:'req-a',stage:'awaiting_owner_input',tasks:[]}]},{columns:[{tasks:[root,{...root,id:'fake',idempotency_key:'unrelated'}]}]});
assert.equal(api.operatorStatus(root,{teamTask:rootMapping.root}).title,'Нужен ваш ответ','human gate is visible on controller cards omitted from role assignments');
assert.equal(rootMapping.fake,undefined,'a title or role alone cannot bind another request');
(async()=>{
 let inFlight=0,maxFlight=0,calls=0;
 fetchImpl=async(url)=>{ calls++;inFlight++;maxFlight=Math.max(maxFlight,inFlight);await new Promise(r=>setTimeout(r,2));inFlight--;return {task:{id:url.split('/').pop().split('?')[0],status:'blocked'},links:{parents:['parent']},runs:[]}; };
 const data={columns:[{tasks:Array.from({length:9},(_,i)=>({id:'b'+i,status:'blocked',comment_count:0}))}]};
 const cache=new Map();
 const contexts=await api.loadBlockedContexts(data,'engineering-team',cache);
 assert.equal(Object.keys(contexts).length,9);assert.ok(maxFlight<=4);assert.equal(calls,9);
 await api.loadBlockedContexts(data,'engineering-team',cache);assert.equal(calls,9,'unchanged cards reuse diagnostic cache');
 data.columns[0].tasks[0].comment_count=1;await api.loadBlockedContexts(data,'engineering-team',cache);assert.equal(calls,10,'new comment invalidates affected card only');
 fetchImpl=async()=>{throw new Error('optional diagnostics unavailable');};
 const missing=await api.loadBlockedContexts(data,'another-board',cache);assert.equal(Object.keys(missing).length,0,'no stale diagnostics across boards or failures');
 assert.equal(api.operatorStatus({status:'blocked'},{}).title,'Причина требует уточнения');
const testWaitCard={id:'backend1',status:'blocked',assignee:'team-backend'};
const testWaitTeam={id:'backend1',status:'waiting_tests',test_wait:{kind:'backend',job_status:'waiting'}};
const waitingStatus=api.operatorStatus(testWaitCard,{teamTask:testWaitTeam});
assert.equal(waitingStatus.title,'Ожидает проверку');
assert.equal(waitingStatus.reason,'Проверка выполняется отдельным заданием. Бюджет агента и слот свободны.');
assert.match(waitingStatus.action,/Действие не требуется/);
 console.log('Kanban usability UI tests passed');
})().catch(e=>{console.error(e);process.exitCode=1;});
