const $ = id => document.getElementById(id);
const labels={idle:"已连接，等待任务",working:"正在采集",offline:"离线",waiting_for_verification:"等待知网验证",paused:"已暂停",browser_closed:"知网标签页已关闭",error:"暂时无法采集",disconnected:"请重新连接"};
async function render(){const cfg=await chrome.storage.local.get(null);$("pair").hidden=!!cfg.token;$("connected").hidden=!cfg.token;$("status").textContent=cfg.detail||labels[cfg.state]||"已连接";if(cfg.server)$("server").value=cfg.server;}
async function send(type,extra={}){const result=await chrome.runtime.sendMessage({type,...extra});if(result?.error)throw new Error(result.error);await render();}
$("pair").addEventListener("submit",async event=>{event.preventDefault();$("error").textContent="";try{const server=new URL($("server").value).origin;const allowed=await chrome.permissions.request({origins:[server+"/*"]});if(!allowed)throw new Error("需要允许扩展连接你填写的平台地址");await send("pair",{server,code:$("code").value.trim()});$("code").value="";}catch(error){$("error").textContent=error.message;}});
for(const type of ["open","pause","resume"])$(type).addEventListener("click",()=>send(type).catch(error=>{$("error").textContent=error.message;}));
$("change").addEventListener("click",()=>{$("pair").hidden=false;});
chrome.storage.onChanged.addListener(()=>void render());
void render();
