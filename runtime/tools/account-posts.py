#!/usr/bin/env python3
"""Bounded, resumable collection of the public Douyin author-post feed.
No arbitrary URLs, shell execution, browser cookies or model credentials accepted.
"""
import argparse, datetime as dt, fcntl, hashlib, json, math, os, re, tempfile, time
import urllib.error, urllib.parse, urllib.request
from pathlib import Path
ROOT=Path('/data/benchmarks/_work')
VERSION='account-posts-1.0'
MAX_JSON=16*1024*1024
MAX_INDEX_BYTES=8*1024*1024
MAX_ITEMS=10000

def now(): return dt.datetime.now(dt.timezone.utc).isoformat()
def inside(root,p):
 p=p.resolve(strict=True);root=root.resolve(strict=True)
 if p==root or root not in p.parents: raise ValueError('artifact_path_escape')
 return p
def read_json(root,p):
 p=inside(root,p)
 if not p.is_file() or p.stat().st_size>MAX_JSON:raise ValueError('invalid_or_oversize_artifact')
 return json.loads(p.read_text(encoding='utf-8'))
def atomic(p,text):
 fd,tmp=tempfile.mkstemp(prefix='.posts-',dir=p.parent)
 try:
  with os.fdopen(fd,'w',encoding='utf-8') as f:f.write(text);f.flush();os.fsync(f.fileno())
  os.replace(tmp,p)
 finally:
  if os.path.exists(tmp):os.unlink(tmp)
def dump(p,data):atomic(p,json.dumps(data,ensure_ascii=False,indent=2))
def valid_id(v):return isinstance(v,str) and bool(re.fullmatch(r'[A-Za-z0-9_-]{8,256}',v))
def setup(value,root=ROOT):
 directory=inside(root,Path(value))
 if not directory.is_dir():raise ValueError('invalid_evidence_dir')
 info=read_json(directory,directory/'prepare-result.json')
 profile=info.get('profile',{})
 if info.get('resource_type')!='account_profile' or not isinstance(profile,dict) or not valid_id(profile.get('sec_uid')):raise ValueError('douyin_account_profile_required')
 if profile.get('platform') not in (None,'douyin'):raise ValueError('unsupported_account_platform')
 out=directory/'account-posts-v1';out.mkdir(exist_ok=True);out=inside(directory,out)
 return directory,out,profile

def new_state(profile):
 return {'version':VERSION,'sec_uid':profile['sec_uid'],'profile':profile,'started_at':now(),'updated_at':now(),'pages_fetched':0,'next_cursor':None,'seen_cursors':[],'pagination_exhausted':False,'resumable':True,'stop_reason':'not_started','pending_task':None,'next_allowed_epoch':0,'last_request_epoch':0,'items':{},'item_bytes':0,'duplicates':0,'skipped_items':0,'issues':[]}
def load_state(out,profile):
 if not (out/'state.json').exists():return new_state(profile)
 state=read_json(out,out/'state.json')
 if state.get('version')!=VERSION or state.get('sec_uid')!=profile['sec_uid'] or not isinstance(state.get('items'),dict):raise ValueError('account_state_mismatch')
 return state

def date_value(v):
 if not isinstance(v,str):return None
 try:
  d=dt.datetime.fromisoformat(v.replace('Z','+00:00'))
  if d.tzinfo is None:return None
  return d.astimezone(dt.timezone.utc)
 except (ValueError,OverflowError):return None

def normalize(item,sec_uid,page,index,retrieved):
 if not isinstance(item,dict):raise ValueError('invalid_item')
 content_id=item.get('content_id')
 if not isinstance(content_id,str) or not re.fullmatch(r'\d{5,30}',content_id):raise ValueError('invalid_content_id')
 author=item.get('author') or {}
 if not isinstance(author,dict) or author.get('sec_uid')!=sec_uid:raise ValueError('author_identity_unverified_or_mismatched')
 if item.get('platform')!='douyin':raise ValueError('platform_mismatch')
 if item.get('is_private') is True or item.get('is_deleted') is True:raise ValueError('nonpublic_or_deleted_item')
 d=date_value(item.get('created_at'));stats=item.get('stats') or {}
 if not isinstance(stats,dict):stats={}
 counts={k:(v if type(v) is int and v>=0 else None) for k in ['digg_count','comment_count','share_count','collect_count','play_count'] for v in [stats.get(k)]}
 kind=item.get('kind') if item.get('kind') in ['video','image','image_album','album'] else str(item.get('kind') or 'unknown')[:40]
 return {'content_id':content_id,'platform':'douyin','kind':kind,'web_url':'https://www.douyin.com/video/'+content_id,
  'title':str(item.get('title') or '')[:20000],'description':str(item.get('description') or '')[:20000],
  'created_at':d.isoformat() if d else None,'created_at_raw':item.get('created_at') if isinstance(item.get('created_at'),str) else None,
  'published_at_china':d.astimezone(dt.timezone(dt.timedelta(hours=8))).isoformat() if d else None,
  'duration_ms':item.get('duration_ms') if type(item.get('duration_ms')) is int and item['duration_ms']>=0 else None,
  'stats':counts,'author_sec_uid':sec_uid,'provider_fetched_at':item.get('fetched_at'),'retrieved_at':retrieved,
  'source_page':page,'source_item_index':index,'is_pinned':item.get('is_pinned') if type(item.get('is_pinned')) is bool else None}

