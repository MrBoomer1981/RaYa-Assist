"""
audit.py — запускать перед каждым деплоем.
Использование: python3 audit.py
"""
import ast, re, sys, types, os
from pathlib import Path

base = Path(__file__).parent
errors = []

# Сканируем ТОЛЬКО app/ и main.py — НЕ venv, NOT __pycache__
def get_sources():
    result = []
    for target in [base / 'app', base / 'main.py']:
        if target.is_file():
            result.append(target)
        elif target.is_dir():
            for p in sorted(target.rglob('*.py')):
                if '__pycache__' not in str(p):
                    result.append(p)
    return result

sources = get_sources()
print(f"Проверяю {len(sources)} файлов...")

# 1. СИНТАКСИС
parsed = {}
for p in sources:
    try:
        src = p.read_text(encoding='utf-8')
        parsed[p] = (src, ast.parse(src))
    except SyntaxError as e:
        errors.append(f"SYNTAX {p.relative_to(base)}:{e.lineno}: {e.msg}")
    except Exception as e:
        errors.append(f"READ {p.relative_to(base)}: {e}")

# 2. MISSING self.method()
INHERITED = {'_build_messages','_system_prompt','_execute','run','_llm','agent_name','timeout','_format_facts'}
for p,(src,tree) in parsed.items():
    rel = str(p.relative_to(base))
    for cls_node in ast.walk(tree):
        if not isinstance(cls_node, ast.ClassDef): continue
        own = {n.name for n in ast.walk(cls_node) if isinstance(n,(ast.FunctionDef,ast.AsyncFunctionDef))}
        attrs = set()
        for n in ast.walk(cls_node):
            if isinstance(n,(ast.FunctionDef,ast.AsyncFunctionDef)) and n.name=='__init__':
                for s in ast.walk(n):
                    if isinstance(s,ast.Assign):
                        for t in s.targets:
                            if isinstance(t,ast.Attribute) and isinstance(t.value,ast.Name) and t.value.id=='self':
                                attrs.add(t.attr)
        available = own | INHERITED | attrs
        sc = ast.get_source_segment(src,cls_node) or ''
        for call in set(re.findall(r'self\.([a-z_]\w+)\s*\(',sc)):
            if call not in available and not call.startswith('__'):
                ln = src[:src.find(sc)].count('\n') + sc[:sc.find(f'self.{call}(')].count('\n') + 1
                errors.append(f"MISSING_METHOD {rel}:{ln}: {cls_node.name}.{call}()")

# 3. КОНСТАНТЫ-САМОССЫЛКИ
for p,(src,_) in parsed.items():
    for m in re.finditer(r'^([A-Z_]{3,})\s*=\s*\1\b',src,re.MULTILINE):
        errors.append(f"SELF_REF {p.relative_to(base)}:{src[:m.start()].count(chr(10))+1}: '{m.group(1)}={m.group(1)}'")

# 4. @contextmanager БЕЗ yield
for p,(src,_) in parsed.items():
    lines = src.split('\n')
    for i,l in enumerate(lines[:-1]):
        if '@contextmanager' in l and 'yield' not in '\n'.join(lines[i+1:i+60]):
            errors.append(f"CTX_NO_YIELD {p.relative_to(base)}:{i+1}: без yield")

# 5. ИМПОРТ (mock внешних зависимостей)
sys.path.insert(0, str(base))
def mc(n): return type(n,(),{'__init__':lambda s,*a,**kw:None})
for mn,attrs in {
    'groq':['Groq','AsyncGroq'],'aiogram':['Bot','Dispatcher','Router','F'],
    'aiogram.filters':['Command'],'aiogram.types':['Message','Document','PhotoSize','Voice','TelegramObject','BufferedInputFile'],
    'aiogram.client.default':['DefaultBotProperties'],'aiogram.dispatcher.middlewares.base':['BaseMiddleware'],
    'langchain_groq':['ChatGroq'],'langchain_core':[],'langchain_core.messages':['HumanMessage','SystemMessage','AIMessage','BaseMessage'],
    'langchain_core.messages.base':['BaseMessage'],'fastapi':['FastAPI','HTTPException','Request','Depends','Query'],
    'fastapi.responses':['JSONResponse','HTMLResponse','FileResponse'],'fastapi.staticfiles':['StaticFiles'],
    'pydantic':['field_validator','BaseModel','Field'],'pydantic_settings':['BaseSettings','SettingsConfigDict'],
    'gtts':['gTTS'],'pydub':[],'pydub.audio_segment':['AudioSegment'],'aiofiles':[],
    'PIL':[],'PIL.Image':['Image'],'tavily':['AsyncTavilyClient'],'tavily.client':['AsyncTavilyClient'],
    'huggingface_hub':['InferenceClient'],'uvicorn':['run'],
}.items():
    m=types.ModuleType(mn)
    for a in attrs: setattr(m,a,mc(a))
    m.BaseMiddleware=object; m.BaseSettings=object; m.SettingsConfigDict=dict
    m.Field=lambda*a,**kw:None; m.field_validator=lambda*a,**kw:(lambda f:f); m.BaseModel=object
    fk=mc('App')
    for mth in('get','post','delete','put','patch','mount','add_middleware'): setattr(fk,mth,lambda*a,**kw:(lambda f:f))
    m.FastAPI=lambda*a,**kw:fk(); m.HTTPException=Exception; m.Query=lambda*a,**kw:None
    sys.modules[mn]=m

