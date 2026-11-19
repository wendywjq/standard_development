#!/usr/bin/env python3
"""Download image references through the Citadel CLI, with a resumable local state."""
import argparse, collections, concurrent.futures, hashlib, json, os, re, shutil, sqlite3, subprocess, threading, time
from pathlib import Path
from urllib.parse import urlsplit
import xml.etree.ElementTree as ET

BASE=Path(__file__).resolve().parents[1]
DEFAULT_STATE=BASE.parent/'standard-development-state'/'images'


def image_key(url):
    if not url:return 'missing-url'
    path=urlsplit(url).path
    match=re.search(r'/api/file/(?:cdn/)?(?:\d+)/(\d+)',path)
    return 'file:'+match[1] if match else 'url:'+hashlib.sha256(url.split('#')[0].encode()).hexdigest()


def file_type(data):
    if data.startswith(b'\x89PNG\r\n\x1a\n'):return '.png','image/png'
    if data.startswith(b'\xff\xd8\xff'):return '.jpg','image/jpeg'
    if data.startswith((b'GIF87a',b'GIF89a')):return '.gif','image/gif'
    if data[:4]==b'RIFF' and data[8:12]==b'WEBP':return '.webp','image/webp'
    if data.startswith(b'BM'):return '.bmp','image/bmp'
    if data.startswith((b'II*\0',b'MM\0*')):return '.tiff','image/tiff'
    if data[4:8]==b'ftyp' and b'avif' in data[8:40]:return '.avif','image/avif'
    if b'<svg' in data[:1000]:
        ET.fromstring(data)
        return '.svg','image/svg+xml'
    raise ValueError('response_is_not_image')


def reason_for(message):
    if re.search(r'无权|没有权限|权限不足|无.*权限|permission|forbidden|403',message,re.I):return 'no_permission','当前账号无权下载'
    if re.search(r'404|not.?found|不存在|已删除',message,re.I):return 'not_found','图片资源不存在或已删除'
    if re.search(r'安全屋|高密',message):return 'restricted_environment','需安全屋环境，未尝试其他访问方式'
    if re.search(r'429|too many|限流|频繁',message,re.I):return 'rate_limited','服务限流，稍后重试'
    if re.search(r'login|登录|401|认证|token.*expir',message,re.I):return 'authentication_required','当前认证无法下载，未申请额外权限'
    return 'download_failed','下载失败，未取得有效图片文件'


