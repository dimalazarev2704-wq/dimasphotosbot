import sys, types, copy, asyncio, re

# ---- stub telegram so bot.py can be imported without the library ----
class _Meta(type):
    def __getattr__(cls,n):
        if n.startswith("__"): raise AttributeError(n)
        return n
class _Any(metaclass=_Meta):
    def __init__(self,*a,**k): self.a=a; self.k=k
    def __call__(self,*a,**k): return _Any(*a,**k)
    def __getattr__(self,n): return _Any()
class TelegramError(Exception):
    def __init__(self,m="x"): super().__init__(m); self.message=m
class TimedOut(TelegramError): pass
class NetworkError(TelegramError): pass
class BadRequest(TelegramError): pass
class Forbidden(TelegramError): pass
class RetryAfter(TelegramError):
    def __init__(self,r=1): super().__init__("retry"); self.retry_after=r
class ChatMigrated(TelegramError): pass
def _mod(name, **attrs):
    m = types.ModuleType(name)
    def ga(n):
        if n.startswith("__"): raise AttributeError(n)
        c = _Meta(n,(_Any,),{}); setattr(m,n,c); return c
    m.__getattr__ = ga
    for k,v in attrs.items(): setattr(m,k,v)
    sys.modules[name]=m; return m
class Btn:
    def __init__(self,text,callback_data=None,web_app=None,url=None,**k): self.text=text; self.callback_data=callback_data; self.web_app=web_app
class Markup:
    def __init__(self,kb=None,**k): self.inline_keyboard=kb or []
_mod("telegram", InlineKeyboardButton=Btn, InlineKeyboardMarkup=Markup)
_mod("telegram.error", TelegramError=TelegramError, TimedOut=TimedOut, NetworkError=NetworkError, BadRequest=BadRequest, Forbidden=Forbidden, RetryAfter=RetryAfter, ChatMigrated=ChatMigrated)
_mod("telegram.constants"); _mod("telegram.request"); _mod("telegram.helpers")
class _Ctx: DEFAULT_TYPE=object
_mod("telegram.ext", ContextTypes=_Ctx)

# ---- tiny in-memory Mongo ----
def _get(d,k):
    for p in k.split("."):
        if not isinstance(d,dict) or p not in d: return None
        d=d[p]
    return d
def _match(doc,q):
    for k,v in q.items():
        if k=="$or": 
            if not any(_match(doc,x) for x in v): return False
            continue
        val=_get(doc,k)
        if isinstance(v,dict) and any(x.startswith("$") for x in v):
            for op,a in v.items():
                if op=="$in":
                    if (val if not isinstance(val,list) else None) not in a and not (isinstance(val,list) and any(x in a for x in val)): return False
                elif op=="$nin":
                    if val in a: return False
                elif op=="$exists":
                    if (k.split(".")[0] in doc) != a: return False
                elif op=="$ne":
                    if val==a: return False
                elif op=="$gt":
                    if val is None or not val>a: return False
                else: raise NotImplementedError(op)
        else:
            if val!=v: return False
    return True
def _apply(doc,u):
    for op,f in u.items():
        for k,v in f.items():
            if op=="$set": doc[k]=copy.deepcopy(v)
            elif op=="$unset": doc.pop(k,None)
            elif op=="$inc": doc[k]=doc.get(k,0)+v
            elif op=="$push":
                l=doc.setdefault(k,[])
                if isinstance(v,dict) and "$each" in v: l.extend(copy.deepcopy(v["$each"]))
                else: l.append(copy.deepcopy(v))
            elif op=="$setOnInsert": pass
            else: raise NotImplementedError(op)
class Cur:
    def __init__(s,docs): s.docs=docs
    def sort(s,key,d=1):
        if isinstance(key,list): key,d=key[0]
        s.docs=sorted(s.docs,key=lambda x:(x.get(key) is None,x.get(key)),reverse=d==-1); return s
    def limit(s,n): s.docs=s.docs[:n]; return s
    def skip(s,n): s.docs=s.docs[n:]; return s
    async def to_list(s,n=None): return [copy.deepcopy(x) for x in (s.docs if not n else s.docs[:n])]
class Coll:
    def __init__(s): s.d=[]
    def find(s,q=None,proj=None): return Cur([x for x in s.d if _match(x,q or {})])
    async def find_one(s,q=None,sort=None):
        c=s.find(q)
        if sort: c.sort(*sort[0])
        return copy.deepcopy(c.docs[0]) if c.docs else None
    async def insert_one(s,doc): s.d.append(copy.deepcopy(doc))
    async def update_one(s,q,u,upsert=False):
        for x in s.d:
            if _match(x,q): _apply(x,u); return
        if upsert:
            n={k:v for k,v in q.items() if not isinstance(v,dict)}
            for k,v in u.get("$setOnInsert",{}).items(): n[k]=v
            _apply(n,u); s.d.append(n)
    async def update_many(s,q,u):
        for x in s.d:
            if _match(x,q): _apply(x,u)
    async def delete_one(s,q):
        for i,x in enumerate(s.d):
            if _match(x,q): del s.d[i]; return
    async def delete_many(s,q): s.d=[x for x in s.d if not _match(x,q)]
    async def count_documents(s,q): return len([x for x in s.d if _match(x,q)])
    async def aggregate(s,pipe):
        rows=[x for x in s.d]; out=rows
        for st in pipe:
            if "$match" in st: out=[x for x in out if _match(x,st["$match"])]
            elif "$group" in st:
                g=st["$group"]; fld=g["_id"][1:]; res={}
                for x in out:
                    k=x.get(fld); r=res.setdefault(k,{"_id":k})
                    for name,spec in g.items():
                        if name=="_id": continue
                        (op,arg),=spec.items()
                        if op=="$sum": r[name]=r.get(name,0)+(1 if arg==1 else x.get(arg[1:],0))
                        elif op=="$first": r.setdefault(name,x.get(arg[1:]))
                out=list(res.values())
            elif "$sort" in st:
                (k,d),=st["$sort"].items(); out=sorted(out,key=lambda x:(x.get(k) is None,x.get(k)),reverse=d==-1)
        class C:
            async def to_list(self,n=None): return copy.deepcopy(out)
        return C()
class DB:
    def __init__(s): s.c={}
    def __getattr__(s,n):
        if n.startswith("__"): raise AttributeError(n)
        return s.c.setdefault(n,Coll())

for n in ("pymongo","pymongo.errors","pymongo.asynchronous","pymongo.asynchronous.collection","dotenv","aiohttp"):
    if n not in sys.modules:
        try: __import__(n)
        except Exception: _mod(n)
class _PE(Exception): pass
sys.modules["pymongo.errors"].PyMongoError=_PE; sys.modules["pymongo.errors"].DuplicateKeyError=_PE