os.environ.update({'GROQ_API_KEY':'gsk_test','TELEGRAM_TOKEN':'1:test','ALLOWED_USER_IDS':'123',
    'TELEGRAM_USER_ID':'123','WEB_TOKEN':'sokrat','MODEL_NAME':'llama-3.3-70b-versatile',
    'TEMPERATURE':'0.7','MAX_HISTORY':'20','DB_PATH':'/tmp/test.db'})

# Очищаем кеш app.* перед каждым импортом чтобы избежать накопления
for mod in ['app.config','app.utils','app.database','app.agents.base_agent','app.agents.registry',
    'app.agents.router','app.agents.orchestrator','app.agents.raya_agent','app.agents.code_agent',
    'app.agents.image_agent','app.agents.research_agent',
    'app.agents.todo_agent','app.agents.morning_agent','app.agents.text_agent','app.agents.ideas_agent',
    'app.agents.explain_agent','app.agents.critic_agent','app.llm_pipeline',
    'app.llm_service','app.personality_service','app.proactive_service','app.search_service',
    'app.middleware','app.handlers','app.web_server','app.core']:
    for k in [k for k in sys.modules if k.startswith('app.')]: del sys.modules[k]
    try: __import__(mod)
    except Exception as e: errors.append(f"IMPORT {mod}: {type(e).__name__}: {e}")

# ИТОГ
u=sorted(set(errors))
print(f"{'✅  ЧИСТО — МОЖНО ДЕПЛОИТЬ' if not u else f'❌  ОШИБОК: {len(u)}'}\n")
for e in u: print(f"  ❌ {e}")
sys.exit(1 if u else 0)

# ══════════════════════════════════════════════════════════
# 6. INLINE ИМПОРТЫ тяжёлых библиотек внутри функций
# ══════════════════════════════════════════════════════════
import re as _re
_HEAVY = ('langchain', 'aiogram', 'fastapi', 'pydantic', 'groq', 'huggingface')
for p in sources:
    src, _ = parsed.get(p, (p.read_text(encoding='utf-8'), None))
    rel = str(p.relative_to(base))
    for m in _re.finditer(r'^( {8,})from (' + '|'.join(_HEAVY) + r').*import', src, _re.MULTILINE):
        ln = src[:m.start()].count('\n') + 1
        errors.append(f"INLINE_IMPORT {rel}:{ln}: {src.split(chr(10))[ln-1].strip()}")

# ══════════════════════════════════════════════════════════
# 7. STDLIB модули используются без top-level импорта
# ══════════════════════════════════════════════════════════
_STDLIB_CHECK = {
    'sqlite3': r'sqlite3\.(connect|execute)',
    'calendar': r'calendar\.(monthrange)',
    'time': r'time\.(monotonic|sleep|time)',
    'hashlib': r'hashlib\.(md5|sha)',
    'base64': r'base64\.(b64|encode|decode)',
}
for p, (src, tree) in parsed.items():
    rel = str(p.relative_to(base))
    top = {a.asname or a.name.split('.')[0]
           for node in tree.body
           if isinstance(node, (ast.Import, ast.ImportFrom))
           for a in node.names}
    for mod, pattern in _STDLIB_CHECK.items():
        if mod not in top and _re.search(pattern, src):
            errors.append(f"MISSING_STDLIB {rel}: '{mod}' используется без top-level импорта")

# ══════════════════════════════════════════════════════════
# 8. БЕЗУСЛОВНАЯ РЕКУРСИЯ (функция всегда вызывает себя)
# ══════════════════════════════════════════════════════════
for p, (src, tree) in parsed.items():
    rel = str(p.relative_to(base))
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        fname = node.name
        body = ast.get_source_segment(src, node) or ''
        # Есть ровно один вызов себя AND нет if/while (нет базового случая)
        self_calls = _re.findall(rf'\b{_re.escape(fname)}\s*\(', body)
        if (len(self_calls) == 1
                and 'if ' not in body
                and _re.search(rf'^\s+return {_re.escape(fname)}\(', body, _re.MULTILINE)):
            ln = src[:src.find(body)].count('\n') + 1
            errors.append(f"RECURSION {rel}:{ln}: {fname}() всегда рекурсирует — нет базового случая")

# ══════════════════════════════════════════════════════════
# ПЕРЕПИСЫВАЕМ ИТОГ (заменяем старый)
# ══════════════════════════════════════════════════════════
