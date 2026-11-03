/** Read-only TypeScript AST extraction for the source learning reference. */
import fs from 'node:fs';
import path from 'node:path';
import { createRequire } from 'node:module';
import { fileURLToPath } from 'node:url';
const require = createRequire(new URL('../frontend/package.json', import.meta.url));
const ts = require('typescript');
const root = path.resolve(fileURLToPath(new URL('..', import.meta.url)));
const input = JSON.parse(fs.readFileSync(0, 'utf8'));
const result = {};
for (const relative of input) {
  const full = path.resolve(root, relative);
  const source = ts.createSourceFile(full, fs.readFileSync(full, 'utf8'), ts.ScriptTarget.Latest, true);
  const records = [];
  const seen = new Set();
  const compact = value => value.replace(/\s+/g, ' ').trim();
  function callName(node) {
    if (ts.isIdentifier(node)) return node.text;
    if (ts.isPropertyAccessExpression(node)) return callName(node.expression) + '.' + node.name.text;
    if (ts.isCallExpression(node)) return callName(node.expression) + '(...)';
    if (ts.isParenthesizedExpression(node)) return callName(node.expression);
    if (ts.isElementAccessExpression(node)) return callName(node.expression) + '[...]';
    if (ts.isArrayLiteralExpression(node)) return '<array>';
    if (ts.isArrowFunction(node) || ts.isFunctionExpression(node)) return '<callback@L' + (source.getLineAndCharacterOfPosition(node.getStart(source)).line + 1) + '>';
    return compact(node.getText(source)).slice(0, 180);
  }
  function visit(node, scope = []) {
    let next = scope;
    const callable = ts.isFunctionDeclaration(node) || ts.isFunctionExpression(node) || ts.isArrowFunction(node)
      || ts.isMethodDeclaration(node) || ts.isConstructorDeclaration(node) || ts.isGetAccessorDeclaration(node);
    if (callable) {
      const line = source.getLineAndCharacterOfPosition(node.getStart(source)).line + 1;
      let name = node.name?.getText(source);
      let trigger = '';
      if (ts.isConstructorDeclaration(node)) name = 'constructor';
      if (!name && ts.isVariableDeclaration(node.parent)) name = node.parent.name.getText(source);
      if (!name && ts.isPropertyAssignment(node.parent)) name = node.parent.name.getText(source);
      if (!name && ts.isCallExpression(node.parent) && /(?:^|\.)useCallback$/.test(node.parent.expression.getText(source)) && ts.isVariableDeclaration(node.parent.parent)) name = node.parent.parent.name.getText(source);
      if (ts.isJsxExpression(node.parent) && ts.isJsxAttribute(node.parent.parent)) trigger = node.parent.parent.name.getText(source);
      if (ts.isCallExpression(node.parent)) trigger = callName(node.parent.expression);
      if (!name) name = `${trigger || 'callback'}@L${line}`;
      const qualified = [...scope, name].join('.');
      const calls = new Set();
      function facts(child) {
        if (child !== node && (ts.isFunctionDeclaration(child) || ts.isFunctionExpression(child) || ts.isArrowFunction(child))) return;
        if (ts.isCallExpression(child)) calls.add(callName(child.expression));
        ts.forEachChild(child, facts);
      }
      facts(node);
      const key = `${qualified}:${line}`;
      if (!seen.has(key)) {
        seen.add(key);
        const doc = (ts.getJSDocCommentsAndTags(node) || []).map(x => x.comment).filter(x => typeof x === 'string').join('\n').replace(/\r\n/g, '\n');
        records.push({ name: qualified, line, kind: node.modifiers?.some(m => m.kind === ts.SyntaxKind.AsyncKeyword) ? 'async function' : 'function/callback',
          parameters: node.parameters.map(p => compact(p.getText(source))).join(', '), trigger, doc, calls: [...calls].sort(),
          returnType: node.type ? compact(node.type.getText(source)) : '由实现/TypeScript 推断' });
      }
      next = [...scope, name];
    } else if (ts.isClassDeclaration(node) && node.name) next = [...scope, node.name.getText(source)];
    ts.forEachChild(node, child => visit(child, next));
  }
  visit(source);
  result[relative] = records.sort((a, b) => a.line - b.line);
}
process.stdout.write(JSON.stringify(result));