def main():
    p=argparse.ArgumentParser();p.add_argument('--state',type=Path,default=DEFAULT_STATE);p.add_argument('--workers',type=int,default=2);p.add_argument('--interval',type=float,default=.3);p.add_argument('--limit',type=int);p.add_argument('--keys-file',type=Path);p.add_argument('--cli',default=shutil.which('oa-skills') or str(Path.home()/'.npm-global/bin/oa-skills'));a=p.parse_args()
    a.state.mkdir(parents=True,exist_ok=True);temp=a.state/'tmp';temp.mkdir(exist_ok=True);dest=BASE/'assets'/'images';dest.mkdir(parents=True,exist_ok=True)
    refs=json.loads((BASE/'media.json').read_text())['references'];groups={}
    for r in refs:
        if r['kind'] not in ['图片','流程图']:continue
        key=image_key(r['url']);g=groups.setdefault(key,{'key':key,'kind':r['kind'],'url':r['url'],'urls':[],'document_ids':[]})
        if r['url'] not in g['urls']:g['urls'].append(r['url'])
        if r['document_id'] not in g['document_ids']:g['document_ids'].append(r['document_id'])
    db=sqlite3.connect(a.state/'progress.sqlite3');db.execute('PRAGMA journal_mode=WAL');db.execute('CREATE TABLE IF NOT EXISTS results (key TEXT PRIMARY KEY, result TEXT NOT NULL)');db.commit()
    done={key:json.loads(value) for key,value in db.execute('SELECT key,result FROM results')}
    selection_file=a.keys_file or (BASE/'IMAGE_SELECTION.json' if (BASE/'IMAGE_SELECTION.json').exists() else None)
    allowed=set(json.loads(selection_file.read_text())) if selection_file else None
    pending=[g for k,g in groups.items() if (allowed is None or k in allowed) and (k not in done or (done[k].get('status')=='saved' and not (BASE/done[k]['path']).is_file()))]
    if a.limit:pending=pending[:a.limit]
    lock=threading.Lock();next_start=[0.0];start=time.monotonic();last_report=[0.0];finished=0
    def throttle():
        with lock:
            now=time.monotonic();delay=max(0,next_start[0]-now);next_start[0]=max(now,next_start[0])+a.interval
        if delay:time.sleep(delay)
    def fetch(g):
        r=dict(g);tmp=temp/(hashlib.sha256(g['key'].encode()).hexdigest()+'.part');log=''
        if not g['url']:
            return dict(r,status='missing_url',reason='原文图片缺少地址')
        try:
            for attempt in range(3):
                throttle();command='fetchDrawio' if g['kind']=='流程图' else 'fetchImage';urlarg='--drawioUrl' if command=='fetchDrawio' else '--imageUrl'
                proc=subprocess.run([a.cli,'citadel',command,urlarg,g['url'],'--save',str(tmp),'--raw'],capture_output=True,text=True,timeout=90)
                log=proc.stderr+'\n'+proc.stdout
                if tmp.exists():
                    data=tmp.read_bytes()
                    try:extension,mime=file_type(data)
                    except Exception:
                        response=data[:3000].decode('utf8',errors='replace');status,reason=reason_for(response)
                        if status=='download_failed' and not response.strip():reason='服务返回空文件'
                        return dict(r,status=status,reason=reason)
                    if len(data)>95*1024*1024:return dict(r,status='too_large',reason='单张图片超过 GitHub 普通文件大小限制')
                    sha=hashlib.sha256(data).hexdigest();asset=dest/(sha+extension)
                    if not asset.exists():tmp.replace(asset)
                    return dict(r,status='saved',path=str(asset.relative_to(BASE)),sha256=sha,bytes=len(data),mime_type=mime)
                status,reason=reason_for(log)
                if status not in ['rate_limited','download_failed'] or attempt==2:return dict(r,status=status,reason=reason)
                time.sleep(min(10*(attempt+1),30))
        except subprocess.TimeoutExpired:return dict(r,status='timeout',reason='下载超时，未取得图片')
        except Exception as e:return dict(r,status='download_failed',reason='图片下载或文件校验失败：'+type(e).__name__)
        finally:
            if tmp.exists():tmp.unlink()
    def persist():
        if allowed is None:
            snapshot={'version':1,'total_unique_references':len(groups),'processed':len(done),'complete':len(done)==len(groups),'requests_for_permissions':False,'images':list(done.values())}
        else:
            selected_hashes={done[k]['sha256'] for k in allowed if k in done and done[k].get('status')=='saved'}
            exported=[]
            for key,group in groups.items():
                result=done.get(key,{})
                if not group['url']:exported.append(dict(group,status='missing_url',reason='原文图片缺少地址'))
                elif result.get('status')=='saved' and result['sha256'] in selected_hashes:exported.append(result)
                elif key in allowed and result:exported.append(result)
                elif key in allowed:exported.append(dict(group,status='pending',reason='选中的图片尚未同步'))
                else:exported.append(dict(group,status='not_selected',reason='未纳入本次 500 张图片同步范围'))
            snapshot={'version':2,'scope':'500_unique_images','selection':'committed_image_selection','limit':len(allowed),'selected_unique_files':len(selected_hashes),'total_unique_references':len(groups),'complete':all(k in done for k in allowed),'requests_for_permissions':False,'images':exported}
        target=BASE/'image-sync.json';tmpfile=a.state/'snapshot.json';tmpfile.write_text(json.dumps(snapshot,ensure_ascii=False,indent=2)+'\n');os.replace(tmpfile,target)
        tracked={key:result for key,result in done.items() if allowed is None or key in allowed}
        counts=collections.Counter(r['status'] for r in tracked.values());unique={r['path']:r['bytes'] for r in tracked.values() if r['status']=='saved'}
        progress={'total':len(groups) if allowed is None else len(allowed),'processed':len(tracked),'counts':dict(counts),'unique_files':len(unique),'bytes':sum(unique.values()),'elapsed_seconds':round(time.monotonic()-start)}
        (a.state/'status.json').write_text(json.dumps(progress,ensure_ascii=False,indent=2))
        print(json.dumps(progress,ensure_ascii=False),flush=True)
    persist()
    with concurrent.futures.ThreadPoolExecutor(max_workers=a.workers) as pool:
        tasks={pool.submit(fetch,g):g for g in pending}
        for f in concurrent.futures.as_completed(tasks):
            r=f.result();done[r['key']]=r;db.execute('INSERT OR REPLACE INTO results(key,result) VALUES (?,?)',(r['key'],json.dumps(r,ensure_ascii=False)));db.commit();finished+=1
            if finished%25==0 or time.monotonic()-last_report[0]>=30:
                persist();last_report[0]=time.monotonic()
    persist();db.close()

if __name__=='__main__':main()
