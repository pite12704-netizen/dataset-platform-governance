"""Generate a source-only learning atlas; never load Settings, credentials, or business data.

Use --check to compare the checked-in reference with the current source. Call edges
are static candidates; dependency injection, closures and message dispatch need the
handwritten execution walkthrough in docs/project-deep-dive.md.
"""
import argparse
import ast
import copy
import hashlib
import json
import re
import shutil
import subprocess
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
METHOD_PAGES = {
    '01 当前后端': 'docs/reference/backend-methods.md',
    '02 前端与前端测试': 'docs/reference/frontend-methods.md',
    '03 数据库迁移': 'docs/reference/migration-methods.md',
    '04 后端与真实依赖测试': 'docs/reference/test-methods.md',
    '05 运维与实验脚本': 'docs/reference/script-methods.md',
    '07 保留引擎与历史原型': 'docs/reference/legacy-methods.md',
}
OUTPUTS = ['docs/code-atlas.md', 'docs/method-reference.md', 'docs/api-reference.md', 'docs/code-reference-data.json', *METHOD_PAGES.values()]
OPAQUE = {'deployment/compose.desktop.local', 'deployment/compose.env.local', 'deployment/service.env.local'}
MOUNTED = {'admin', 'auth', 'datasets', 'downloads', 'image_reviews', 'imports', 'spaces', 'tasks', 'uploads', 'workflows'}
EXCLUDED = {'.venv', 'node_modules', '.git', 'var', 'data', 'dist', 'build', '__pycache__', '.agents', '.codex', '.aws'}


def source_files():
    """Inventory owned source/config files, excluding dependencies, data and secrets."""
    result = subprocess.run(['rg', '--files'], cwd=ROOT, capture_output=True, text=True,
                            encoding='utf-8', check=True)
    files = []
    for raw in result.stdout.splitlines():
        name = raw.replace('\\', '/')
        p = Path(name)
        if set(p.parts) & EXCLUDED or 'secrets' in p.parts or p.name.startswith('.env'):
            continue
        if p.suffix in {'.py', '.ts', '.tsx', '.mjs', '.css', '.md', '.yml', '.json', '.ini', '.txt', '.toml', '.mako', '.html', '.sh', '.ps1', '.lock', '.tsbuildinfo', '.example'} or p.name.startswith('Dockerfile') or p.name in {'Caddyfile', 'README', 'compose.desktop.local', 'compose.env.local', 'service.env.local'}:
            files.append(name)
    return sorted(set(files) | set(OUTPUTS))


def group(name):
    """Separate deployed code, tests, operations and legacy prototypes."""
    if name.startswith('backend/app/'): return '01 当前后端'
    if name.startswith('frontend/'): return '02 前端与前端测试'
    if name.startswith('backend/migrations/') or name == 'backend/alembic.ini': return '03 数据库迁移'
    if name.startswith(('backend/tests/', 'backend/integration/')): return '04 后端与真实依赖测试'
    if name.startswith(('scripts/', 'clients/')): return '05 运维与实验脚本'
    if name.startswith(('deployment/', 'config/')): return '06 部署与地址配置'
    if name.startswith('usermanage/') or name in {'converter_core.py', 'quality_score_core.py', 'data_storage.py', 'DashBoard_.py'}: return '07 保留引擎与历史原型'
    return '08 文档与项目配置'


def file_meaning(name, notes):
    """Prefer curated facts, otherwise use a source docstring or its explicit category."""
    if name in notes['files']: return notes['files'][name]
    p = ROOT / name
    if name.startswith('usermanage/'):
        return '历史用户管理原型的 ' + p.stem + ' 模块；当前 app.main 未注册它，不能用来解释现行权限和业务表。'
    if '/migrations/versions/' in name:
        if p.exists():
            try: return (ast.get_docstring(ast.parse(p.read_text(encoding='utf-8-sig'))) or p.stem).strip()
            except SyntaxError: pass
        return '有明确 revision 的表结构迁移；upgrade/downgrade 由 Alembic 执行。'
    if name.startswith(('backend/tests/', 'backend/integration/', 'scripts/tests/', 'frontend/tests/')):
        return '隔离测试文件：' + p.stem + '；逐用例的断言与调用见方法索引，禁止用测试账号操作业务数据。'
    if name.endswith('.css'): return '对应 ' + p.stem + ' 页面/组件样式，不负责后端权限、数据库或队列。'
    if name.endswith('.md'): return '项目专题说明：' + p.stem + '；作为源码与实测证据的阅读入口。'
    if name.endswith('.lock'): return '精确依赖锁文件，构建用于复现环境，不是业务逻辑。'
    if name.endswith('.py') and p.exists():
        try:
            doc = ast.get_docstring(ast.parse(p.read_text(encoding='utf-8-sig')))
            if doc: return doc.strip()
        except SyntaxError: pass
    return '项目中的 ' + p.name + ' 配置/资源；参考所在目录分层和源码链接。'


