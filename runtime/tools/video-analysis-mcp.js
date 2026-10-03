'use strict';
const {McpServer}=require('@modelcontextprotocol/sdk/server/mcp.js');
const {StdioServerTransport}=require('@modelcontextprotocol/sdk/server/stdio.js');
const {z}=require('zod/v4');
const {execFile}=require('node:child_process');
const {promisify}=require('node:util');
const fs=require('node:fs/promises');
const path=require('node:path');
const crypto=require('node:crypto');
const execFileAsync=promisify(execFile);
const SKILL_DIR='/data/admin/skills/yyl-benchmark-breakdown';
const ROOT='/data/benchmarks/_work';
const PYTHON='/data/agent-venv/bin/python';
const BUILDER='/data/tools/video-timeline.py';
const ACCOUNT_COLLECTOR='/data/tools/account-posts.py';
const ALLOWED_HOSTS=[/(^|\.)douyin\.com$/i,/(^|\.)iesdouyin\.com$/i,/(^|\.)tiktok\.com$/i,/(^|\.)bilibili\.com$/i,/(^|\.)b23\.tv$/i,/(^|\.)xiaohongshu\.com$/i,/(^|\.)xhslink\.com$/i,/(^|\.)youtube\.com$/i,/(^|\.)youtu\.be$/i];
function response(data,isError=false,images=[]){return {content:[{type:'text',text:JSON.stringify(data)},...images],isError};}
function validateUrl(raw){let u;try{u=new URL(raw);}catch{throw Error('invalid_url');}if(!['https:','http:'].includes(u.protocol)||u.username||u.password)throw Error('unsupported_scheme_or_credentials');if(!ALLOWED_HOSTS.some(re=>re.test(u.hostname)))throw Error('unsupported_video_host');return u.toString();}
async function run(script,args,timeout){try{const {stdout,stderr}=await execFileAsync('bash',[path.join(SKILL_DIR,'scripts',script),...args],{cwd:SKILL_DIR,encoding:'utf8',timeout,maxBuffer:2*1024*1024,env:process.env});return {ok:true,stdout:stdout.slice(-100000),stderr:stderr.slice(-12000)};}catch(e){return {ok:false,code:e.code??null,timed_out:Boolean(e.killed),stdout:String(e.stdout??'').slice(-30000),stderr:String(e.stderr??'media_process_failed').slice(-12000)};}}
function inside(root,file){const rel=path.relative(root,file);return rel!==''&&!rel.startsWith('..'+path.sep)&&rel!=='..'&&!path.isAbsolute(rel);}
async function evidenceDir(value){const root=await fs.realpath(ROOT);const dir=await fs.realpath(value);if(!inside(root,dir)||(await fs.stat(dir)).isDirectory()===false)throw Error('evidence_dir_outside_allowed_root');return dir;}
async function safeFile(root,file,maxBytes){const real=await fs.realpath(file);if(!inside(root,real))throw Error('artifact_path_escape');const st=await fs.stat(real);if(!st.isFile()||st.size>maxBytes)throw Error('invalid_or_oversize_artifact');return fs.readFile(real);}
async function loadTimeline(value){const dir=await evidenceDir(value);const out=await fs.realpath(path.join(dir,'av-timeline-v1'));if(!inside(dir,out))throw Error('artifact_path_escape');const d=JSON.parse((await safeFile(out,path.join(out,'timeline.json'),2*1024*1024)).toString('utf8'));if(d.version!=='av-timeline-1.0'||d.evidence_dir!==dir||!Array.isArray(d.frames)||!Array.isArray(d.sheets)||!Array.isArray(d.segments))throw Error('invalid_timeline');return {dir,out,d};}
async function imageBlock(out,file){const data=await safeFile(out,file,4*1024*1024);if(data[0]!==0xff||data[1]!==0xd8)throw Error('invalid_jpeg');return {type:'image',mimeType:'image/jpeg',data:data.toString('base64')};}
function summary(d){return {version:d.version,status:d.status,evidence_dir:d.evidence_dir,timeline_file:d.timeline_file,source_url:d.source_url,duration_sec:d.duration_sec,asr_status:d.asr_status,sampling:d.sampling,warnings:d.warnings,limitations:d.limitations};}
async function readPages(value,page=1,count=2){
 if(!Number.isInteger(page)||page<1||!Number.isInteger(count)||count<1||count>2)throw Error('invalid_page_range');
 const {out,d}=await loadTimeline(value);if(page>d.sheets.length)throw Error('page_out_of_range');
 const pages=d.sheets.slice(page-1,page-1+count);const ids=new Set(pages.flatMap(p=>p.frame_ids));
 const frames=d.frames.filter(f=>ids.has(f.id));const start=frames[0].time_sec,end=frames.at(-1).time_sec;
 const segments=d.segments.filter(s=>s.end_sec>=start&&s.start_sec<=end);
 const images=await Promise.all(pages.map(p=>imageBlock(out,p.file)));
 return response({...summary(d),pages_returned:pages.map(p=>p.page),total_pages:d.sheets.length,next_page:page+pages.length<=d.sheets.length?page+pages.length:null,frames,segments,instruction:'图片已随工具结果返回。按图片标签F编号和真实PTS核对画面；ASR为机器分段。先记录本页视觉观察再翻页，勿把采样帧称为逐帧观看。'},false,images);
}
async function readFrames(value,ids){if(!Array.isArray(ids)||ids.length<1||ids.length>2||new Set(ids).size!==ids.length||ids.some(x=>!/^F\d{3}$/.test(x)))throw Error('invalid_frame_ids');const {out,d}=await loadTimeline(value);const frames=ids.map(id=>{const f=d.frames.find(f=>f.id===id);if(!f)throw Error('frame_not_found');return f;});return response({evidence_dir:d.evidence_dir,frames,segments:d.segments.filter(s=>frames.some(f=>s.start_sec<=f.time_sec&&s.end_sec>f.time_sec)),instruction:'下列图片与frames顺序一一对应，用于复核字幕/动作。'},false,await Promise.all(frames.map(f=>imageBlock(out,f.file))));}
async function build(value){const dir=await evidenceDir(value);try{const r=await execFileAsync(PYTHON,[BUILDER,dir],{encoding:'utf8',timeout:480000,maxBuffer:2*1024*1024});const data=JSON.parse(r.stdout.trim());if(data.ok===false)throw Error(data.error||'timeline_build_failed');return await readPages(dir,1,2);}catch(e){let code='timeline_build_failed';try{code=JSON.parse(String(e.stdout||'').trim()).error||code;}catch{}if(e.message==='evidence_dir_outside_allowed_root')code=e.message;throw Error(code);}}
async function preparationResult(dir,prep,content,durationMs){
 const base={stage:'prepare_assets',duration_ms:durationMs,...prep,evidence_dir:dir,manifests:content?[{path:path.join(dir,'manifest.txt'),content}]:[],next_tool:prep.ok?'video_build_timeline':null};
 // Never turn a failed process into a success based on an artifact.
 if(!prep.ok)return response(base,true);
 let result;
 try{result=JSON.parse((await safeFile(dir,path.join(dir,'prepare-result.json'),1024*1024)).toString('utf8'));}
 catch(e){if(e.code==='ENOENT')return response(base,false);throw e;}
 if(result.resource_type!=='account_profile'||result.status!=='profile_only'||result.profile_fetched!==true||result.works_fetched!==false||!result.profile||typeof result.profile!=='object'||Array.isArray(result.profile)||!['uid','sec_uid','unique_id'].some(k=>typeof result.profile[k]==='string'&&result.profile[k].trim()))throw Error('invalid_profile_result');
 return response({...base,resource_type:'account_profile',status:'profile_only',profile:result.profile,profile_fetched:true,works_fetched:false,works_status:'not_requested',retrieved_at:result.retrieved_at,provider_endpoint:result.provider_endpoint,limitations:result.limitations,next_tool:'account_fetch_posts',next_tool_arguments:{evidence_dir:dir},instruction:'已成功获取账号资料。用户要求账号历史作品或成长复盘时，接着调用account_fetch_posts获取并翻页读取作品列表；只要求账号简介时不必采集。不要调用视频时间轴处理主页，也不得说账号资料读取失败。统计快照不是历史涨粉证据。'},false);
}
async function accountPosts(value,mode,maxPages=5,offset=0,limit=20){
 const dir=await evidenceDir(value);
 if(!['collect','read'].includes(mode)||!Number.isInteger(maxPages)||maxPages<1||maxPages>5||!Number.isInteger(offset)||offset<0||!Number.isInteger(limit)||limit<1||limit>30)throw Error('invalid_account_posts_arguments');
 const args=[ACCOUNT_COLLECTOR,mode,dir,...(mode==='collect'?['--max-pages',String(maxPages)]:['--offset',String(offset),'--limit',String(limit)])];
 let raw;
 try{const r=await execFileAsync(PYTHON,args,{encoding:'utf8',timeout:mode==='collect'?660000:30000,maxBuffer:2*1024*1024});raw=r.stdout;}
 catch(e){try{const data=JSON.parse(String(e.stdout||'').trim());if(data.ok===false)return response(data,true);}catch{}throw Error(e.killed?'account_posts_timeout_retry_same_evidence_dir':'account_posts_failed');}
 const data=JSON.parse(raw.trim());
 if(data.ok===false)return response(data,true);
 if(data.resource_type!=='account_posts'||data.evidence_dir!==dir)throw Error('invalid_account_posts_result');
 const autoContinue=data.can_resume&&data.stop_reason==='page_budget';
 const next=autoContinue?'account_fetch_posts':mode==='collect'&&data.works_fetched?'account_read_posts':mode==='read'&&data.next_offset!==null?'account_read_posts':null;
 return response({...data,next_tool:next,next_tool_arguments:next==='account_fetch_posts'?{evidence_dir:dir}:next==='account_read_posts'?{evidence_dir:dir,offset:mode==='collect'?0:data.next_offset,limit}:null},false);
}
const server=new McpServer({name:'video-analysis',version:'1.2.0'});
server.tool('video_check_dependencies','Check existing video evidence dependencies; does not grant shell access.',{},async()=>{const r=await run('check-deps.sh',[],120000);return response({stage:'dependency_check',...r},!r.ok);});
server.tool('video_prepare_assets','Resolve a supported link. Account profiles return profile_only with profile data and next_tool=account_fetch_posts for account-history tasks. Videos prepare metadata, ASR and images; only then call video_build_timeline. Do not infer images from paths.',{url:z.string()},async({url})=>{try{const safe=validateUrl(url);const deps=await run('check-deps.sh',[],120000);if(!deps.ok)return response({stage:'dependency_check',...deps},true);await fs.mkdir(ROOT,{recursive:true});const dir=path.join(ROOT,new Date().toISOString().replace(/[-:.TZ]/g,'')+'-'+crypto.randomBytes(4).toString('hex'));const started=Date.now();const prep=await run('prepare-assets.sh',[safe,dir],15*60*1000);let content='';try{content=(await fs.readFile(path.join(dir,'manifest.txt'),'utf8')).slice(0,30000);}catch{}return await preparationResult(dir,prep,content,Date.now()-started);}catch(e){return response({ok:false,error:e.message},true);}});
server.tool('account_fetch_posts','Fetch public Douyin account posts from a successful account_profile evidence_dir. Up to five provider pages per call; persists deduplicated posts, raw pages and cursor for safe resume. Call again with the SAME evidence_dir if can_resume=true. Does not claim complete lifetime history or download videos.',{evidence_dir:z.string(),max_pages:z.number().int().min(1).max(5).optional()},async({evidence_dir,max_pages})=>{try{return await accountPosts(evidence_dir,'collect',max_pages??5);}catch(e){return response({ok:false,error:e.message},true);}});
server.tool('account_read_posts','Read saved account posts oldest first, with dates, stable links and current interaction counts. Continue next_offset until null after collecting; collection changes can reorder offsets. Missing metrics stay null. This is metadata, not visual/audio analysis.',{evidence_dir:z.string(),offset:z.number().int().min(0).optional(),limit:z.number().int().min(1).max(30).optional()},async({evidence_dir,offset,limit})=>{try{return await accountPosts(evidence_dir,'read',5,offset??0,limit??20);}catch(e){return response({ok:false,error:e.message},true);}});
server.tool('video_build_timeline','Build timestamped ASR + true-PTS sampled frames for an existing downloaded video under /data/benchmarks/_work. Returns real image blocks for first two pages; continue video_read_timeline until next_page is null. Does not generate visual conclusions.',{evidence_dir:z.string()},async({evidence_dir})=>{try{return await build(evidence_dir);}catch(e){return response({ok:false,error:e.message},true);}});
server.tool('video_read_timeline','Read one or two timeline contact-sheet pages as real images with aligned ASR. Preserve each page observation before advancing; image history may age out. Page labels use source seconds, not invented shot timing.',{evidence_dir:z.string(),page:z.number().int().min(1),count:z.number().int().min(1).max(2).optional()},async({evidence_dir,page,count})=>{try{return await readPages(evidence_dir,page,count??2);}catch(e){return response({ok:false,error:e.message},true);}});
server.tool('video_read_frames','Read up to two larger sampled frames by F001 identifiers, with actual timestamps and overlapping ASR; use to verify small subtitles or ambiguous scenes.',{evidence_dir:z.string(),frame_ids:z.array(z.string().regex(/^F\d{3}$/)).min(1).max(2)},async({evidence_dir,frame_ids})=>{try{return await readFrames(evidence_dir,frame_ids);}catch(e){return response({ok:false,error:e.message},true);}});
module.exports={validateUrl,inside,evidenceDir,loadTimeline,readPages,readFrames,build,preparationResult,accountPosts,server};
if(require.main===module)server.connect(new StdioServerTransport()).catch(()=>{process.stderr.write('video-analysis MCP startup failed\n');process.exit(1);});
