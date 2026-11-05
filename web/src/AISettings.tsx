import React, { useEffect, useState } from 'react';
import { api } from './api';
import './AISettings.css';
import QuotaFields from './QuotaFields';
import type {Quotas} from './QuotaFields';

type Plan = Quotas & {id:string;version:string;name:string;amount_minor:number;currency:string;storage_gib:number;application_limit:number;application_cpu_millicores:number;application_memory_mib:number;code_runs_monthly:number;code_timeout_seconds:number;code_concurrency:number;model_credit_cny_minor:number;description:string;features:string[]};
type Policy = {chat_model:string;coding_model:string;allowed_models:string[];cny_per_usd:string;plans:Plan[]};
type State = {revision:number;policy:Policy;catalog:{name:string;input_cost_per_token:number|null;output_cost_per_token:number|null}[]};

export default function AISettings() {
  const [state,setState]=useState<State>();
  const [error,setError]=useState('');
  const [busy,setBusy]=useState(false);
  const [saved,setSaved]=useState(false);
  async function load(){setError('');try{setState(await api<State>('/v1/admin/ai'));}catch{setError('模型配置暂时无法加载，请检查管理员权限和 LiteLLM 连接。');}}
  useEffect(()=>{void load();},[]);
  function edit(change:Partial<Policy>){setSaved(false);setState(value=>value?{...value,policy:{...value.policy,...change}}:value);}
  function plan(index:number,change:Partial<Plan>){if(state)edit({plans:state.policy.plans.map((value,i)=>i===index?{...value,...change}:value)});}
  return <section className="panel ai-settings" aria-label="套餐与配额管理">
    <div className="panel-heading"><div><h2>套餐与配额管理</h2><p>管理售价、模型额度与资源权益。用户界面展示已开通的权益，目录改价不会修改已有订单。</p></div><button type="button" className="subtle" disabled={busy} onClick={()=>void load()}>刷新配置</button></div>
    {error&&<p role="alert" className="error">{error}</p>}
    {!state?<p>正在读取模型目录…</p>:<form className="ai-policy-form" onSubmit={event=>{event.preventDefault();setBusy(true);setError('');setSaved(false);void api<{revision:number;policy:Policy}>('/v1/admin/ai','PUT',{revision:state.revision,policy:state.policy}).then(value=>{setState({...state,...value});setSaved(true);}).catch(()=>setError('保存未完成。配置可能已被其他管理员更新，请刷新后核对；原配置继续生效。')).finally(()=>setBusy(false));}}>
      <div className="ai-settings-grid">
        <label>普通聊天默认模型<select aria-label="普通聊天默认模型" value={state.policy.chat_model} onChange={event=>edit({chat_model:event.target.value,allowed_models:Array.from(new Set([...state.policy.allowed_models,event.target.value]))})}>{state.catalog.map(value=><option key={value.name}>{value.name}</option>)}</select></label>
        <label>OpenHands 编程默认模型<select aria-label="OpenHands 编程默认模型" value={state.policy.coding_model} onChange={event=>edit({coding_model:event.target.value,allowed_models:Array.from(new Set([...state.policy.allowed_models,event.target.value]))})}>{state.catalog.map(value=><option key={value.name}>{value.name}</option>)}</select></label>
        <label>结算汇率（人民币 / 美元）<input aria-label="结算汇率" type="number" min="0.01" max="1000" step="0.000001" required value={state.policy.cny_per_usd} onChange={event=>edit({cny_per_usd:event.target.value})}/></label>
      </div>
      <p>这是平台结算规则，非实时外汇报价。充值金额按订单创建时的汇率折算；已有订单不随本次修改变化。默认模型修改后，新对话自动使用；已有对话可在输入框中切换。</p>
      <fieldset><legend>允许用户选择的真实模型</legend><div className="ai-model-list">{state.catalog.map(model=><label key={model.name}><input type="checkbox" checked={state.policy.allowed_models.includes(model.name)} disabled={[state.policy.chat_model,state.policy.coding_model].includes(model.name)} onChange={event=>edit({allowed_models:event.target.checked?[...state.policy.allowed_models,model.name]:state.policy.allowed_models.filter(name=>name!==model.name)})}/>{model.name}<small>输入 ${model.input_cost_per_token==null?'未知':(model.input_cost_per_token*1000000).toFixed(3)} / 输出 ${model.output_cost_per_token==null?'未知':(model.output_cost_per_token*1000000).toFixed(3)}，每百万 tokens</small></label>)}</div></fieldset>
      <h3>套餐权益</h3><p>注册赠送 1 美元，仅一次。保存目录 → 用户确认模拟订单 → 模型额度到账 → 资源开通完成 → 用户权益更新。资源权益不相加、不因重复充值降级；代码当月已用次数不清零。当前没有自动续费或到期销毁。</p>
      <div className="quota-plans">{state.policy.plans.map((value,index)=><fieldset key={value.id}><legend>{value.name} · {value.id}</legend>
        <section className="quota-group"><h4>价格与模型额度</h4>
        <label>套餐名称<input required maxLength={60} value={value.name} onChange={event=>plan(index,{name:event.target.value})}/></label>
        <label>售价（元）<input type="number" min="0" step="0.01" required value={value.amount_minor/100} onChange={event=>plan(index,{amount_minor:Math.round(Number(event.target.value)*100)})}/></label>
        <label>包含的模型额度（元）<input type="number" min="0" step="0.01" required value={(value.model_credit_cny_minor??0)/100} onChange={event=>plan(index,{model_credit_cny_minor:Math.round(Number(event.target.value)*100)})}/></label>
        <p className="quota-help">售价 ¥{(value.amount_minor/100).toFixed(2)}，模型额度 ¥{((value.model_credit_cny_minor??0)/100).toFixed(2)}，按当前结算汇率预计增加 ${(((value.model_credit_cny_minor??0)/100)/(Number(state.policy.cny_per_usd)||1)).toFixed(6)}。实际以订单快照为准。</p></section>
        <section className="quota-group"><h4>存储与单应用规格</h4>
        <label>逻辑总容量（GiB）<input type="number" min="1" max="40" required value={value.storage_gib} onChange={event=>plan(index,{storage_gib:Number(event.target.value)})}/></label>
        <p className="quota-help">文件、依赖缓存、数据库、对象和镜像实际用量共同计入总量。分项上限不叠加；跨服务统一硬写入限制尚未启用。</p>
        <label>发布应用数<input type="number" min="1" max="100" required value={value.application_limit} onChange={event=>plan(index,{application_limit:Number(event.target.value)})}/></label>
        <label>单应用 CPU 上限（核）<input type="number" min="0.05" max="4" step="0.001" required value={value.application_cpu_millicores/1000} onChange={event=>plan(index,{application_cpu_millicores:Math.round(Number(event.target.value)*1000)})}/></label>
        <label>单应用内存上限（MiB）<input type="number" min="128" max="8192" step="1" required value={value.application_memory_mib} onChange={event=>plan(index,{application_memory_mib:Number(event.target.value)})}/></label>
        </section><QuotaFields value={value} onChange={change=>plan(index,change)}/>
        <section className="quota-group"><h4>聊天中的运行代码</h4>
        <label>每月代码运行次数<input type="number" min="0" max="10000" required value={value.code_runs_monthly} onChange={event=>plan(index,{code_runs_monthly:Number(event.target.value)})}/></label>
        <label>单次代码最长时间（秒）<input type="number" min="1" max="15" required value={value.code_timeout_seconds} onChange={event=>plan(index,{code_timeout_seconds:Number(event.target.value)})}/></label>
        <label>同时运行代码数<input type="number" min="1" max="2" required value={value.code_concurrency} onChange={event=>plan(index,{code_concurrency:Number(event.target.value)})}/></label>
        <p className="quota-help">按北京时间自然月计次。当前执行引擎：单次程序内存固定 256 MiB，最长 15 秒，并发上限 2；这些是支持范围，不可填写未实现的档位。注册默认 30 次/月、10 秒/次、并发 1。</p></section>
        <small>每个发布应用的硬上限，编程与构建任务另计。修改影响新订单；已有用户保留已生效的权益，购买较低套餐充值不会降级。</small>
        <label>适用人群<input maxLength={300} value={value.description??''} onChange={event=>plan(index,{description:event.target.value})}/></label>
        <label>详细说明（每行一项）<textarea rows={4} value={(value.features??[]).join('\n')} onChange={event=>plan(index,{features:event.target.value.split('\n').filter(Boolean)})}/></label>
      </fieldset>)}</div>
      <div><button disabled={busy}>{busy?'正在保存…':'保存套餐与配额'}</button>{saved&&<span role="status"> 已保存新目录，用于后续订单。已有用户权益和余额未改变；模型权限将在后台同步。</span>}</div>
    </form>}
  </section>;
}