def link(name, line=None, label=None):
    """Emit a clickable absolute workspace path with an optional verified line."""
    target = '/' + ROOT.as_posix().rstrip('/') + '/' + name
    return '[' + (label or name) + '](<' + target + (':' + str(line) if line else '') + '>)'


def short_expr(node):
    """Show structural expressions only, bounded to a readable line."""
    return ast.unparse(node).replace('\n', ' ')[:240]


def call_name(node):
    """Avoid duplicating whole SQL expressions in a chained method name."""
    if isinstance(node, ast.Name): return node.id
    if isinstance(node, ast.Attribute): return call_name(node.value) + '.' + node.attr
    if isinstance(node, ast.Call): return call_name(node.func) + '(...)'
    return short_expr(node)


def field_declaration(node, sensitive=False):
    """Keep password/schema constraints while masking sensitive literal defaults."""
    if node is None: return '无显式默认值；必填/继承规则见所属契约'
    if not sensitive: return short_expr(node)
    value=copy.deepcopy(node)
    if isinstance(value, ast.Call):
        for kw in value.keywords:
            if kw.arg=='default':kw.value=ast.Constant(value='<敏感默认值省略>')
        return short_expr(value)
    return '<敏感默认值省略>'


class Facts(ast.NodeVisitor):
    """Collect only the current function's operations; nested bodies have own entries."""
    def __init__(self):
        self.calls = set(); self.guards = []; self.returns = []; self.asserts = []; self.dependencies = set()

    def visit_FunctionDef(self, node): pass
    visit_AsyncFunctionDef = visit_FunctionDef
    def visit_ClassDef(self, node): pass

    def visit_Call(self, node):
        name = call_name(node.func)
        self.calls.add(name)
        if name == 'require' and node.args:
            message = node.args[1].value if len(node.args) > 1 and isinstance(node.args[1], ast.Constant) else None
            self.guards.append({'condition': short_expr(node.args[0]), 'message': message if isinstance(message, str) else ''})
        if name == 'Depends' and node.args: self.dependencies.add(short_expr(node.args[0]))
        self.generic_visit(node)

    def visit_Return(self, node):
        value = node.value
        if isinstance(value, ast.Dict):
            fields = [str(k.value) for k in value.keys if isinstance(k, ast.Constant)]
            self.returns.append('字典字段：' + ', '.join(fields))
        elif value is None: self.returns.append('None')
        elif isinstance(value, ast.Constant): self.returns.append(type(value.value).__name__)
        else: self.returns.append(short_expr(value))
        self.generic_visit(node)

    def visit_Assert(self, node):
        self.asserts.append(short_expr(node.test)); self.generic_visit(node)