def sorted_items(state):return sorted(state['items'].values(),key=lambda x:(x['created_at'] is None,x['created_at'] or '',x['content_id']))
def summary(state,out):
 items=sorted_items(state);dated=[x for x in items if x['created_at']]
 count=(state['profile'].get('stats') or {}).get('content_count')
 missing_dates=len(items)-len(dated)
 limits=['仅覆盖本次接口可访问的公开作品，不代表账号从注册起的完整历史；已删除、私密或接口遗漏内容不可据此还原。','互动数是采集时快照，未知指标保留null；不能当作发布时表现、历史涨粉或投流证据。','按发布日期排序，不把主页置顶顺序当作发布顺序；列表文案不等于已观看视频画面和口播。']
 if type(count) is int and count!=len(items):limits.append('账号资料作品数与已取去重条数不同，可能涉及采集未完成、可见性、缓存或更新，不能据此补造缺失作品。')
 if state['skipped_items']:limits.append('有作品因字段无效、作者身份不符或非公开标记被跳过，详见skipped_items与issues。')
 return {'ok':True,'resource_type':'account_posts','status':'visible_list_exhausted' if state['pagination_exhausted'] else 'partial',
  'profile_fetched':True,'profile':{k:state['profile'].get(k) for k in ['nickname','unique_id','sec_uid']},
  'works_fetched':bool(items) or state['pages_fetched']>0,'unique_posts':len(items),'pages_fetched':state['pages_fetched'],
  'pagination_exhausted':state['pagination_exhausted'],'history_complete':False,'can_resume':not state['pagination_exhausted'] and state['resumable'],
  'stop_reason':state['stop_reason'],'retry_after_seconds':max(0,math.ceil(state.get('next_allowed_epoch',0)-time.time())),'next_cursor':state['next_cursor'],'pending_task':state['pending_task'],
  'duplicates_removed':state['duplicates'],'skipped_items':state['skipped_items'],'missing_dates':missing_dates,
  'profile_reported_count':count,'profile_count_matches_collected':count==len(items) if type(count) is int else None,
  'earliest_visible_at':dated[0]['created_at'] if dated else None,'latest_visible_at':dated[-1]['created_at'] if dated else None,
  'issues':state['issues'][-20:],'updated_at':state['updated_at'],'limitations':limits,
  'files':{'index':str(out/'posts.jsonl'),'summary':str(out/'summary.json'),'raw_pages':str(out/'pages')},
  'instruction':'这是作品列表证据而非完整账号分析。先检查覆盖范围和缺口；仅stop_reason=page_budget时自动继续account_fetch_posts；接口错误或任务未完成时报告已取范围，不循环重试。分页读取索引用account_read_posts。不得凭当前互动量证明历史涨粉原因。'}

def save(state,out):
 state['updated_at']=now();dump(out/'state.json',state)
 atomic(out/'posts.jsonl',''.join(json.dumps(x,ensure_ascii=False)+'\n' for x in sorted_items(state)))
 dump(out/'summary.json',summary(state,out))

class ProviderError(Exception):
 def __init__(self,code,retry_after=0):self.code=code;self.retry_after=retry_after;super().__init__(code)
class Pending(Exception):
 def __init__(self,task_id):self.task_id=task_id
