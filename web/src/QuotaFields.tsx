export type Quotas = {
  applications_cpu_millicores?: number; applications_memory_mib?: number;
  database_budget_mib?: number; database_per_instance_mib?: number; database_limit?: number;
  agent_cpu_millicores?: number; agent_memory_mib?: number; agent_command_seconds?: number;
  agent_task_seconds?: number; agent_concurrency?: number; build_cpu_millicores?: number;
  build_memory_mib?: number; build_timeout_seconds?: number; build_concurrency?: number;
};

type Field = [keyof Quotas, string, number, number, number?];
const groups: {title:string;help:string;fields:Field[]}[] = [
  {title:'发布应用 · 合计额度',help:'按已分配规格预留，并非瞬时 CPU 使用率。停止完成后释放额度；发布失败仍有旧版本运行时继续占用。',fields:[
    ['applications_cpu_millicores','所有应用 CPU 合计（核）',0.05,32,1000],
    ['applications_memory_mib','所有应用内存合计（MiB）',128,65536]]},
  {title:'数据库',help:'包含在套餐逻辑总容量内。当前实施申请额度与超量告警，不是数据库硬写入限制。单库额度用于新建数据库预留，已有库不自动缩容。',fields:[
    ['database_budget_mib','数据库总预算（MiB）',0,40960],
    ['database_per_instance_mib','单库预留 / 警戒额度（MiB）',0,40960],
    ['database_limit','最多数据库数',0,100]]},
  {title:'OpenHands 编程',help:'仅适用于新提交任务。不同工程可并发，同一目录及其子目录禁止同时修改。等待审批包含在任务总时限内。',fields:[
    ['agent_cpu_millicores','命令 CPU 上限（核）',0.1,4,1000],
    ['agent_memory_mib','命令内存上限（MiB）',128,8192],
    ['agent_command_seconds','单条命令最长时间（秒）',10,1800],
    ['agent_task_seconds','单个编程任务最长时间（秒）',60,7200],
    ['agent_concurrency','同时进行的编程任务',1,2]]},
  {title:'镜像构建',help:'构建独立于聊天代码运行次数。内存需包含构建缓存开销；集群容量不足时仍可能排队。',fields:[
    ['build_cpu_millicores','构建 CPU 上限（核）',0.25,4,1000],
    ['build_memory_mib','构建内存上限（MiB）',1536,8192],
    ['build_timeout_seconds','单次构建最长时间（秒）',60,1800],
    ['build_concurrency','同时构建任务数',1,4]]},
];

export default function QuotaFields({value,onChange}:{value:Quotas;onChange:(change:Partial<Quotas>)=>void}) {
  return <>{groups.map(group=><details className="quota-group quota-details" key={group.title}>
    <summary>{group.title}<span>{group.fields.slice(0,2).map(([key,label,,,scale=1])=>`${label.includes('CPU')?'CPU':label.includes('内存')?'内存':label.split('（')[0]} ${value[key]==null?'—':value[key]!/scale}${scale===1000?' 核':key.endsWith('_mib')?' MiB':''}`).join(' · ')}</span></summary><p className="quota-help">{group.help}</p>
    <div className="quota-fields">{group.fields.map(([key,label,min,max,scale=1])=><label key={key}>{label}
      <input type="number" required min={min} max={max} step={scale===1000?0.001:1}
        onInvalid={event=>{const details=event.currentTarget.closest('details');if(details)details.open=true;}}
        value={value[key]==null?'':value[key]!/scale}
        onChange={event=>onChange({[key]:Math.round(Number(event.target.value)*scale)})}/>
    </label>)}</div>
  </details>)}</>;
}
