const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const ts = require('typescript');

const source = fs.readFileSync(path.join(__dirname, '../components/speechText.ts'), 'utf8');
const compiled = ts.transpileModule(source, {
  compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2020 },
}).outputText;
const moduleExports = {};
new Function('exports', compiled)(moduleExports);
const { speechText } = moduleExports;

const examples = [
  ['Read [the article](https://example.com/article?tracking=very-long).', 'Read the article.'],
  ['![A mountain lake](https://images.example.com/photo.jpg?signature=long)', 'A mountain lake'],
  ['See https://example.com/path?query=long for details.', 'See for details.'],
  ['Source: <https://example.com/long/path>', 'Source:'],
  ['Visit www.example.com/long/path.', 'Visit.'],
  ['[Mercury](https://example.com/Mercury_(planet)) is small.', 'Mercury is small.'],
  ['[Source](https://example.com "Article title")', 'Source'],
  ['[Report][ref]\n\n[ref]: https://example.com/long/path', 'Report'],
  ['[Download report](/api/files/download/long-token)', 'Download report'],
  ['https://example.com/image.png', ''],
  ['![https://example.com/image.png](https://example.com/image.png)', ''],
  ['Δείτε [την εικόνα](https://example.com/photo).', 'Δείτε την εικόνα.'],
  ['The answer is 42. It costs $5.', 'The answer is 42. It costs $5.'],
];
for (const [input, expected] of examples) {
  test(input, () => assert.equal(speechText(input), expected));
}
