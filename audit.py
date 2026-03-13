"""
audit.py — запускать перед каждым деплоем.
Проверяет:
  1. Синтаксис всех .py файлов
  2. self.method() которые не определены в классе
  3. Константы-самоссылки (FOO = FOO)
  4. @contextmanager без yield
  5. Реальный импорт всех модулей (с mock внешних зависимостей)

Использование:
  python3 audit.py
"""
import ast, re, sys, types, os
from pathlib import Path

base = Path(__file__).parent
errors = []

# ══════════════════════════════════════════════════════════
# 1. СИНТАКСИС
# ══════════════════════════════════════════════════════════
for p in sorted(base.rglob('*.py')):
    if '__pycache__' in str(p) or p.name == 'audit.py': continue
    try:
        ast.parse(p.read_text())
    except SyntaxError as e:
        errors.append(f"SYNTAX {p.relative_to(base)}:{e.lineno}: {e.msg}")

# ══════════════════════════════════════════════════════════
# 2. MISSING self.method() — с учётом наследования и callable-атрибутов
# ══════════════════════════════════════════════════════════
INHERITED = {
    '_build_messages', '_system_prompt', '_execute', 'run',
    '_llm', 'agent_name', 'timeout', '_format_facts',
}

for p in sorted(base.rglob('*.py')):
    if '__pycache__' in str(p) or p.name == 'audit.py': continue
    src = p.read_text()
    rel = str(p.relative_to(base))
    try:
        tree = ast.parse(src)
    except SyntaxError:
        continue

    for cls_node in ast.walk(tree):
        if not isinstance(cls_node, ast.ClassDef): continue
        cls = cls_node.name

        own_methods = set()
        for item in ast.walk(cls_node):
            if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                own_methods.add(item.name)

        # Callable-атрибуты назначенные в __init__ (self._xxx = something)
        callable_attrs = set()
        for item in ast.walk(cls_node):
            if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)) and item.name == '__init__':
                for stmt in ast.walk(item):
                    if isinstance(stmt, ast.Assign):
                        for t in stmt.targets:
                            if (isinstance(t, ast.Attribute) and
                                    isinstance(t.value, ast.Name) and t.value.id == 'self'):
                                callable_attrs.add(t.attr)

        available = own_methods | INHERITED | callable_attrs
        src_class = ast.get_source_segment(src, cls_node) or ''
        self_calls = set(re.findall(r'self\.([a-z_]\w+)\s*\(', src_class))

        for call in self_calls:
            if call not in available and not call.startswith('__'):
                ln = src_class.find(f'self.{call}(')
                line_no = (src[:src.find(src_class)].count('\n') +
                           src_class[:ln].count('\n') + 1)
                errors.append(f"MISSING_METHOD {rel}:{line_no}: {cls}.{call}()")

# ══════════════════════════════════════════════════════════
# 3. КОНСТАНТЫ-САМОССЫЛКИ
# ══════════════════════════════════════════════════════════
for p in sorted(base.rglob('*.py')):
    if '__pycache__' in str(p) or p.name == 'audit.py': continue
    src = p.read_text()
    rel = str(p.relative_to(base))
    for m in re.finditer(r'^([A-Z_]{3,})\s*=\s*\1\b', src, re.MULTILINE):
        ln = src[:m.start()].count('\n') + 1
        errors.append(f"SELF_REF {rel}:{ln}: '{m.group(1)} = {m.group(1)}'")

# ══════════════════════════════════════════════════════════
# 4. @contextmanager БЕЗ yield
# ══════════════════════════════════════════════════════════
for p in sorted(base.rglob('*.py')):
    if '__pycache__' in str(p) or p.name == 'audit.py': continue
    src = p.read_text()
    rel = str(p.relative_to(base))
    lines = src.split('\n')
    for i, l in enumerate(lines[:-1]):
        if '@contextmanager' in l:
            body = '\n'.join(lines[i+1:i+60])
            if 'yield' not in body:
                errors.append(f"CTX_NO_YIELD {rel}:{i+1}: @contextmanager без yield")

# ══════════════════════════════════════════════════════════
# 5. РЕАЛЬНЫЙ ИМПОРТ (mock внешних зависимостей)
# ══════════════════════════════════════════════════════════
sys.path.insert(0, str(base))

def make_cls(name):
    return type(name, (), {'__init__': lambda s, *a, **kw: None})

