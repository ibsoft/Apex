/* A tiny dependency-free syntax highlighter for fenced code blocks.

   It is deliberately a *pure tokenizer*: `tokenize(code, language)` returns a
   flat list of `{ type, value }` whose concatenated `value`s are the original
   source, so the renderer only maps types to colours and can never drop text.
   Colouring is not parsing — a mis-classified word is cosmetic, and the tests
   pin the round-trip invariant that guarantees the code the operator sees is
   exactly the code the model wrote.
*/

export type TokenType =
  | "plain"
  | "comment"
  | "string"
  | "number"
  | "keyword"
  | "builtin"
  | "type"
  | "function"
  | "property"
  | "operator"
  | "punct"
  | "tag"
  | "attr";

export interface Token {
  type: TokenType;
  value: string;
}

const PY_KEYWORDS = [
  "def", "class", "import", "from", "as", "if", "elif", "else", "for", "while",
  "return", "yield", "lambda", "with", "try", "except", "finally", "raise",
  "pass", "break", "continue", "global", "nonlocal", "del", "assert", "in",
  "is", "not", "and", "or", "None", "True", "False", "async", "await", "self",
];

const JS_KEYWORDS = [
  "const", "let", "var", "function", "return", "if", "else", "for", "while",
  "do", "switch", "case", "break", "continue", "new", "class", "extends",
  "super", "this", "typeof", "instanceof", "void", "delete", "in", "of",
  "import", "export", "default", "async", "await", "try", "catch", "finally",
  "throw", "yield", "null", "undefined", "true", "false", "interface", "type",
  "enum", "implements", "public", "private", "protected", "readonly", "static",
  "get", "set", "as", "satisfies", "namespace", "declare",
];

const SH_KEYWORDS = [
  "if", "then", "else", "elif", "fi", "for", "in", "do", "done", "while",
  "until", "case", "esac", "function", "return", "exit", "export", "local",
  "source", "readonly", "declare", "select", "time",
];

const C_KEYWORDS = [
  "const", "static", "struct", "class", "public", "private", "protected",
  "void", "int", "long", "short", "char", "float", "double", "bool", "boolean",
  "unsigned", "signed", "return", "if", "else", "for", "while", "do", "switch",
  "case", "break", "continue", "new", "delete", "try", "catch", "finally",
  "throw", "throws", "import", "package", "namespace", "using", "template",
  "typename", "auto", "fn", "let", "mut", "pub", "impl", "trait", "match",
  "enum", "interface", "extends", "implements", "null", "true", "false",
  "func", "defer", "go", "chan", "var", "range", "nil",
];

const GENERIC_KEYWORDS = new Set([
  ...PY_KEYWORDS, ...JS_KEYWORDS, ...SH_KEYWORDS, ...C_KEYWORDS,
]);

const PY_KEYWORD_SET = new Set(PY_KEYWORDS);
const JS_KEYWORD_SET = new Set(JS_KEYWORDS);
const SH_KEYWORD_SET = new Set(SH_KEYWORDS);
const C_KEYWORD_SET = new Set(C_KEYWORDS);

const BUILTINS = new Set([
  // python
  "print", "len", "range", "str", "int", "float", "list", "dict", "set",
  "tuple", "map", "filter", "sum", "min", "max", "abs", "open", "isinstance",
  "enumerate", "zip", "sorted", "reversed", "super", "object",
  // js / ts
  "console", "document", "window", "Math", "JSON", "Object", "Array",
  "String", "Number", "Boolean", "Promise", "Map", "Set", "Date", "RegExp",
  "Error", "require", "module", "exports", "process", "globalThis",
  // shell
  "echo", "cd", "pwd", "ls", "mkdir", "rm", "cp", "mv", "cat", "grep", "sed",
  "awk", "printf", "test", "expr", "sudo", "apt", "npm", "git", "python",
  "node", "pip", "curl", "wget",
]);

interface BlockRule {
  start: string;
  end: string;
  type: "comment" | "string";
}

interface Profile {
  line: string[];
  blocks: BlockRule[];
  keywords: Set<string>;
  template: boolean;
  html: boolean;
}

