import { spawn, type ChildProcessWithoutNullStreams } from 'node:child_process';
import { open, rm } from 'node:fs/promises';
import { createInterface } from 'node:readline';

export type Step = { id:string; tool:string; source?:string; script?:string; action?:string; inputs?:Record<string,unknown>;
  after?:string[]; bindings?:Record<string,{step:string;path?:(string|number)[];file?:string}>; timeout?:number; requirements?:Record<string,number>;
  optimize?:Record<string,unknown>; point?:Record<string,unknown>; points?:{id:string;point:Record<string,unknown>;inputs:Record<string,unknown>}[] };
export type Submission = {run_id:string;request_id:string;state?:string;counts?:Record<string,number>;result?:unknown;files?:string[];output?:string;error?:string};
export type SweepPoint = {step:string;id:string;state:string;point:Record<string,unknown>;values:Record<string,unknown>;error?:string;output?:string};
export type SweepTable = {run_id:string;points:number;states:Record<string,number>;swept:string[];values:string[];rows:SweepPoint[];
  next_offset:number|null;best?:(SweepPoint&{goal:string;path:string})|null;saved?:string|null};
export type RunState = 'queued'|'running'|'succeeded'|'failed'|'cancelled'|'blocked'|'interrupted';
export type RunSummary = {id:string;name:string;state:RunState;created:number;finished:number|null;tools:string[];counts:Record<string,number>};
export type RunPage = {runs:RunSummary[];next_offset:number|null;next_before:string|null};
export type SavedProgress = {path:string;kind:'program'|'optimization'|'points'} &
  ({saved:number;files:number;error?:never}|{error:string;saved?:never;files?:never});
export type Integration = {id:string;distribution:string;version:string;entry:string;software:string[];programs:string[];
  guides:{name:string;title:string;language:string;tools:string[]}[]};
export type IntegrationPage = {packages:Integration[];errors:{id:string;distribution:string;version:string;entry:string;error:string}[]};
export type RunStatus = {id:string;state:string;counts:Record<string,number>;next_offset:number|null;
  steps:{id:string;state:string;output?:string;result?:unknown;error?:string;attempt:number;progress?:SavedProgress[]}[]};
export type Options = {command?:string;args?:string[];home?:string;env?:NodeJS.ProcessEnv;timeout?:number};
type Pending = {resolve:(value:any)=>void;reject:(error:Error)=>void;timer:ReturnType<typeof setTimeout>;release:()=>void};
export type RequestOptions = {signal?:AbortSignal};

