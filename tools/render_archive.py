#!/usr/bin/env python3
"""Render the saved Citadel XML / legacy HTML without contacting the source service."""
from pathlib import Path
from html import escape
from html.parser import HTMLParser
from urllib.parse import urlsplit, unquote
import collections, copy, hashlib, json, re, xml.etree.ElementTree as ET

BASE = Path(__file__).resolve().parents[1]
DOCS = BASE / 'docs'
DATE = '2026-11-19'
SOURCE = 'https://km.sankuai.com/collabpage/1637005052#b-921650b1a29b4bd2bd2f815014f77623'
records = json.loads((BASE / 'manifest.json').read_text())['documents']
by_id = {n['id']: n for n in records}
assets = []
render_notes = []
VOID = {'area', 'base', 'br', 'col', 'embed', 'hr', 'img', 'input', 'link', 'meta', 'param', 'source', 'track', 'wbr'}
SAFE = {'article','section','div','p','span','a','h1','h2','h3','h4','h5','h6','ul','ol','li','table','thead','tbody','tfoot','tr','th','td','caption','colgroup','col','strong','b','em','i','u','del','s','sub','sup','blockquote','hr','br','pre','code','img','details','summary','figure','figcaption','video','audio','source'}
STATUS = {'saved':'已保存正文', 'no_permission':'无权限', 'restricted_environment':'需安全屋环境', 'read_failed':'读取失败'}

class Fragment(HTMLParser):
    def __init__(self, text):
        super().__init__(convert_charrefs=True)
        self.root = ET.Element('div'); self.stack = [self.root]; self.feed(text)
    def handle_starttag(self, tag, attrs):
        el = ET.SubElement(self.stack[-1], tag, {k:v or '' for k,v in attrs})
        if tag not in VOID: self.stack.append(el)
    def handle_endtag(self, tag):
        for i in range(len(self.stack)-1, 0, -1):
            if self.stack[i].tag == tag: self.stack = self.stack[:i]; break
    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in VOID: self.handle_endtag(tag)
    def handle_data(self, text):
        el = self.stack[-1]
        if len(el): el[-1].tail = (el[-1].tail or '') + text
        else: el.text = (el.text or '') + text


def safe_url(url):
    return urlsplit(url.strip()).scheme.lower() in {'', 'http', 'https', 'mailto'}