const GENERIC_PROFILE: Profile = {
  line: ["//", "#"],
  blocks: [{ start: "/*", end: "*/", type: "comment" }],
  keywords: GENERIC_KEYWORDS,
  template: true,
  html: false,
};

export function profileFor(language: string): Profile {
  const l = (language || "").trim().toLowerCase();
  if (l === "python" || l === "py") {
    return {
      line: ["#"],
      blocks: [
        { start: '"""', end: '"""', type: "string" },
        { start: "'''", end: "'''", type: "string" },
      ],
      keywords: PY_KEYWORD_SET,
      template: false,
      html: false,
    };
  }
  if (["js", "javascript", "ts", "typescript", "jsx", "tsx", "node"].includes(l)) {
    return {
      line: ["//"],
      blocks: [{ start: "/*", end: "*/", type: "comment" }],
      keywords: JS_KEYWORD_SET,
      template: true,
      html: false,
    };
  }
  if (["sh", "bash", "shell", "zsh", "console", "bashrc", "fish"].includes(l)) {
    return { line: ["#"], blocks: [], keywords: SH_KEYWORD_SET, template: false, html: false };
  }
  if (["html", "xml", "svg", "vue", "svelte"].includes(l)) {
    return { line: [], blocks: [], keywords: new Set(), template: false, html: true };
  }
  if (["css", "scss", "less"].includes(l)) {
    return {
      line: [],
      blocks: [{ start: "/*", end: "*/", type: "comment" }],
      keywords: new Set(),
      template: false,
      html: false,
    };
  }
  if (["c", "cpp", "c++", "h", "hpp", "java", "go", "rust", "rs", "swift", "kt", "kotlin", "cs", "csharp"].includes(l)) {
    return {
      line: ["//"],
      blocks: [{ start: "/*", end: "*/", type: "comment" }],
      keywords: C_KEYWORD_SET,
      template: false,
      html: false,
    };
  }
  return GENERIC_PROFILE;
}

const NUMBER_RE = /0[xX][0-9a-fA-F_]+|0[bB][01_]+|\d[\d_]*(?:\.[\d_]*)?(?:[eE][+-]?\d+)?/y;
const IDENT_RE = /[A-Za-z_$][A-Za-z0-9_$]*/y;
const OP_RE = /(?:=>|->|::|==|!=|<=|>=|&&|\|\||\.\.\.|[+\-*/%=<>!&|^~?:])+/y;

function readString(
  code: string,
  start: number,
  quote: string,
  template: boolean,
): { value: string; next: number } {
  let i = start + 1;
  while (i < code.length) {
    const ch = code[i];
    if (ch === "\\") {
      i += 2;
      continue;
    }
    if (ch === quote) {
      i++;
      break;
    }
    // A single/double quoted string never spans a newline; a template literal
    // does, so only stop the non-template quote at the line break.
    if (ch === "\n" && !template) break;
    i++;
  }
  return { value: code.slice(start, i), next: i };
}