/** One connection per host. Accepted calculations outlive this client. */
export class OpticsLab {
  private readonly child:ChildProcessWithoutNullStreams;
  private readonly pending=new Map<number,Pending>();
  private sequence=0;
  private closed=false;
  private stderr='';
  private constructor(private readonly options:Options) {
    const env={...process.env,...options.env,...(options.home ? {OPTICS_HOME:options.home}: {})};
    this.child=spawn(options.command ?? (process.platform==='win32'?'python':'python3'),
      options.args ?? ['-m','optics_photonics','mcp'],{env,stdio:'pipe',windowsHide:true});
    const lines=createInterface({input:this.child.stdout});
    lines.on('line',line=>{
      try {
        const reply=JSON.parse(line);
        const request=this.pending.get(reply.id);
        if (!request) return;
        this.pending.delete(reply.id);clearTimeout(request.timer);request.release();
        if (reply.error) request.reject(new Error(reply.error.message));
        else request.resolve(reply.result);
      } catch(error) { this.fail(new Error('Invalid MCP response: '+String(error)));this.child.kill(); }
    });
    this.child.stderr.on('data',data=>{this.stderr=(this.stderr+String(data)).slice(-4000);});
    this.child.on('error',error=>this.fail(error));
    this.child.on('close',code=>this.fail(new Error(`Optics connection closed (${code ?? 'signal'}): ${this.stderr}`)));
    this.child.stdin.on('error',error=>this.fail(error));
  }
  static async connect(options:Options={}) {
    const client=new OpticsLab(options);
    try {
      await client.request('initialize',{protocolVersion:'2025-11-25',capabilities:{},clientInfo:{name:'optics-typescript',version:'0.11.1'}});
      client.notify('notifications/initialized');
      return client;
    } catch(error) { client.close(); throw error; }
  }
  private fail(error:Error) {
    this.closed=true;
    for (const request of this.pending.values()) {clearTimeout(request.timer);request.release();request.reject(error);}
    this.pending.clear();
  }
  private notify(method:string,params?:unknown) {
    if(!this.closed) this.child.stdin.write(JSON.stringify({jsonrpc:'2.0',method,...(params===undefined?{}:{params})})+'\n');
  }
  /** Requests are answered independently; aborting one stops its wait, not an accepted calculation. */
  private request(method:string,params:unknown,options:RequestOptions={}):Promise<any> {
    if(this.closed) return Promise.reject(new Error('Optics connection is closed'));
    const signal=options.signal;
    if(signal?.aborted) return Promise.reject(signal.reason);
    const id=++this.sequence;
    return new Promise((resolve,reject)=>{
      const withdraw=(error:unknown,reason:string)=>{
        const request=this.pending.get(id);
        if(!request) return;
        this.pending.delete(id);clearTimeout(request.timer);request.release();
        this.notify('notifications/cancelled',{requestId:id,reason});
        reject(error);
      };
      const timer=setTimeout(()=>withdraw(new Error(`Optics request timed out: ${method}`),'Client timeout'),this.options.timeout??35000);
      const abort=()=>withdraw(signal?.reason,'Aborted by client');
      const release=()=>signal?.removeEventListener('abort',abort);
      let message:string;
      try {message=JSON.stringify({jsonrpc:'2.0',id,method,params})+'\n';}
      catch(error){clearTimeout(timer);reject(error);return;}
      this.pending.set(id,{resolve,reject,timer,release});
      signal?.addEventListener('abort',abort,{once:true});
      this.child.stdin.write(message,error=>{
        if(error){clearTimeout(timer);release();this.pending.delete(id);reject(error);}
      });
    });
  }
  async call<T=unknown>(name:string,arguments_:Record<string,unknown>={},options:RequestOptions={}):Promise<T> {
    const result=await this.request('tools/call',{name,arguments:arguments_},options);
    if(result.isError) throw new Error(result.content?.map((c:{text?:string})=>c.text??'').join('\n')||'Optics operation failed');
    if(result.structuredContent !== undefined) return result.structuredContent as T;
    const text=result.content?.find((c:{type:string;text?:string})=>c.type==='text')?.text;
    if(typeof text!=='string') throw new Error('Optics tool returned no structured or text result: '+name);
    try {return JSON.parse(text) as T;} catch {return text as T;}
  }
  search(query='',options:Record<string,unknown>={}) {return this.call('software_search',{...options,query});}
  /** Read only the program, source or operating guide needed for this task. */
  details(tool:string,options:{program?:string;source?:boolean;brief?:boolean;language?:'en'|'zh'}={}) {return this.call<Record<string,unknown>>('software_details',{tool,...options});}
  async guide(topic:string) {
    const result=await this.call<{text:string}|string>('read_guide',{topic});
    return typeof result==='string'?result:result.text;
  }
  integrations(refresh=false) {return this.call<IntegrationPage>('software_integrations',{refresh});}
  integrationGuide(integration:string,topic:string,language:'en'|'zh'='en') {return this.call<{integration:string;topic:string;language:string;text:string}>('software_integrations',{integration,topic,language});}
  environments(options:Record<string,unknown>={}) {return this.call('environments',options);}
  installedSoftware() {return this.call('environment_installed');}
  uninstallSoftware(selections:string[]) {return this.call('environment_remove',{selections});}
  planSoftware(selections:string[]) {return this.call('environment_plan',{selections});}
  prepareSoftware(selections:string[],options:{python?:string;packages?:string[];request_id?:string}={}) {return this.call<Submission>('environment_prepare',{selections,...options});}
  resources(cpu?:number,pools?:Record<string,number>) {return this.call('compute_resources',{...(cpu===undefined?{}:{cpu}),...(pools===undefined?{}:{pools})});}
  submit(steps:Step[],options:{name?:string;request_id?:string;run_id?:string}={}) {return this.call<Submission>('submit_workflow',{steps,...options});}
  /** Installed software of every route (executables, session SDKs, modules inside installations, Windows automation servers, service credentials), each with its binding arguments. */
  discover(options:{tool?:string;roots?:string[]}={}) {return this.call('discover_native_software',options);}
  /** Connect installed software by a route: session, executable (then script runs its language), automation or python. */
  bindNative(tool:string,options:{route?:'session'|'executable'|'automation'|'python';executable?:string;python?:string;sdk_path?:string;progid?:string;project?:string;language?:string;options?:Record<string,unknown>;threads?:number;concurrency?:number;requirements?:Record<string,number>;env?:Record<string,string>}={}) {return this.call('bind_native_software',{tool,...options});}
  /** One calculation: a packaged program (action), a Python program (source) or a program in the bound software's own language (script); sweep runs every combination of inputs as parallel steps, optimize searches inputs inside one process. */
  calculate(tool:string,options:{action?:string;source?:string;script?:string;inputs?:Record<string,unknown>;sweep?:Record<string,unknown>;pairing?:'product'|'zip';chunk?:number;optimize?:Record<string,unknown>;run_id?:string;name?:string;request_id?:string;timeout?:number;wait?:number}={}) {return this.call<Submission>('submit_calculation',{tool,...options});}
  /** Swept inputs, state and chosen RESULT values (dotted paths) of every point of a sweep, with the best point. */
  sweep(run_id:string,options:{values?:string[];minimize?:string;maximize?:string;save?:boolean;offset?:number;limit?:number}={}) {return this.call<SweepTable>('sweep_results',{run_id,...options});}
  status(run_id:string,options:{wait?:number;offset?:number;limit?:number;include_results?:boolean;signal?:AbortSignal}={}) {
    const {signal,...query}=options;
    return this.call<RunStatus>('run_status',{run_id,...query},{signal});
  }
  /** Find work after reconnecting. Use next_before for stable pages while new runs arrive. */
  runs(options:{search?:string;tool?:string;state?:RunState;before?:string;offset?:number;limit?:number}={}) {
    return this.call<RunPage>('list_runs',options);
  }
  async wait(run_id:string,options:{signal?:AbortSignal;timeout?:number}={}) {
    const end=options.timeout===undefined?Infinity:Date.now()+options.timeout;
    for(;;){
      options.signal?.throwIfAborted();
      const remaining=end-Date.now();
      if(remaining<=0) throw new Error('Calculation is still running: '+run_id);
      const state=await this.status(run_id,{wait:Math.min(25,remaining/1000),signal:options.signal});
      if(['succeeded','failed','cancelled','blocked','interrupted'].includes(state.state)) return state;
    }
  }
  files(run_id:string,offset=0,limit=100,prefix=''){return this.call('result_files',{run_id,offset,limit,...(prefix?{prefix}:{})});}
  /** Part of an NPY array or NPZ member; index entries are positions or [start, stop, step]. */
  array(run_id:string,file:string,options:{member?:string;index?:(number|null|(number|null)[])[];max_elements?:number}={}){return this.call('read_array',{run_id,file,...options});}
  /** A PNG of a one- or two-dimensional selection (lines or a map with a color bar) and the extremes with their positions. */
  async picture(run_id:string,file:string,options:{member?:string;index?:(number|null|(number|null)[])[];max_elements?:number;image?:'auto'|'line'|'map';part?:'abs'|'real'|'imag'|'phase'|'power';scale?:'linear'|'log'}={}):Promise<{png:Buffer;summary:Record<string,unknown>}> {
    const result=await this.request('tools/call',{name:'read_array',arguments:{run_id,file,image:'auto',...options}});
    if(result.isError) throw new Error(result.content?.map((c:{text?:string})=>c.text??'').join('\n')||'Optics operation failed');
    const image=result.content?.find((c:{type:string;mimeType?:string})=>c.type==='image'&&c.mimeType==='image/png');
    if(!image||typeof image.data!=='string') throw new Error('The server did not return a PNG; upgrade the Optics connector to 0.8.0 or newer');
    const structured=result.structuredContent??JSON.parse(result.content?.find((c:{type:string})=>c.type==='text')?.text??'{}');
    return {png:Buffer.from(image.data,'base64'),summary:structured.summary??{}};
  }
  read(run_id:string,file:string,offset=0,limit=16384){return this.call('read_result',{run_id,file,offset,limit});}
  /** Copy a result file to this computer; the server may run elsewhere, for example over SSH. */
  async download(run_id:string,file:string,destination:string,options:RequestOptions={}):Promise<{path:string;bytes:number}> {
    const output=await open(destination,'wx');
    let offset=0,bytes=0,complete=false;
    try {
      for(;;){
        const part=await this.call<{encoding:string;data:string;next_offset:number|null}>('read_result',{run_id,file,offset,limit:65536},options);
        const chunk=Buffer.from(part.data,part.encoding==='base64'?'base64':'utf8');
        await output.writeFile(chunk);bytes+=chunk.length;
        if(part.next_offset===null) break;
        offset=part.next_offset;
      }
      complete=true;
    } finally {
      await output.close();
      if(!complete) await rm(destination,{force:true});
    }
    return {path:destination,bytes};
  }
  cancel(run_id:string){return this.call('cancel_run',{run_id});}
  resume(run_id:string){return this.call('resume_run',{run_id});}
  close(){this.child.stdin.end();this.child.kill();this.fail(new Error('Optics client closed'));}
}