class Symbols(ast.NodeVisitor):
    """Record classes/functions, including nested defs inside conditional branches."""
    def __init__(self, name, text, notes):
        self.name, self.text, self.notes = name, text, notes
        self.scope = []; self.records = []; self.router_prefix = ''; self.syntax = None

    def visit_Assign(self, node):
        if any(isinstance(t, ast.Name) and t.id == 'router' for t in node.targets) and isinstance(node.value, ast.Call):
            for kw in node.value.keywords:
                if kw.arg == 'prefix' and isinstance(kw.value, ast.Constant): self.router_prefix = kw.value.value
        self.generic_visit(node)

    def visit_ClassDef(self, node):
        qualified = '.'.join(self.scope + [node.name])
        fields = []
        for item in node.body:
            if isinstance(item, ast.AnnAssign) and isinstance(item.target, ast.Name):
                fields.append({'name': item.target.id, 'line': item.lineno, 'type': short_expr(item.annotation),
                    'declaration': field_declaration(item.value, any(x in item.target.id for x in ['password', 'secret', 'api_key', 'database_url']))})
        table = next((n.value.value for n in node.body if isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id == '__tablename__' for t in n.targets) and isinstance(n.value, ast.Constant)), None)
        constraints = [short_expr(n.value) for n in node.body if isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id == '__table_args__' for t in n.targets)]
        self.records.append({'name': qualified, 'line': node.lineno, 'kind': 'class', 'parameters': '',
            'doc': ast.get_docstring(node) or '', 'meaning': self.notes.get('models', {}).get(node.name, '') if self.name == 'backend/app/models.py' else '',
            'fields': fields, 'table': table, 'constraints': constraints, 'calls': [], 'guards': [], 'returns': [], 'asserts': [], 'routes': []})
        self.scope.append(node.name); self.generic_visit(node); self.scope.pop()

    def visit_FunctionDef(self, node):
        qualified = '.'.join(self.scope + [node.name]); f = Facts()
        for body in node.body: f.visit(body)
        for default in list(node.args.defaults) + [n for n in node.args.kw_defaults if n]: f.visit(default)
        routes = []
        for d in node.decorator_list:
            if isinstance(d, ast.Call) and isinstance(d.func, ast.Attribute) and d.func.attr in {'get','post','put','delete','patch','head','options'} and d.args and isinstance(d.args[0], ast.Constant):
                prefix = self.router_prefix
                if Path(self.name).stem in MOUNTED and self.name.startswith('backend/app/'): prefix = '/api/v1' + prefix
                routes.append({'method': d.func.attr.upper(), 'path': prefix + str(d.args[0].value)})
            f.visit(d)
        doc = ast.get_docstring(node) or ''
        meaning = self.notes['functions'].get(self.name + '::' + qualified, '')
        if not meaning and doc: meaning = doc.strip()
        if not meaning and node.name.startswith('test_'): meaning = '测试 ' + node.name.removeprefix('test_').replace('_',' ') + ' 场景；下列断言是实际验收条件。'
        if not meaning and self.scope:
            parent = self.notes['functions'].get(self.name + '::' + '.'.join(self.scope), '')
            if parent: meaning = '外层 ' + '.'.join(self.scope) + ' 的局部辅助阶段；具体操作由下面的调用与保护条件确定。'
        self.records.append({'name': qualified, 'line': node.lineno, 'end_line': node.end_lineno,
            'kind': 'async function' if isinstance(node, ast.AsyncFunctionDef) else 'function',
            'parameters': ast.unparse(node.args), 'return_type': short_expr(node.returns) if node.returns else '未显式标注，以下为源码返回形式',
            'meaning': meaning, 'doc': doc, 'calls': sorted(f.calls), 'guards': f.guards,
            'returns': list(dict.fromkeys(f.returns)), 'asserts': f.asserts, 'dependencies': sorted(f.dependencies), 'routes': routes})
        self.scope.append(node.name); self.generic_visit(node); self.scope.pop()

    visit_AsyncFunctionDef = visit_FunctionDef


def parse_python(name, notes):
    """Parse static source, preserving a legacy syntax failure as an explicit fact."""
    text = (ROOT / name).read_text(encoding='utf-8-sig')
    v = Symbols(name, text, notes)
    try: v.visit(ast.parse(text))
    except SyntaxError as exc:
        # Preserve names/lines in the legacy file without pretending it can execute.
        records=[{'name':m.group(2),'line':text[:m.start()].count('\n')+1,'kind':'unparsed function',
            'parameters':m.group(3),'meaning':'历史文件存在语法错误，无法执行；这里只按文本定位定义，不推断调用/返回。',
            'calls':[],'guards':[],'returns':[],'asserts':[],'routes':[]}
            for m in re.finditer(r'(?m)^[ \t]*(async[ \t]+)?def[ \t]+(\w+)\(([^\n]*)\)',text)]
        return records, {'line': exc.lineno, 'message': exc.msg}
    return sorted(v.records, key=lambda n:n['line']), None


def method_meaning(record, owner):
    """Distinguish curated intent from extracted implementation facts."""
    if record.get('meaning'): return record['meaning']
    if record.get('doc'): return record['doc']
    if record['kind'] == 'class': return '数据/状态契约类；字段及约束见下方，方法单独列出。'
    if record['name'].split('.')[-1] in {'render', 'expandedRowRender'}: return 'Ant Design 列表单元格/展开行的展示回调，按当前记录构造组件；具体请求调用见下方。'
    if record.get('trigger'): return '由 ' + record['trigger'] + ' 触发的局部回调；下面列出它实际执行的调用。'
    calls = record.get('calls', [])
    return '在本文件职责中执行的辅助方法；源码可确认的操作为 ' + ('、'.join(calls[:8]) if calls else '本地参数/状态计算') + '。这是结构说明，不把函数名当成已验证能力。'