export function tokenize(code: string, language: string): Token[] {
  const p = profileFor(language);
  if (p.html) return tokenizeHtml(code);

  const tokens: Token[] = [];
  const push = (type: TokenType, value: string) => {
    if (value) tokens.push({ type, value });
  };
  const n = code.length;
  let i = 0;
  let block: { end: string; type: TokenType } | null = null;

  while (i < n) {
    if (block) {
      const close = code.indexOf(block.end, i);
      if (close === -1) {
        push(block.type, code.slice(i));
        i = n;
      } else {
        push(block.type, code.slice(i, close + block.end.length));
        i = close + block.end.length;
      }
      block = null;
      continue;
    }

    let matchedBlock = false;
    for (const b of p.blocks) {
      if (code.startsWith(b.start, i)) {
        const close = code.indexOf(b.end, i + b.start.length);
        if (close === -1) {
          block = { end: b.end, type: b.type };
          push(b.type, code.slice(i));
          i = n;
        } else {
          push(b.type, code.slice(i, close + b.end.length));
          i = close + b.end.length;
        }
        matchedBlock = true;
        break;
      }
    }
    if (matchedBlock) continue;

    const ch = code[i];

    let lineComment = false;
    for (const marker of p.line) {
      if (code.startsWith(marker, i)) {
        const end = code.indexOf("\n", i);
        push("comment", code.slice(i, end === -1 ? n : end));
        i = end === -1 ? n : end;
        lineComment = true;
        break;
      }
    }
    if (lineComment) continue;

    if (ch === '"' || ch === "'" || (ch === "`" && p.template)) {
      const { value, next } = readString(code, i, ch, p.template || ch === "`");
      push("string", value);
      i = next;
      continue;
    }

    if (/[0-9]/.test(ch) || (ch === "." && /[0-9]/.test(code[i + 1] ?? ""))) {
      NUMBER_RE.lastIndex = i;
      const m = NUMBER_RE.exec(code);
      if (m && m.index === i) {
        push("number", m[0]);
        i = NUMBER_RE.lastIndex;
        continue;
      }
    }

    if (/[A-Za-z_$]/.test(ch)) {
      IDENT_RE.lastIndex = i;
      const m = IDENT_RE.exec(code)!;
      const word = m[0];
      const after = code[i + word.length];
      const before = i > 0 ? code[i - 1] : "";
      let type: TokenType = "plain";
      if (p.keywords.has(word)) type = "keyword";
      else if (BUILTINS.has(word)) type = "builtin";
      else if (before === ".") type = "property";
      else if (after === "(") type = "function";
      else if (/^[A-Z]/.test(word)) type = "type";
      push(type, word);
      i += word.length;
      continue;
    }

    if (/[+\-*/%=<>!&|^~?:]/.test(ch)) {
      OP_RE.lastIndex = i;
      const m = OP_RE.exec(code)!;
      push("operator", m[0]);
      i = OP_RE.lastIndex;
      continue;
    }

    if ("(){}[];,".includes(ch)) {
      push("punct", ch);
      i++;
      continue;
    }

    const ws = /^\s+/.exec(code.slice(i));
    if (ws) {
      push("plain", ws[0]);
      i += ws[0].length;
      continue;
    }
    push("plain", ch);
    i++;
  }
  return tokens;
}

function tokenizeHtml(code: string): Token[] {
  const tokens: Token[] = [];
  const push = (type: TokenType, value: string) => {
    if (value) tokens.push({ type, value });
  };
  const n = code.length;
  let i = 0;
  while (i < n) {
    if (code.startsWith("<!--", i)) {
      const end = code.indexOf("-->", i + 4);
      const stop = end === -1 ? n : end + 3;
      push("comment", code.slice(i, stop));
      i = stop;
      continue;
    }
    if (code[i] === "<") {
      const end = code.indexOf(">", i);
      const stop = end === -1 ? n : end + 1;
      pushHtmlTag(tokens, code.slice(i, stop));
      i = stop;
      continue;
    }
    const next = code.indexOf("<", i);
    const stop = next === -1 ? n : next;
    push("plain", code.slice(i, stop));
    i = stop;
  }
  return tokens;
}

function pushHtmlTag(tokens: Token[], tag: string): void {
  const push = (type: TokenType, value: string) => {
    if (value) tokens.push({ type, value });
  };
  push("plain", "<");
  let j = 1;
  if (tag[j] === "/") {
    push("plain", "/");
    j++;
  }
  const nm = /^[A-Za-z][\w:-]*/.exec(tag.slice(j));
  if (nm) {
    push("tag", nm[0]);
    j += nm[0].length;
  }
  const re = /"[^"]*"|'[^']*'|[A-Za-z_:][\w:.-]*|\s+|=|[\s\S]/g;
  const rest = tag.slice(j);
  let m: RegExpExecArray | null;
  while ((m = re.exec(rest)) !== null) {
    const v = m[0];
    if (v[0] === '"' || v[0] === "'") push("string", v);
    else if (v === "=") push("operator", v);
    else if (/^\s+$/.test(v)) push("plain", v);
    else if (/^[A-Za-z_:]/.test(v)) push("attr", v);
    else push("plain", v);
  }
}