class Provider:
 def __init__(self):
  self.base=os.environ.get('DOUYIN_API_V5_BASE','').rstrip('/')
  if not self.base:raise ProviderError('provider_not_configured')
  try:self.key=Path(os.environ.get('DOUYIN_API_V5_KEY_FILE','/run/secrets/dtk5-api-key')).read_text().strip()
  except OSError:raise ProviderError('provider_key_unavailable')
  if not self.key:raise ProviderError('provider_key_unavailable')
 def request(self,path):
  req=urllib.request.Request(self.base+path,headers={'X-API-Key':self.key,'Accept':'application/json'})
  try:
   with urllib.request.urlopen(req,timeout=40) as r:status=r.status;raw=r.read(MAX_JSON+1)
  except urllib.error.HTTPError as e:
   code='provider_http_'+str(e.code);retry=0
   try:
    body=json.loads(e.read(65536));error=body.get('error') or {};stable=error.get('code')
    if isinstance(stable,str) and re.fullmatch(r'[A-Z0-9_]{1,80}',stable):code=stable
    value=error.get('retry_after')
    if type(value) in (int,float) and math.isfinite(value) and value>0:retry=min(value,86400)
   except (ValueError,AttributeError):pass
   if code=='UPSTREAM_RISK_CONTROL':retry=max(retry,120)
   elif e.code==429:retry=max(retry,60)
   raise ProviderError(code,retry)
  except (urllib.error.URLError,TimeoutError,OSError):raise ProviderError('provider_network_error')
  if len(raw)>MAX_JSON:raise ProviderError('provider_response_too_large')
  try:payload=json.loads(raw)
  except (ValueError,UnicodeError):raise ProviderError('provider_invalid_json')
  if not isinstance(payload,dict) or payload.get('success') is not True:
   raise ProviderError('provider_rejected_request')
  return status,payload
 def page(self,sec_uid,cursor,pending=None):
  if pending:
   if not isinstance(pending,str) or not re.fullmatch(r'[a-fA-F0-9-]{36}',pending):raise ProviderError('invalid_pending_task')
   task=pending
  else:
   query={'sec_user_id':sec_uid,'count':20,'wait':30}
   if cursor is not None:query['cursor']=cursor
   status,payload=self.request('/api/v1/douyin/user/posts?'+urllib.parse.urlencode(query))
   if status!=202:return payload
   data=payload.get('data') or {};meta=payload.get('meta') or {}
   if not isinstance(data,dict) or not isinstance(meta,dict):raise ProviderError('invalid_pending_task')
   task=data.get('task_id') or meta.get('task_id')
   if not isinstance(task,str) or not re.fullmatch(r'[a-fA-F0-9-]{36}',task):raise ProviderError('invalid_pending_task')
  # Persist the task id on timeout instead of blindly submitting another job.
  deadline=time.monotonic()+30
  while time.monotonic()<deadline:
   status,payload=self.request('/api/v1/tasks/'+task)
   data=payload.get('data') or {}
   if not isinstance(data,dict):raise ProviderError('invalid_task_shape')
   if data.get('state')=='done':return {'success':True,'data':data.get('data'),'meta':data.get('result_meta') or {}}
   if data.get('state') in ['failed','cancelled','canceled']:raise ProviderError('provider_task_failed')
   time.sleep(2)
  raise Pending(task)

def page_data(payload):
 if not isinstance(payload,dict) or payload.get('success') is not True:raise ProviderError('provider_rejected_request')
 meta=payload.get('meta') or {}
 if not isinstance(meta,dict) or meta.get('endpoint')!='douyin.author_posts':raise ProviderError('unexpected_provider_endpoint')
 d=payload.get('data')
 if not isinstance(d,dict) or not isinstance(d.get('items'),list) or type(d.get('has_more')) is not bool:raise ProviderError('invalid_page_shape')
 cursor=d.get('cursor')
 if cursor is not None and (not isinstance(cursor,str) or len(cursor)>512):raise ProviderError('invalid_page_cursor')
 if len(d['items'])>200:raise ProviderError('oversized_page')
 return d['items'],d['has_more'],cursor