def render(n, target='html'):
    folder = DOCS / n['id']
    if n['status'] != 'saved':
        return '<p class="unavailable">'+escape(STATUS[n['status']])+'：'+escape(n['reason'])+'。未申请权限。</p>'
    if n.get('format') == '1.0':
        root = Fragment((folder/'source.html').read_text()).root
    else:
        raw = (folder/'source.xml').read_text()
        clean = re.sub('[\x00-\x08\x0b\x0c\x0e-\x1f]', '', raw)
        root = ET.fromstring(clean)
    parents = {c:p for p in root.iter() for c in p}
    for el in root.iter():
        tag = el.tag; attrs = dict(el.attrib)
        node_id = attrs.get('nodeId') or attrs.get('data-diff-id')
        if node_id: el.set('id','b-'+node_id)
        if tag in {'script','iframe','object','embed','form','input','button','style','link','meta'}:
            url = attrs.get('src') or attrs.get('href')
            el.tag = 'p'; el.attrib.clear()
            if url and safe_url(url):
                el.text = '嵌入内容：'; ET.SubElement(el,'a',{'href':url}).text=url
            else: el.text = '[原文嵌入组件，详见 source 文件]'
        elif tag == 'km-doc': el.tag = 'div'
        elif tag == 'km-title':
            parent = parents.get(el)
            el.tag = 'summary' if parent is not None and parent.tag in {'km-collapse','details'} else ('h1' if parent is root and list(root).index(el)==0 else 'strong')
        elif tag == 'km-collapse':
            el.tag = 'details'
            if attrs.get('active') == 'true': el.set('open','open')
        elif tag == 'km-content': el.tag = 'div'
        elif tag == 'km-note': el.tag = 'aside'; el.set('class','note')
        elif tag == 'km-quote': el.tag = 'blockquote'
        elif tag == 'km-catalog': el.tag = 'div'; el.text = '文档目录：使用浏览器查找，或展开文中折叠内容。'
        elif tag in {'km-page-tree','km-doc-list-view'}:
            el.tag='p';el.text='下级目录组件保留于源文件；本次范围为汇总表直接链接文档。'
        elif tag in {'km-drawio','km-minder','km-attachment','km-video','km-audio'}:
            kind = {'km-drawio':'流程图','km-minder':'思维导图','km-attachment':'附件','km-video':'视频','km-audio':'音频'}[tag]
            url=attrs.get('src') or attrs.get('url') or '';name=attrs.get('name') or attrs.get('title') or kind
            el.tag='figure';el.text=None
            ET.SubElement(el,'figcaption').text=f'{kind}：{name}（原始链接，可能需要学城权限）'
            if url and safe_url(url):
                ET.SubElement(el,'a',{'href':url}).text='在原文服务中查看'
            if target=='html':assets.append({'document_id':n['id'],'kind':kind,'name':name,'url':url,'storage':'source_url_only'})
        elif tag == 'km-mention':el.tag='span';el.text='@'+attrs.get('name','')
        elif tag == 'km-emoji':el.tag='span';el.text=attrs.get('emoji') or attrs.get('name') or el.text or '表情'
        elif tag == 'km-status':el.tag='span';el.text=el.text or attrs.get('text','')
        elif tag == 'km-footnote':
            el.tag='sup';el.text=attrs.get('annotate') or attrs.get('text') or el.text or '注'
        elif tag == 'km-footnote-list':el.tag='section'
        elif tag == 'km-footnote-item':el.tag='p'
        elif tag == 'km-html':
            el.tag='pre';el.text='原文 HTML 组件：\n'+(el.text or attrs.get('html',''))
        elif tag.startswith('km-'):
            el.tag='div'
            if target=='html':render_notes.append({'document_id':n['id'],'component':tag,'status':'source_preserved'})
        elif tag not in SAFE and tag != 'aside':el.tag='div'
        if el.tag == 'a':
            url=el.get('href','')
            if url.lower().startswith('www.'):
                url='https://'+url
                el.set('href',url)
            if not safe_url(url):el.attrib.pop('href',None)
            else:
                match=re.search(r'^https?://km\.sankuai\.com/(?:collabpage|page)/(\d+)',url)
                if match and match[1] in by_id:
                    cid=match[1];frag=urlsplit(url).fragment
                    dest='index.html' if target=='html' else 'README.md'
                    el.set('href',f'../{cid}/{dest}'+('#'+frag if frag else ''))
                    el.set('title','学城原文：'+url+'；'+STATUS[by_id[cid]['status']])
        if el.tag == 'img':
            url=el.get('src','')
            if not safe_url(url):el.attrib.pop('src',None)
            el.set('alt',el.get('alt') or attrs.get('name') or '原文图片（需学城权限）')
            if target=='html':
                el.set('loading','lazy')
                assets.append({'document_id':n['id'],'kind':'图片','name':el.get('alt'),'url':url,'storage':'source_url_only'})
        styles=[]
        for key,prop in [('color','color'),('bgColor','background-color'),('font-size','font-size'),('verticalAlign','vertical-align'),('align','text-align')]:
            val=attrs.get(key)
            if val and not re.search(r'url|expression|[<>]',val,re.I):styles.append(prop+':'+(val+'px' if key=='font-size' and val.isdigit() else val))
        existing=attrs.get('style','')
        for declaration in existing.split(';'):
            if ':' not in declaration:continue
            key,val=declaration.split(':',1);key=key.strip().lower()
            if key in {'color','background-color','font-size','font-weight','text-align','vertical-align','text-decoration','white-space'} and not re.search(r'url|expression|[<>]',val,re.I):styles.append(key+':'+val)
        if styles:el.set('style',';'.join(styles))
        for key in list(el.attrib):
            if key not in {'href','title','src','alt','rowspan','colspan','width','height','id','class','style','open','loading','start'}:del el.attrib[key]
    body=ET.tostring(root,encoding='unicode',method='html')
    if n.get('format')=='1.0':body='<h1>'+escape(n['title'])+'</h1>'+body
    return body

