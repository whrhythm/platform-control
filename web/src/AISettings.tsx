import { useEffect, useState } from 'react';
import { api } from './api';
import './AISettings.css';
import QuotaFields from './QuotaFields';
import type { Quotas } from './QuotaFields';
type Plan = Quotas & {id:string;version:string;name:string;amount_minor:number;currency:string;storage_gib:number;application_limit:number;application_cpu_millicores:number;application_memory_mib:number;code_runs_monthly:number;code_timeout_seconds:number;code_concurrency:number;model_credit_cny_minor:number;description:string;features:string[]};
type Policy = {chat_model:string;coding_model:string;allowed_models:string[];cny_per_usd:string;plans:Plan[];trial_plan:Plan};
type State = {revision:number;policy:Policy;catalog:{name:string;input_cost_per_token:number|null;output_cost_per_token:number|null}[]};
function NumberField({label,value,min=0,max=1000000,step=1,onChange}:{label:string;value:number;min?:number;max?:number;step?:number;onChange:(value:number)=>void}){
  return <label>{label}<input type="number" required min={min} max={max} step={step} value={value} onChange={event=>onChange(Number(event.target.value))}/></label>;
}
export default function AISettings({view='plans',onDirty=()=>{}}:{view?:'plans'|'models';onDirty?:(dirty:boolean)=>void}){
  const [state,setState]=useState<State>();const [error,setError]=useState('');const [busy,setBusy]=useState(false);const [saved,setSaved]=useState(false);const [dirty,setDirty]=useState(false);const [selected,setSelected]=useState('trial');
  function mark(value:boolean){setDirty(value);onDirty(value);}
  async function load(){setError('');setBusy(true);try{setState(await api<State>('/v1/admin/ai'));mark(false);setSaved(false);}catch{setError('配置加载失败，请检查管理员权限或稍后重试。');}finally{setBusy(false);}}
  useEffect(()=>{void load();},[]);
  useEffect(()=>{if(!dirty)return;const warn=(e:BeforeUnloadEvent)=>{e.preventDefault();e.returnValue='';};window.addEventListener('beforeunload',warn);return()=>window.removeEventListener('beforeunload',warn);},[dirty]);
  function edit(change:Partial<Policy>){mark(true);setSaved(false);setState(value=>value?{...value,policy:{...value.policy,...change}}:value);}
  const policy=state?.policy;
  const plans=policy?[policy.trial_plan,...policy.plans]:[];
  const current=plans.find(value=>value.id===selected)??plans[0];
  function editPlan(change:Partial<Plan>){if(!policy||!current)return;if(current.id==='trial')edit({trial_plan:{...current,...change}});else edit({plans:policy.plans.map(value=>value.id===current.id?{...value,...change}:value)});}
  const trial=current?.id==='trial';
  return <section className="panel ai-settings" aria-label={view==='plans'?'套餐与配额管理':'模型与结算设置'}>
    <div className="panel-heading"><div><h2>{view==='plans'?'套餐权益配置':'模型与美元预算'}</h2><p>{view==='plans'?'选择套餐编辑。现有用户继续保留已经开通的权益。':'默认模型用于新对话；用户仍可在聊天中选择已开放的模型。'}</p></div><button type="button" className="subtle" disabled={busy} onClick={()=>{if(!dirty||window.confirm('刷新会放弃尚未保存的修改，是否继续？'))void load();}}>刷新配置</button></div>
    {error&&<div role="alert" className="error">{error}</div>}
    {!state||!policy?<p role="status">正在读取配置…</p>:<form className="ai-policy-form" onSubmit={event=>{event.preventDefault();setBusy(true);setError('');setSaved(false);void api<{revision:number;policy:Policy}>('/v1/admin/ai','PUT',{revision:state.revision,policy}).then(value=>{setState({...state,...value});setSaved(true);mark(false);}).catch(e=>setError(e instanceof Error?e.message:'保存失败，请核对后重试。')).finally(()=>setBusy(false));}}>
      <fieldset className="policy-controls" disabled={busy}>
      {view==='models'?<>
        <div className="ai-settings-grid">{(['chat_model','coding_model'] as const).map(key=><label key={key}>{key==='chat_model'?'普通聊天默认模型':'OpenHands 编程默认模型'}<select value={policy[key]} onChange={e=>edit({[key]:e.target.value,allowed_models:Array.from(new Set([...policy.allowed_models,e.target.value]))})}>{state.catalog.map(model=><option key={model.name}>{model.name}</option>)}</select></label>)}</div>
        <section className="quota-group"><h3>美元预算折算</h3><div className="exchange-row"><span>1 美元 =</span><input aria-label="每美元折合人民币" type="number" min="0.01" max="1000" step="0.000001" required value={policy.cny_per_usd} onChange={e=>edit({cny_per_usd:e.target.value})}/><span>元人民币</span></div>
          <p className="conversion-example">例如：套餐售价 ¥10，包含 ¥5 模型额度，LiteLLM 增加 <strong>${(Math.floor(5/(Number(policy.cny_per_usd)||1)*1e6)/1e6).toFixed(6)} 美元</strong>预算。</p>
          <p className="quota-help">套餐售价和模型额度分别设置；折算的是模型额度。LiteLLM 以美元计费，不是直接增加人民币余额。这是平台结算规则，不是实时汇率，已有订单保留创建时的汇率。</p></section>
        <section className="quota-group"><h3>用户可选模型</h3><div className="ai-model-list">{state.catalog.map(model=><label key={model.name}><input type="checkbox" checked={policy.allowed_models.includes(model.name)} disabled={[policy.chat_model,policy.coding_model].includes(model.name)} onChange={e=>edit({allowed_models:e.target.checked?[...policy.allowed_models,model.name]:policy.allowed_models.filter(name=>name!==model.name)})}/><span>{model.name}<small>每百万 tokens：输入 ${model.input_cost_per_token==null?'未知':(model.input_cost_per_token*1000000).toFixed(3)} · 输出 ${model.output_cost_per_token==null?'未知':(model.output_cost_per_token*1000000).toFixed(3)}</small></span></label>)}</div></section>
      </>:current&&<>
        <div className="plan-switcher" role="group" aria-label="选择要编辑的套餐">{plans.map(value=><button type="button" key={value.id} aria-pressed={current.id===value.id} className={current.id===value.id?'selected':''} onClick={()=>setSelected(value.id)}><strong>{value.name}</strong><span>{value.id==='trial'?'新用户默认权益':`¥${(value.amount_minor/100).toFixed(2)} / 次`}</span></button>)}</div>
        <div className="plan-context"><span className="badge">{trial?'首次开通生效':'新订单生效'}</span><span>{trial?'新用户自动获得，已有用户不重置、不补发。注册一次性赠送 $1 美元模型预算，赠金固定。':'用户确认订单、模型额度到账并完成资源开通后生效；修改目录不追溯已有订单。'}</span></div>
        <section className="quota-group"><h3>{trial?'体验套餐':'价格与模型额度'}</h3><div className="quota-fields">
          <label>套餐名称<input required maxLength={60} value={current.name} onChange={e=>editPlan({name:e.target.value})}/></label>
          {!trial&&<><NumberField label="套餐售价（元人民币）" value={current.amount_minor/100} step={0.01} onChange={value=>editPlan({amount_minor:Math.round(value*100)})}/><NumberField label="包含的模型额度（元人民币）" value={current.model_credit_cny_minor/100} step={0.01} onChange={value=>editPlan({model_credit_cny_minor:Math.round(value*100)})}/></>}
        </div>{!trial&&<p className="conversion-example">模型额度 ¥{(current.model_credit_cny_minor/100).toFixed(2)} ÷ {Number(policy.cny_per_usd)} = <strong>${(Math.floor(current.model_credit_cny_minor/100/(Number(policy.cny_per_usd)||1)*1e6)/1e6).toFixed(6)} 美元预算</strong>。按订单创建时的配置结算。</p>}</section>
        <section className="quota-group"><h3>存储与单应用规格</h3><div className="quota-fields">
          <NumberField label="逻辑总容量（GiB）" min={1} max={40} value={current.storage_gib} onChange={value=>editPlan({storage_gib:value})}/>
          <NumberField label="发布应用数" min={1} max={100} value={current.application_limit} onChange={value=>editPlan({application_limit:value})}/>
          <NumberField label="单应用 CPU 上限（核）" min={0.05} max={4} step={0.001} value={current.application_cpu_millicores/1000} onChange={value=>editPlan({application_cpu_millicores:Math.round(value*1000)})}/>
          <NumberField label="单应用内存上限（MiB）" min={128} max={8192} value={current.application_memory_mib} onChange={value=>editPlan({application_memory_mib:value})}/>
        </div><p className="quota-help">文件、依赖缓存、数据库、对象和镜像共同计入逻辑总容量；分项额度不额外增加总容量。跨服务硬写入限制尚未启用。</p></section>
        <QuotaFields value={current} onChange={editPlan}/>
        <section className="quota-group"><h3>聊天中的运行代码</h3><div className="quota-fields">
          <NumberField label="每月代码运行次数" max={10000} value={current.code_runs_monthly} onChange={value=>editPlan({code_runs_monthly:value})}/>
          <NumberField label="单次最长时间（秒）" min={1} max={15} value={current.code_timeout_seconds} onChange={value=>editPlan({code_timeout_seconds:value})}/>
          <NumberField label="同时运行代码数" min={1} max={2} value={current.code_concurrency} onChange={value=>editPlan({code_concurrency:value})}/>
        </div><p className="quota-help">按北京时间自然月计次，升级不清零当月已用次数。当前运行引擎单次内存固定 256 MiB。</p></section>
        <details className="quota-group"><summary>套餐展示文案</summary><div className="copy-fields"><label>适用人群<input maxLength={300} value={current.description} onChange={e=>editPlan({description:e.target.value})}/></label><label>详细说明（每行一项，最多 12 项）<textarea rows={3} value={current.features.join('\n')} onChange={e=>editPlan({features:e.target.value.split('\n')})}/></label></div></details>
      </>}
      </fieldset>
      <div className="policy-actions"><button disabled={busy||!dirty}>{busy?'正在保存…':'保存配置'}</button><span role="status">{saved?'已保存。新配置用于后续开通；已有用户权益和余额保持不变。':dirty?'有未保存的修改':`配置版本 ${state.revision}`}</span></div>
    </form>}
  </section>;
}
