const bridge = window.AstrBotPluginPage;
const notice = document.querySelector('#notice');
const container = document.querySelector('#repos');
const labels = {pending:'等待发送',running:'发送中',success:'已送达',failed:'失败',unknown:'结果未知',cancelled:'已取消'};
const modes = {group:'群聊',private:'私聊',both:'群聊 + 私聊'};
let refreshing = false;
let busy = false;
function node(tag, text, cls) {const e=document.createElement(tag);if(text!==undefined)e.textContent=text;if(cls)e.className=cls;return e;}
function date(value) {return value ? new Date(value*1000).toLocaleString() : '—';}
function confirmRetry() {
  return new Promise(resolve=>{
    const dialog=node('dialog');dialog.append(node('p','结果未知的消息可能已经送达。重试可能产生重复消息，是否继续？'));
    for(const [text,result] of [['取消',false],['仍然重试',true]]){const b=node('button',text);b.onclick=()=>{dialog.close();dialog.remove();resolve(result);};dialog.append(b);}
    dialog.oncancel=()=>{dialog.remove();resolve(false);};document.body.append(dialog);dialog.showModal();
  });
}
async function action(button, endpoint, payload) {
  busy=true;button.disabled=true;
  try {const result=await bridge.apiPost(endpoint,payload);
    notice.textContent=endpoint==='check' ? `${result.configuration_ok?'配置完整':'配置需检查'} · ${result.reason}；公网可达性以真实 GitHub 请求为准。` : '任务已加入队列，下方将显示实际结果。';
  } catch(e) {notice.textContent=`操作失败：${e.message}`;}
  finally {busy=false;button.disabled=false;await refresh();}
}
function button(text, endpoint, payload) {const b=node('button',text);b.onclick=()=>action(b,endpoint,payload);return b;}
function render(data) {
  container.replaceChildren();
  if(!data.repositories.length){container.append(node('p','尚未配置仓库，请先在插件配置中添加仓库和接收者。','empty'));return;}
  for(const repo of data.repositories){
    const card=node('section',undefined,'card');card.append(node('h2',repo.name));
    const details=node('div',undefined,'details');
    const c=repo.connection;
    for(const text of [
      `通知模式：${modes[repo.mode]} · 群号：${repo.group_id||'—'}`,
      `接收者 QQ：${repo.owners.join('、')||'未配置'}`,
      `平台：${repo.platform} · 机器人：${repo.bot_qq||'自动选择'}`,
      `连接：${c?c.reason:'尚未检查'}${c?'（'+date(c.checked_at)+'）':''}`,
      `Webhook 监听：${data.listening?'运行中':'未启动'}`,
      repo.received ? `GitHub 已连通：${date(repo.received.received)} · ${repo.received.event}` : 'GitHub：尚未收到通过签名校验的请求'
    ])details.append(node('div',text));
    card.append(details);
    const actions=node('div',undefined,'actions');actions.append(button('检查配置','check',{repository:repo.name}));
    for(const kind of ['group','private'])if(repo.mode===kind||repo.mode==='both')actions.append(button(kind==='group'?'测试群聊与表情':'测试私聊','test',{repository:repo.name,kind}));
    card.append(actions);
    if(!repo.tasks.length)card.append(node('p','暂无通知记录。点击测试可验证 QQ 送达；GitHub 连通需在仓库端投递 Webhook。','empty'));
    else {
      const scroll=node('div',undefined,'scroll');const table=node('table');const head=node('tr');
      for(const title of ['任务 / 目标','状态','尝试 / 耗时','时间','操作'])head.append(node('th',title));table.append(head);
      for(const t of repo.tasks){
        const row=node('tr');const target=node('td',`${t.test?'测试':t.number?'PR #'+t.number:'连接确认'} · ${t.operation==='reaction'?'贴表情':'消息'}`);
        target.append(node('small',`${t.kind==='group'?'群':'私聊'} ${t.target} · 任务 ${t.id}`));
        const state=node('td');state.append(node('span',labels[t.status],`badge ${t.status}`));if(t.error)state.append(node('small',t.error));if(t.status==='pending'&&t.blocked_by)state.append(node('small',`等待前序任务 ${t.blocked_by}`));
        const timing=node('td',`${t.attempts} 次 / ${t.duration===null?'—':t.duration.toFixed(2)+' 秒'}`);
        const when=node('td',date(t.updated));if(t.status==='pending'&&t.next_at)when.append(node('small','重试：'+date(t.next_at)));
        const ops=node('td');if(['failed','unknown'].includes(t.status)){const b=node('button','重试失败项');b.onclick=async()=>{if(t.status==='unknown'&&!await confirmRetry())return;await action(b,'retry',{task_id:t.id});};ops.append(b);}
        row.append(target,state,timing,when,ops);table.append(row);
      }scroll.append(table);card.append(scroll);
    }container.append(card);
  }
}
async function refresh(){if(refreshing)return;refreshing=true;try{render(await bridge.apiGet('status'));}catch(e){notice.textContent=`状态读取失败：${e.message}`;}finally{refreshing=false;}}
document.querySelector('#refresh').onclick=refresh;
try{await bridge.ready();notice.textContent='配置检查不会发送消息；测试按钮会向已配置目标发送明确标注的测试通知。';await refresh();setInterval(()=>{if(!document.hidden&&!busy&&!document.querySelector('dialog'))refresh();},3000);}catch(e){notice.textContent=`无法连接 AstrBot：${e.message}`;}