for mod_name, attrs in {
    'groq': ['Groq', 'AsyncGroq'],
    'aiogram': ['Bot', 'Dispatcher', 'Router', 'F'],
    'aiogram.filters': ['Command'],
    'aiogram.types': ['Message', 'Document', 'PhotoSize', 'Voice', 'TelegramObject'],
    'aiogram.client.default': ['DefaultBotProperties'],
    'aiogram.dispatcher.middlewares.base': ['BaseMiddleware'],
    'langchain_groq': ['ChatGroq'],
    'langchain_core': [],
    'langchain_core.messages': ['HumanMessage', 'SystemMessage', 'AIMessage', 'BaseMessage'],
    'langchain_core.messages.base': ['BaseMessage'],
    'fastapi': ['FastAPI', 'HTTPException', 'Request', 'Depends', 'Query'],
    'fastapi.responses': ['JSONResponse', 'HTMLResponse', 'FileResponse'],
    'fastapi.staticfiles': ['StaticFiles'],
    'pydantic': ['field_validator', 'BaseModel', 'Field'],
    'pydantic_settings': ['BaseSettings', 'SettingsConfigDict'],
    'gtts': ['gTTS'],
    'pydub': [], 'pydub.audio_segment': ['AudioSegment'],
    'aiofiles': [],
    'PIL': [], 'PIL.Image': ['Image'],
    'tavily': ['AsyncTavilyClient'],
    'tavily.client': ['AsyncTavilyClient'],
    'huggingface_hub': ['InferenceClient'],
    'uvicorn': ['run'],
}.items():
    m = types.ModuleType(mod_name)
    for a in attrs:
        setattr(m, a, make_cls(a))
    m.BaseMiddleware = object
    m.BaseSettings = object
    m.SettingsConfigDict = dict
    m.Field = lambda *a, **kw: None
    m.field_validator = lambda *a, **kw: (lambda f: f)
    m.BaseModel = object
    fake = make_cls('App')
    for method in ('get', 'post', 'delete', 'put', 'patch', 'mount', 'add_middleware'):
        setattr(fake, method, lambda *a, **kw: (lambda f: f))
    m.FastAPI = lambda *a, **kw: fake()
    m.HTTPException = Exception
    m.Query = lambda *a, **kw: None
    sys.modules[mod_name] = m

os.environ.update({
    'GROQ_API_KEY': 'gsk_test', 'TELEGRAM_TOKEN': '1:test',
    'ALLOWED_USER_IDS': '123', 'TELEGRAM_USER_ID': '123',
    'WEB_TOKEN': 'sokrat', 'MODEL_NAME': 'llama-3.3-70b-versatile',
    'TEMPERATURE': '0.7', 'MAX_HISTORY': '20', 'DB_PATH': '/tmp/test.db',
})

for mod in [
    'app.config', 'app.utils', 'app.database',
    'app.agents.base_agent', 'app.agents.registry', 'app.agents.router',
    'app.agents.orchestrator', 'app.agents.raya_agent', 'app.agents.code_agent',
    'app.agents.image_agent', 'app.agents.diary_agent', 'app.agents.research_agent',
    'app.agents.science_agent', 'app.agents.todo_agent', 'app.agents.morning_agent',
    'app.agents.text_agent', 'app.agents.ideas_agent', 'app.agents.planning_agent',
    'app.agents.explain_agent', 'app.agents.critic_agent',
    'app.llm_pipeline', 'app.llm_service', 'app.personality_service',
    'app.proactive_service', 'app.search_service', 'app.middleware',
    'app.handlers', 'app.web_server', 'app.core',
]:
    try:
        __import__(mod)
    except Exception as e:
        errors.append(f"IMPORT {mod}: {type(e).__name__}: {e}")

# ══════════════════════════════════════════════════════════
# ИТОГ
# ══════════════════════════════════════════════════════════
n = sum(1 for p in base.rglob('*.py')
        if '__pycache__' not in str(p) and p.name != 'audit.py')
unique = sorted(set(errors))
print(f"\nПроверено: {n} файлов")
print(f"{'✅  ЧИСТО — МОЖНО ДЕПЛОИТЬ' if not unique else f'❌  ОШИБОК: {len(unique)} — НЕ ДЕПЛОИТЬ'}\n")
for e in unique:
    print(f"  ❌ {e}")

sys.exit(1 if unique else 0)