CSS='''*{box-sizing:border-box}body{margin:0;color:#202c40;background:#f5f6f8;font:15px/1.75 system-ui,-apple-system,"PingFang SC",sans-serif}a{color:#175ca8;text-decoration:none;overflow-wrap:anywhere}a:hover{text-decoration:underline}header{background:#fff;border-bottom:1px solid #dde2e8;padding:20px 28px}header h1{margin:0;font-size:25px}header p{margin:5px 0;color:#5c6878}main{padding:24px 28px}article{padding:24px;background:#fff;border:1px solid #e0e5ea;border-radius:8px;overflow-x:auto}h1{font-size:28px}h2{font-size:23px}h3{font-size:20px}p{overflow-wrap:anywhere}table{border-collapse:collapse;min-width:100%;font-size:14px}td,th{border:1px solid #ccd4dd;padding:9px;vertical-align:top;min-width:105px}td p,th p{margin:4px 0}td img{max-width:300px}img{max-width:100%;height:auto}details{margin:8px 0;border:1px solid #e0e5eb;border-radius:5px;padding:8px}summary{cursor:pointer;font-weight:600}pre{overflow:auto;background:#f5f7fa;padding:14px}blockquote,.note{margin:14px 0;padding:12px 18px;background:#eff5fc;border-left:4px solid #7da7d4}figure{padding:12px;border:1px dashed #c8d2df;margin:10px 0}.notice{background:#fff6dc;border-left:4px solid #c39324;padding:12px 16px;margin:0 0 20px}.unavailable{background:#fff2ef;padding:20px}.muted{font-size:13px;color:#657487}.toolbar{display:flex;flex-wrap:wrap;gap:14px;margin:10px 0}.index{max-width:1300px;margin:auto}.index td:first-child{min-width:45px}input[type=search]{width:100%;font:inherit;padding:12px;border:1px solid #bcc9d8;border-radius:6px;margin-bottom:20px}.badge{display:inline-block;white-space:nowrap;padding:2px 8px;background:#edf0f4;border-radius:4px}.badge.saved{background:#e7f5ec;color:#21633b}:target{outline:2px solid #df9b39;scroll-margin-top:15px}@media(max-width:700px){header{padding:16px}main{padding:12px}article{padding:14px}td,th{padding:7px}}
'''
(BASE/'archive.css').write_text(CSS)


def page(title,body,root=False):
    prefix='' if root else '../../'
    return '<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta name="referrer" content="no-referrer"><title>'+escape(title)+'</title><link rel="stylesheet" href="'+prefix+'archive.css"></head><body>'+body+'</body></html>'


rows=[];mdrows=[]
for i,n in enumerate(records,1):
    folder=DOCS/n['id'];folder.mkdir(parents=True,exist_ok=True)
    body=render(n)
    toolbar='<div class="toolbar"><a href="../../index.html">全部文档</a><a href="'+escape(n['url'],quote=True)+'">学城原文</a><a href="README.md">GitHub 阅读版</a></div>'
    header='<header><h1>'+escape(n['title'])+'</h1><p>'+STATUS[n['status']]+' · 文档 ID '+n['id']+' · 保存日期 '+DATE+'</p>'+toolbar+'</header>'
    note='<p class="notice">正文与表格已保存；图片、附件、音视频、思维导图及流程图保留原始链接，可能需要学城登录和对应权限。未展开保存正文中的二级引用链接。</p>' if n['status']=='saved' else ''
    (folder/'index.html').write_text(page(n['title'],header+'<main>'+note+'<article>'+body+'</article></main>'))
    md='# '+n['title']+'\n\n[返回目录](../../README.md) · [学城原文]('+n['url']+') · '+STATUS[n['status']]+' · '+DATE+'\n\n'
    if n['status']=='saved':md+='> 正文保留表格和折叠结构。图片和媒体仅保留原始链接，需要学城访问权限。完整源数据见同目录 source 文件。\n\n'
    md+=render(n,'md')+'\n'
    (folder/'README.md').write_text(md)
    label=escape(n['title']);status=STATUS[n['status']]
    rows.append('<tr><td>'+str(i)+'</td><td><a href="docs/'+n['id']+'/index.html">'+label+'</a></td><td><span class="badge '+n['status']+'">'+status+'</span></td><td>'+n['id']+'</td></tr>')
    mdrows.append('| '+str(i)+' | ['+n['title'].replace('|','\\|')+'](docs/'+n['id']+'/README.md) | '+status+' | '+n['id']+' |')