def collect(directory,max_pages=5,provider=None,root=ROOT):
 if type(max_pages) is not int or not 1<=max_pages<=5:raise ValueError('max_pages_out_of_range')
 directory,out,profile=setup(directory,root)
 lockpath=out/'collect.lock'
 fd=os.open(lockpath,os.O_RDWR|os.O_CREAT|os.O_NOFOLLOW,0o600)
 with os.fdopen(fd,'w') as lock:
  try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
  except BlockingIOError:raise ValueError('account_collection_busy')
  state=load_state(out,profile)
  if state['pagination_exhausted'] or not state['resumable'] or state.get('next_allowed_epoch',0)>time.time():return summary(state,out)
  try:provider=provider or Provider()
  except ProviderError as e:
   state['stop_reason']=e.code;save(state,out);return summary(state,out)
  pages=out/'pages';pages.mkdir(exist_ok=True);pages=inside(out,pages)
  for _ in range(max_pages):
   if len(state['items'])>=MAX_ITEMS:
    state['stop_reason']='item_safety_limit';state['resumable']=False;break
   try:
    delay=max(0,3-(time.time()-state.get('last_request_epoch',0)))
    if delay:time.sleep(delay)
    state['last_request_epoch']=time.time();save(state,out)
    payload=provider.page(profile['sec_uid'],state['next_cursor'],state['pending_task'])
    items,has_more,cursor=page_data(payload)
   except Pending as e:
    state['pending_task']=e.task_id;state['stop_reason']='provider_task_pending';break
   except ProviderError as e:
    state['stop_reason']=e.code;state['next_allowed_epoch']=time.time()+e.retry_after
    if e.code=='provider_task_failed':state['pending_task']=None
    break
   n=state['pages_fetched']+1;retrieved=now()
   dump(pages/('page-%05d.json'%n),{'retrieved_at':retrieved,'requested_cursor':state['next_cursor'],'response':payload})
   index_limit=False
   for idx,item in enumerate(items):
    try:post=normalize(item,profile['sec_uid'],n,idx,retrieved)
    except ValueError as e:
     state['skipped_items']+=1
     if len(state['issues'])<100:state['issues'].append({'page':n,'item_index':idx,'code':str(e)})
     continue
    if post['content_id'] in state['items']:state['duplicates']+=1
    else:
     size=len(json.dumps(post,ensure_ascii=False).encode('utf-8'))
     if state.get('item_bytes',0)+size>MAX_INDEX_BYTES or len(state['items'])>=MAX_ITEMS:
      index_limit=True;state['skipped_items']+=1;continue
     state['items'][post['content_id']]=post;state['item_bytes']=state.get('item_bytes',0)+size
   state['pages_fetched']=n;state['pending_task']=None;state['next_allowed_epoch']=0
   if index_limit:
    state['resumable']=False;state['stop_reason']='index_safety_limit';state['issues'].append({'page':n,'code':'index_safety_limit'})
   elif not has_more:
    state['pagination_exhausted']=True;state['next_cursor']=None;state['resumable']=False;state['stop_reason']='provider_end'
   elif not cursor or cursor==state['next_cursor'] or cursor in state['seen_cursors']:
    state['resumable']=False;state['stop_reason']='pagination_stalled'
    state['issues'].append({'page':n,'code':'missing_or_repeated_cursor'})
   else:
    state['seen_cursors'].append(cursor);state['next_cursor']=cursor;state['stop_reason']='page_budget'
   save(state,out)
   if state['pagination_exhausted'] or not state['resumable']:break
  save(state,out)
  return summary(state,out)

def read_posts(directory,offset=0,limit=20,root=ROOT):
 if type(offset) is not int or offset<0 or type(limit) is not int or not 1<=limit<=30:raise ValueError('invalid_read_range')
 directory,out,profile=setup(directory,root)
 if not (out/'state.json').exists():raise ValueError('account_posts_not_collected')
 state=load_state(out,profile);items=sorted_items(state)
 if offset>len(items):raise ValueError('offset_out_of_range')
 result=summary(state,out);batch=items[offset:offset+limit]
 result.update({'posts':batch,'offset':offset,'returned':len(batch),'next_offset':offset+len(batch) if offset+len(batch)<len(items) else None,'sort_order':'published_at_ascending_unknown_last','collection_revision':state['updated_at']})
 return result

def main():
 p=argparse.ArgumentParser();p.add_argument('mode',choices=['collect','read']);p.add_argument('evidence_dir');p.add_argument('--max-pages',type=int,default=5);p.add_argument('--offset',type=int,default=0);p.add_argument('--limit',type=int,default=20);a=p.parse_args()
 try:
  result=collect(a.evidence_dir,a.max_pages) if a.mode=='collect' else read_posts(a.evidence_dir,a.offset,a.limit)
  result['evidence_dir']=str(Path(a.evidence_dir).resolve());print(json.dumps(result,ensure_ascii=False))
 except Exception as e:
  allowed=isinstance(e,(ValueError,ProviderError))
  print(json.dumps({'ok':False,'error':str(e) if allowed else 'account_posts_internal_error'},ensure_ascii=False));raise SystemExit(1)
if __name__=='__main__':main()