def generate():
    """Build the atlas, symbols, HTTP index and machine-readable verification data."""
    notes = json.loads((ROOT/'docs/code-notes.json').read_text(encoding='utf-8'))
    files = source_files(); ts_files = [x for x in files if x.endswith(('.ts','.tsx','.mjs'))]
    node = shutil.which('node')
    if not node: raise RuntimeError('Node is needed for TypeScript AST extraction')
    front = json.loads(subprocess.run([node,'scripts/code_reference_frontend.mjs'], cwd=ROOT,
        input=json.dumps(ts_files),text=True,encoding='utf-8',capture_output=True,check=True).stdout)
    data = []; parse_errors=[]; all_routes=[]
    for name in files:
        records=[]; error=None
        if name.endswith('.py'): records,error = parse_python(name,notes)
        elif name in front: records=front[name]
        elif name.endswith('.ps1'):
            text=(ROOT/name).read_text(encoding='utf-8-sig')
            records=[{'name':m.group(1),'line':text[:m.start()].count('\n')+1,'kind':'PowerShell function',
                'parameters':'见 param 块/源码','calls':[],'guards':[],'returns':[],'asserts':[],'meaning':'在此脚本中执行 '+m.group(1)+' 阶段；PowerShell 调用与参数以源码为准。'}
                for m in re.finditer(r'(?im)^[ \t]*function[ \t]+([\w-]+)',text)]
        if error: parse_errors.append({'file':name,**error})
        for record in records:
            if not record.get('meaning'):
                record['meaning'] = notes['functions'].get(name + '::' + record['name'], '')
        opaque=name in OPAQUE
        p=ROOT/name
        digest=hashlib.sha256(p.read_bytes()).hexdigest() if p.exists() and name not in OUTPUTS and not opaque else None
        data.append({'path':name,'group':group(name),'purpose':file_meaning(name,notes),'symbols':records,'sha256':digest,'opaque':opaque,'parse_error':error})
        for r in records:
            for route in r.get('routes',[]): all_routes.append({**route,'file':name,'symbol':r['name'],'line':r['line'],'meaning':method_meaning(r,name),'parameters':r['parameters'],'dependencies':r.get('dependencies',[])})
    # Same-file named call candidates only: do not imply dynamic dispatch is observed.
    for entry in data:
        symbols=entry['symbols']; by_leaf=defaultdict(list)
        for r in symbols: by_leaf[r['name'].split('.')[-1]].append(r)
        for r in symbols:
            r['callers']=sorted({caller['name'] for caller in symbols if caller is not r and any(c in {r['name'], r['name'].split('.')[-1]} for c in caller.get('calls',[]))})
    stats={'files':len(files),'python_functions':sum(r['kind'] in {'function','async function'} for e in data if e['path'].endswith('.py') for r in e['symbols']),
        'frontend_functions_and_callbacks':sum(len(e['symbols']) for e in data if e['path'] in front),
        'powershell_functions':sum(len(e['symbols']) for e in data if e['path'].endswith('.ps1')),
        'current_api_routes':len([r for r in all_routes if r['file'].startswith('backend/app/')]),'parse_errors':parse_errors}
    atlas=['# 全项目文件职责索引','','这份索引只读取源码；不连接数据库、MinIO 或模型，也不复制真实环境与 secret 内容。快照日期：2026-10-11。',
        '',f"覆盖 {stats['files']} 个项目文件、{stats['python_functions']} 个 Python 具名函数/方法（含嵌套定义）、{stats['frontend_functions_and_callbacks']} 个前端函数/回调、{stats['powershell_functions']} 个 PowerShell 函数。依赖目录、上传数据、var 运行产物、构建缓存主体不作为业务源码逐条学习。",
        '', '学习总入口：[代码学习手册](../CODE_STUDY_GUIDE.md)；调用链：[逐步执行与面试理解](project-deep-dive.md)；逐方法：[方法索引](method-reference.md)；HTTP：[接口索引](api-reference.md)。',
        '', '## 阅读范围与安全边界','','`backend/app` 是现在的运行后端；旧 `usermanage`、旧看板与评分 Web API 没有由当前工厂注册。`converter_core.convert` 仍被当前转换任务调用，但开放格式小于引擎全部能力。测试文件表示隔离验收，实验脚本不等于生产服务。',
        '', '`deployment/secrets/` 与 `.env*` 是凭据/环境输入；`var/` 是上传暂存、报告、备份、日志和进程记录；`.venv/` 与 `node_modules/` 是依赖；`frontend/dist/` 是可重新构建的资源。它们的目录角色必须了解，但不能把密钥内容或每个三方依赖当项目方法背诵。',
        '', '本地 private 配置仅列文件职责，不解析其内容。当前历史 `usermanage/alembic/env.py` 在第 28 行存在中文引号语法错误；它不是生产迁移入口。现行迁移位于 `backend/migrations/`。']
    methods=['# 全项目方法、类与参数索引','','本文件由 `scripts/build_code_reference.py` 从源码生成。每个条目给出当前行号、参数、人工说明/原注释、实际调用、保护条件、返回形式与同文件调用候选。',
        '', '**阅读规则：**入口装饰器、依赖注入、Celery 消息和回调决定“何时调用”；下面的调用集合只表示静态函数体中出现，不代表所有分支每次执行，也不是完整跨进程调用图。同名方法候选可能重名。动态链路按 [执行详解](project-deep-dive.md) 验证。',
        '', '参数里 `db` 是本阶段会话、`request` 是 HTTP 上下文、`user` 是当前认证主体、`settings` 是进程配置；`version_id` 固定输入版本，`run_token/claim_token` 是执行/领取身份，`revision` 是本次编辑基于的状态。`self` 表示实例自己的字段和资源，不代表线程安全。',
        '', '匿名 JavaScript 回调用 `触发源@L行号` 定位，Python lambda 就近阅读调用行。纯配置文件没有方法，其职责见 [文件索引](code-atlas.md)。']
    for g in sorted({e['group'] for e in data}):
        atlas+=['', '## '+g]
        for e in [x for x in data if x['group']==g]:
            atlas+=['','### '+e['path'],'',link(e['path']),'',e['purpose'], '', f"定义数：{len(e['symbols'])}。" if e['symbols'] else '没有具名业务方法；按配置/资源用途阅读。']
            if e['parse_error']: atlas+=['',f"静态解析限制：第 {e['parse_error']['line']} 行，{e['parse_error']['message']}。"]
            if not e['symbols'] and not e['parse_error']:continue
            methods+=['', '## '+e['path'], '',e['purpose']]
            if e['parse_error']: methods+=['',f"第 {e['parse_error']['line']} 行不能按 Python 解析，见源码；该历史文件未计入可解析方法数。"]
            for r in e['symbols']:
                methods+=['','### '+r['name'], '',link(e['path'],r['line'],f"源码 L{r['line']}"), '', '**作用：**'+method_meaning(r,e['path'])]
                if r['kind']=='class':
                    if r.get('table'): methods+=['', '**数据库表：** `'+r['table']+'`；公共 `id/created_at` 来自 Identified。']
                    for field in r.get('fields',[]):methods+=['',f"- `{field['name']}: {field['type']}`：`{field['declaration']}`。"]
                    for constraint in r.get('constraints',[]): methods+=['', '**约束/索引：** `'+constraint+'`。']
                    continue
                methods+=['',f"**执行方式：** `{r['kind']}`。**参数：** `{r.get('parameters','')}`。"]
                if r.get('routes'): methods+=['','**入口：** '+', '.join('`'+x['method']+' '+x['path']+'`' for x in r['routes'])+'。']
                if r.get('trigger'): methods+=['','**触发源：** `'+r['trigger']+'`。']
                if r.get('dependencies'):methods+=['','**依赖：** '+', '.join('`'+x+'`' for x in r['dependencies'])+'。']
                calls=r.get('calls',[])
                if calls: methods+=['','**调用/操作：** '+', '.join('`'+x+'`' for x in calls)+'。']
                if r.get('callers'): methods+=['','**同文件调用候选：** '+', '.join('`'+x+'`' for x in r['callers'])+'。']
                if r.get('returns'):methods+=['','**返回形式：** '+'；'.join('`'+x+'`' for x in r['returns'])+'。']
                for guard in r.get('guards',[]): methods+=['', '- 保护：`'+guard['condition']+'`'+('；失败提示：'+guard['message'] if guard['message'] else '')+'。']
                for assertion in r.get('asserts',[]): methods+=['','- 测试断言：`'+assertion+'`。']
    api=['# 当前 HTTP 接口与契约索引','','只列 `backend/app` 当前注册路由，不列历史服务和实验脚本。API 文档是当前地址的 `/docs`；此离线索引展示源码中的真实方法、路径和依赖。',
        '', '请求到达顺序：Caddy → CORS/RequestGuard → Pydantic 输入 → Depends 身份与会话 → 路由/领域权限 → 提交/调度 → JSON。异步路由可能把身份/SQL 放进 run_sql/run_db；没有 Depends 不等于不鉴权。',
        '', '返回 401 是认证无效；403 是无权限；404 也用于避免资源存在性泄露；409 是状态/并发/幂等冲突；413 是容量；416 是 Range 无效；429 是限流；503 是依赖或容量暂不可用。写请求超时须先按幂等号回查。']
    for r in sorted((x for x in all_routes if x['file'].startswith('backend/app/')),key=lambda x:(x['file'],x['line'])):
        api+=['','## '+r['method']+' '+r['path'],'',link(r['file'],r['line'],r['symbol']),'',r['meaning'], '', '**参数契约：** `'+r['parameters']+'`。']
        if r['dependencies']:api+=['', '**Depends：** '+', '.join('`'+x+'`' for x in r['dependencies'])+'。']
    api+=['', '输入类字段、枚举、长度与校验器按 [方法索引](method-reference.md) 中 schemas.py 及各路由局部输入类阅读。输出仍按运行时 JSON/前端类型核实，不把源码返回表达式当稳定外部契约。']
    machine={'snapshot_date':'2026-10-11','scope':'owned source only; static facts, not observed runtime calls','statistics':stats,'files':data,'routes':all_routes}
    # Split the large definition reference by layer so beginners can read one layer at a time.
    intro_end = next(i for i, line in enumerate(methods) if line.startswith('## '))
    intro = methods[:intro_end]
    grouped = {g: [] for g in METHOD_PAGES}
    current = None
    for line in methods[intro_end:]:
        if line.startswith('## '): current = group(line[3:])
        if current in grouped: grouped[current].append(line)
    navigation = ['# 方法阅读导航', '', '逐方法说明按执行层拆分，含源码行号、参数、入口、实际调用、保护条件、返回和测试断言。源码事实与人工语义分别展示。',
        '', f"本次覆盖 {stats['python_functions']} 个 Python 具名函数/方法和 {stats['frontend_functions_and_callbacks']} 个前端函数/回调；类与字段另列。", '', '先读 [任务与业务执行详解](project-deep-dive.md)，再按调用链查本索引。']
    pages = {}
    for g, name in METHOD_PAGES.items():
        adjusted=[line.replace('](project-deep-dive.md)','](../project-deep-dive.md)').replace('](code-atlas.md)','](../code-atlas.md)') for line in intro]
        pages[name] = '\n'.join(adjusted + ['## 本层文件导航', ''] + [ '- ' + link(e['path']) for e in data if e['group'] == g and e['symbols']] + grouped[g]) + '\n'
        navigation += ['', '## ' + g, '', link(name, label='打开本层方法说明')]
        for e in data:
            if e['group'] == g and e['symbols']: navigation += ['', '- `' + e['path'] + '`：' + str(len(e['symbols'])) + ' 个定义；' + e['purpose'].split('\n')[0]]
    outputs = {OUTPUTS[0]:'\n'.join(atlas)+'\n', OUTPUTS[1]:'\n'.join(navigation)+'\n', OUTPUTS[2]:'\n'.join(api)+'\n', OUTPUTS[3]:json.dumps(machine,ensure_ascii=False,indent=2)+'\n', **pages}
    return outputs, stats


def main():
    """Write references, or fail --check when source and generated content differ."""
    parser=argparse.ArgumentParser();parser.add_argument('--check',action='store_true');args=parser.parse_args()
    outputs,stats=generate(); mismatches=[]
    for name,content in outputs.items():
        path=ROOT/name
        if args.check:
            if not path.exists() or path.read_text(encoding='utf-8')!=content:mismatches.append(name)
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content,encoding='utf-8')
    print(json.dumps({'statistics':stats,'out_of_date':mismatches,'mode':'check' if args.check else 'write'},ensure_ascii=False))
    if mismatches:raise SystemExit(1)


if __name__=='__main__':main()