counts=collections.Counter(n['status'] for n in records)
intro=f"共 {len(records)} 篇：正文已保存 {counts['saved']} 篇；无权限 {counts['no_permission']} 篇；需安全屋环境 {counts['restricted_environment']} 篇。"
body='<header><div class="index"><h1>标准文档归档</h1><p>'+intro+'</p><p class="muted">来源：05-标准汇总表 V3.0 · '+DATE+' · <a href="'+SOURCE+'">查看来源</a></p></div></header><main class="index"><p class="notice">归档范围：汇总表本身与其中 137 篇直接链接。无权限内容仅保存占位记录。媒体保留原链接，未申请任何权限。</p><input type="search" id="search" placeholder="按标题、文档 ID 或状态查找" aria-label="搜索文档"><article><table><thead><tr><th>序号</th><th>文档</th><th>状态</th><th>ID</th></tr></thead><tbody>'+''.join(rows)+'</tbody></table></article></main><script>document.getElementById("search").addEventListener("input",function(){const q=this.value.trim().toLowerCase();document.querySelectorAll("tbody tr").forEach(row=>row.hidden=!row.textContent.toLowerCase().includes(q));});</script>'
(BASE/'index.html').write_text(page('标准文档归档',body,True))
(BASE/'README.md').write_text('# 标准文档归档\n\n'+intro+'\n\n来源：[05-标准汇总表 V3.0]('+SOURCE+')。保存日期：'+DATE+'。\n\n本次保存汇总表及其 137 篇直接链接，不递归抓取每篇正文中的参考链接或下级目录。各文档保留原文标题、表格合并结构、折叠内容与来源链接。无权限和安全屋限制均已记录，未申请权限。\n\n- 在 GitHub 中阅读：点击下表文档标题。\n- 本地浏览：用浏览器打开 [index.html](index.html)，支持按标题、ID、状态搜索。也可在仓库目录运行 `python3 -m http.server 8000` 后访问 `http://localhost:8000`。\n- 完整来源：`docs/<文档ID>/source.json`；2.0 文档另有 `source.xml`，1.0 文档另有 `source.html`。\n- [权限与读取限制清单](ACCESS_REPORT.md) · [机器可读清单](manifest.json) · [图片与媒体引用](media.json)\n\n> 图片、音视频、附件、思维导图与流程图保留原始地址，未下载二进制文件；查看时仍可能需要学城登录及对应权限。此副本不提供完整离线媒体。部分学城专有组件显示为原文链接或源内容提示，原始数据完整保留于 source 文件。原文的敏感内容提示仍予保留。\n\n| 序号 | 文档 | 状态 | 文档 ID |\n| --- | --- | --- | --- |\n'+'\n'.join(mdrows)+'\n')
(BASE/'media.json').write_text(json.dumps({'storage':'source_url_only','references':assets,'unsupported_components':render_notes},ensure_ascii=False,indent=2))
report='# 权限与读取限制清单\n\n核查日期：'+DATE+'。逐篇执行只读操作；未申请、修改或授予权限。\n\n'+intro+'\n\n'
for status in ['no_permission','restricted_environment','read_failed']:
    group=[n for n in records if n['status']==status]
    if not group:continue
    report+='## '+STATUS[status]+f'（{len(group)} 篇）\n\n| 文档 | ID | 原因 |\n| --- | --- | --- |\n'
    for n in group:report+='| ['+n['title'].replace('|','\\|')+']('+n['url']+') | '+n['id']+' | '+n['reason']+' |\n'
    report+='\n'
report+='安全屋文档并不等同于已确认无阅读权限；当前环境无法读取，未请求安全屋 Token 或尝试其他访问路径。\n\n图片、媒体及附件仅保存原始链接，不将链接有效性等同于已下载或可离线访问。\n'
(BASE/'ACCESS_REPORT.md').write_text(report)
checksums={str(p.relative_to(BASE)):hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(DOCS.glob('*/source.*'))}
(BASE/'source-checksums.json').write_text(json.dumps(checksums,ensure_ascii=False,indent=2)+'\n')
print(json.dumps({'documents':len(records),'counts':dict(counts),'media_references':len(assets),'source_files':len(checksums)},ensure_ascii=False))
