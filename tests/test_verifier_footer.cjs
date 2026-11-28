const assert = require('node:assert/strict');
const { spawnSync } = require('node:child_process');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const ejs = require('ejs');
const frontMatter = require('hexo-front-matter');

const root = path.resolve(__dirname, '..');
const template = fs.readFileSync(path.join(root, 'themes/jasdoit/layout/post.ejs'), 'utf8');

function render(fields = {}) {
  return ejs.render(template, {
    page: { title: '閱讀練習', content: '<p>本文</p>', ...fields },
    theme: { tagline: '每天讀一點日文' },
    url_for: value => value,
    date_xml: () => '2026-11-28',
    date: () => '2026.11.28',
    toc: () => '',
  });
}

test('舊文章與空報告不顯示 Verifier 區塊', () => {
  assert.doesNotMatch(render(), /class="verifier-reports"/);
  assert.doesNotMatch(render({ verifier_reports: [] }), /class="verifier-reports"/);
});

test('報告在閱讀結尾之後預設收合，保留 PASS 與 FLAG 的完整意見', () => {
  const reports = [
    { model: 'openai/test', verdict: 'FLAG', text: 'VERDICT: FLAG\n\n完整建議第一行。\n第二行。' },
    { model: 'z-ai/test', verdict: 'PASS', text: 'VERDICT: PASS\n\n也保留 PASS 建議。' },
  ];
  const html = render({ verifier_reports: reports });
  const details = html.match(/<details class="verifier-reports"([^>]*)>/);
  assert.ok(details);
  assert.doesNotMatch(details[1], /\bopen\b/);
  assert.equal((html.match(/<details\b/g) || []).length, 1);
  assert.ok(html.indexOf('class="reading-end"') < html.indexOf(details[0]));
  assert.match(html, /2 份報告/);
  for (const report of reports) {
    assert.ok(html.includes(report.model));
    assert.ok(html.includes(report.verdict));
    assert.ok(html.includes(report.text));
  }
});

test('每個模型欄位與完整意見都以純文字顯示', () => {
  const hostile = '</pre><script>alert("x")</script><ruby>危険<rt>きけん</rt></ruby>';
  const html = render({ verifier_reports: [{ model: hostile, verdict: hostile, text: hostile }] });
  assert.doesNotMatch(html, /<script>|<ruby>|<rt>|alert\("x"\)/);
  assert.equal((html.match(/&lt;script&gt;/g) || []).length, 3);
  assert.equal((html.match(/&lt;\/pre&gt;/g) || []).length, 3);
  assert.match(html, /&lt;ruby&gt;/);
});

test('文章 FLAG 時摘要僅標示報告數，不把單一 PASS 說成整體通過', () => {
  const html = render({ status: 'flag', verifier_reports: [
    { model: 'openai/test', verdict: 'PASS', text: 'VERDICT: PASS' },
  ] });
  const summary = html.match(/<summary>([\s\S]*?)<\/summary>/)[1];
  assert.match(summary, /1 份報告/);
  assert.doesNotMatch(summary, /PASS|通過/);
});

test('引擎寫入的惡意報告仍是合法 Hexo metadata，不會成為本文或 HTML', () => {
  const text = 'VERDICT: PASS\n\n---\n## 日文文章\n</pre><script>alert("x")</script>\n<ruby>危険<rt>きけん</rt></ruby>';
  const generated = spawnSync('python3', ['-c', `
import runpy
import sys
import tempfile
from pathlib import Path
suite = runpy.run_path('tests/test_daily_loop.py')
text = sys.stdin.read()
with tempfile.TemporaryDirectory() as tmp:
    repo = suite['FixtureRepo'](Path(tmp))
    runners = suite['FakeRunners']()
    runners.verify = lambda *args: (0, text + '\\nMETA {}', '', None)
    code, loop, transcript = suite['run'](repo, runners)
    assert code == 0, transcript
    sys.stdout.write(runners.posts_seen[-1])
`], { cwd: root, input: text, encoding: 'utf8' });
  assert.equal(generated.status, 0, generated.stderr);
  const parsed = frontMatter.parse(generated.stdout);
  assert.equal(parsed.verifier_reports[0].text, text);
  assert.doesNotMatch(parsed._content, /<script>|危険|VERDICT:/);
  const html = render({ verifier_reports: parsed.verifier_reports });
  assert.doesNotMatch(html, /<script>|<ruby>|<rt>/);
  assert.match(html, /&lt;\/pre&gt;&lt;script&gt;/);
});

test('Day 25 的 Frontmatter 可由 Hexo 解析，原始報告不進入文章本文', () => {
  const post = fs.readFileSync(path.join(root, 'source/_posts/day-025-mainichiaukotoba.md'), 'utf8');
  const parsed = frontMatter.parse(post);
  assert.equal(parsed.status, 'flag');
  assert.deepEqual(parsed.verifier_reports.map(report => [report.model, report.verdict]), [
    ['openai/gpt-6.1-sol', 'FLAG'], ['z-ai/glm-5.3', 'PASS'],
  ]);
  assert.ok(parsed.verifier_reports[0].text.includes('漏掉必要的「に」'));
  assert.doesNotMatch(parsed._content, /VERDICT:|## verifier/);
  const html = render({ verifier_reports: parsed.verifier_reports });
  assert.match(html, /漏掉必要的「に」/);
  assert.match(html, /章節結構完整、正文長度合理、前後連貫。/);
});
